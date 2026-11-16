"""
llm.py - everything that talks to the LLM (Groq by default).

What this file does:
  1. Builds the prompt (system instructions + chat history + the new question).
  2. Calls the LLM through the `openai` package. Groq and Gemini both offer an
     "OpenAI-compatible" API, so switching provider = changing 3 lines in .env.
  3. Handles free-tier rate limits: wait + retry, then try a fallback model.
  4. Parses the JSON reply {title, explanation, cpp, c, python}.
"""

import json
import logging
import os
import re
import time

import openai
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "llama-3.1-8b-instant")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

MAX_RETRIES = 2          # extra tries on the main model after a 429
MAX_WAIT_SECONDS = 20    # never sleep longer than this between tries
MAX_OUTPUT_TOKENS = 4096
MAX_STATEMENT_CHARS = 6000  # very long statements are cut to save tokens

LANGUAGES = ("cpp", "c", "python")
LANGUAGE_NAMES = {"cpp": "C++", "c": "C", "python": "Python"}

SYSTEM_PROMPT = """You are LeetBot, a friendly DSA tutor on WhatsApp who helps students.

Reply with ONE valid JSON object and nothing else. It must have exactly these keys,
and every value must be a string:
  "title", "explanation", "cpp", "c", "python"

"title": a short title, e.g. "1. Two Sum (Easy)" or "Hollow Diamond Pattern".

"explanation": short (under about 1200 characters), in this layout:
*Idea:* 2-3 lines with the key insight.
*Approach:* numbered steps (1. 2. 3.).
*Complexity:* Time O(...), Space O(...).
*Dry run:* one small example traced briefly.
Use WhatsApp formatting only: *bold* and _italic_. Never use markdown headers (#),
tables or **double asterisks**.

"cpp", "c", "python": raw source code only (no ``` fences), with short helpful comments.

Rules for LeetCode problems:
- Use EXACTLY the given starter-code signature for each language so the solution can be
  pasted into LeetCode and submitted. Give the optimal solution.
- The C solution must manage memory the way LeetCode expects: malloc the returned
  arrays, set *returnSize (and *returnColumnSizes when present). Do not write main().

Rules for pattern-printing questions (stars, numbers, pyramids, diamonds...):
- In "explanation": first show the expected output for a small n inside one ``` block,
  then explain what the outer loop variable i controls (rows), what the inner loop
  variable j controls (columns), and how many spaces and stars each row prints
  (as a formula in terms of i and n). Skip the Dry run.
- Code must be FULL runnable programs with main() (and input of n) in each language.

Language rule: the user message tells you which languages are wanted. Fill only those
keys with code and set the other code keys to "" (empty string).

Follow-up questions (e.g. "explain step 2", "optimize it", "give C code only"): use the
chat history, put your answer in "explanation", and include code only in the languages
that are actually needed - set the rest to "".
"""


class RateLimited(Exception):
    """Both the main and the fallback model said 'too many requests' (HTTP 429)."""


class LLMError(Exception):
    """The LLM could not be reached or returned an error."""


# The client is created the first time we need it (so importing this file in
# tests never needs a real API key).
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY or "missing-key",
            timeout=60,
            max_retries=0,  # we do our own retrying below
        )
    return _client


# ---------------------------------------------------------------------------
# Building the prompt
# ---------------------------------------------------------------------------

def _languages_line(lang):
    """Sentence telling the LLM which languages to write code in."""
    if lang in LANGUAGES:
        return (f"Languages wanted: only {LANGUAGE_NAMES[lang]} "
                f"(unless I explicitly ask for another language in this message).")
    return "Languages wanted: C++, C and Python (all three)."


def build_user_prompt(user_text, problem=None, lang="all"):
    """
    Build the text of the newest user message.
    If `problem` (from leetcode.fetch_problem) is given, we include the real
    statement and the exact starter code so solutions are submittable.
    """
    wanted = LANGUAGES if lang not in LANGUAGES else (lang,)

    if problem is None:
        return f"{user_text}\n\n{_languages_line(lang)}"

    parts = [
        f"Solve LeetCode problem {problem['id']}. {problem['title']} "
        f"({problem['difficulty']}).",
    ]
    if problem.get("statement"):
        parts.append("Problem statement:\n" + problem["statement"][:MAX_STATEMENT_CHARS])
    else:
        parts.append("(The statement could not be downloaded - it may be a premium "
                     "problem. Solve it from your own knowledge of this problem.)")

    for name in wanted:
        snippet = (problem.get("snippets") or {}).get(name)
        if snippet:
            parts.append(f"Starter code for {LANGUAGE_NAMES[name]} "
                         f"(keep this exact signature):\n{snippet}")

    parts.append(_languages_line(lang))
    return "\n\n".join(parts)


def build_messages(user_prompt, history=()):
    """System prompt + earlier chat turns + the new question."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})
    return messages


# ---------------------------------------------------------------------------
# Calling the LLM with retry + fallback
# ---------------------------------------------------------------------------

def _call_model(model, messages):
    """One request to one model. Returns the reply text."""
    client = _get_client()
    options = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,   # low = focused, less "creative" code
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    try:
        # "JSON mode" forces the model to produce valid JSON.
        response = client.chat.completions.create(
            response_format={"type": "json_object"}, **options)
    except openai.BadRequestError as error:
        # Some models don't support JSON mode, or the model produced broken
        # JSON and the provider rejected it. Try once more as plain text;
        # parse_reply() below can still dig the JSON out.
        log.warning("JSON mode failed on %s (%s) - retrying without it", model, error)
        response = client.chat.completions.create(**options)
    return response.choices[0].message.content or ""


def _wait_time(error, attempt):
    """How long to sleep after a 429. Uses the server's Retry-After hint if any."""
    seconds = 5 * (attempt + 1)  # 5s, then 10s
    try:
        seconds = float(error.response.headers.get("retry-after", seconds))
    except (AttributeError, TypeError, ValueError):
        pass
    return max(1.0, min(seconds, MAX_WAIT_SECONDS))


def chat(messages):
    """
    Ask the LLM, surviving free-tier rate limits:
      main model:      1 try + up to MAX_RETRIES retries (waiting in between)
      fallback model:  1 try
    Raises RateLimited if we were rate-limited all the way, LLMError otherwise.
    """
    models = [LLM_MODEL]
    if LLM_FALLBACK_MODEL and LLM_FALLBACK_MODEL != LLM_MODEL:
        models.append(LLM_FALLBACK_MODEL)

    was_rate_limited = False
    for index, model in enumerate(models):
        attempts = 1 + MAX_RETRIES if index == 0 else 1
        for attempt in range(attempts):
            try:
                return _call_model(model, messages)
            except openai.RateLimitError as error:
                was_rate_limited = True
                log.warning("429 rate limit on %s (try %d/%d)", model, attempt + 1, attempts)
                if attempt < attempts - 1:
                    time.sleep(_wait_time(error, attempt))
            except openai.OpenAIError as error:
                # Any other problem (model removed, server error, bad key...):
                # no point retrying the same model, move on to the fallback.
                log.error("LLM error on %s: %s", model, error)
                break

    if was_rate_limited:
        raise RateLimited()
    raise LLMError("LLM request failed")


# ---------------------------------------------------------------------------
# Parsing the reply
# ---------------------------------------------------------------------------

# Models sometimes use slightly different key names; map them to ours.
KEY_ALIASES = {"c++": "cpp", "cplusplus": "cpp", "python3": "python", "py": "python"}


def _strip_code_fence(code):
    """Remove ``` fences if the model wrapped the code in them anyway."""
    code = code.strip("\n")
    code = re.sub(r"^\s*```[a-zA-Z+]*\n", "", code)
    code = re.sub(r"\n```\s*$", "", code)
    return code


def parse_reply(raw):
    """
    Turn the LLM's text into {"title", "explanation", "cpp", "c", "python"}.
    Returns None if it isn't usable JSON - the caller then sends the raw text.
    """
    if not raw or not raw.strip():
        return None

    # Take everything from the first "{" to the last "}" - this ignores any
    # ```json fence or chatter the model added around the JSON.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        # strict=False allows real newlines inside strings (a common LLM slip).
        data = json.loads(raw[start:end + 1], strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    reply = {"title": "", "explanation": "", "cpp": "", "c": "", "python": ""}
    for key, value in data.items():
        key = KEY_ALIASES.get(str(key).lower(), str(key).lower())
        if key in reply and isinstance(value, str):
            reply[key] = value

    for lang in LANGUAGES:
        reply[lang] = _strip_code_fence(reply[lang])

    # JSON that has none of the useful parts is treated as a failure.
    if not reply["explanation"].strip() and not any(reply[lang].strip() for lang in LANGUAGES):
        return None
    return reply


def solve(user_text, problem=None, lang="all", history=()):
    """
    The one function app.py calls.
    Returns (reply, raw): reply is the parsed dict (or None if parsing failed),
    raw is the LLM's original text.
    """
    prompt = build_user_prompt(user_text, problem, lang)
    raw = chat(build_messages(prompt, history))
    return parse_reply(raw), raw
