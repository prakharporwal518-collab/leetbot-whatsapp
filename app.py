"""
app.py - the Flask web server (the "front door" of LeetBot).

Flow of one WhatsApp message:
  1. You send a message to the bot's number.
  2. Meta sends an HTTP POST to /whatsapp on this server (a "webhook").
  3. We answer 200 immediately (Meta wants a fast answer, otherwise it retries)
     and start a background thread to do the slow work.
  4. The thread fetches the LeetCode problem (leetcode.py), asks the LLM
     (llm.py) and sends the packed reply back (whatsapp.py).

Run locally:   python app.py
Run on Render: gunicorn app:app --workers 1 --threads 8
(ONE worker on purpose: the memory below - seen message ids, chat history,
language choice - lives inside the process and would not be shared by several.)
"""

import logging
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque

from dotenv import load_dotenv
from flask import Flask, request

import leetcode
import llm
import whatsapp

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leetbot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory state (lost when the server restarts - fine for a free hobby bot)
# ---------------------------------------------------------------------------

MAX_SEEN_IDS = 500      # remember this many message ids to ignore Meta's retries
HISTORY_LENGTH = 6      # messages remembered per user (for follow-ups)
HISTORY_ITEM_CHARS = 3000  # each remembered message is cut to this (saves LLM tokens)

_seen_ids = OrderedDict()   # message id -> True, oldest first
_state_lock = threading.Lock()
user_lang = {}              # phone number -> "all" | "cpp" | "c" | "python"
user_history = {}           # phone number -> deque of {"role", "content"}

HELP_WORDS = {"hi", "hello", "hey", "help", "menu", "start"}

# What the user may type after "lang" -> our internal name.
LANG_CHOICES = {
    "all": "all",
    "cpp": "cpp", "c++": "cpp",
    "c": "c",
    "python": "python", "python3": "python", "py": "python",
}

HELP_TEXT = (
    "👋 *LeetBot* - LeetCode solutions in C++, C and Python\n\n"
    "*Send me any of these:*\n"
    "• A problem number: `1` or `#206`\n"
    "• A slug: `two-sum`\n"
    "• A link: leetcode.com/problems/two-sum\n"
    "• Any question: _print a hollow diamond of stars_\n\n"
    "*Follow-ups work too:* _explain step 2_, _optimize it_\n\n"
    "*Choose languages:*\n"
    "• `lang all` - C++, C and Python (default)\n"
    "• `lang cpp` / `lang c` / `lang python` - only one (shorter replies)"
)

TEXT_ONLY_REPLY = "Please send text only"
RATE_LIMIT_REPLY = "Too many requests, try again in a minute."
ERROR_REPLY = "😅 Sorry, something went wrong on my side. Please try again in a moment."


def is_duplicate(message_id):
    """
    True if we already handled this message id. Otherwise remember it.
    Meta re-sends a webhook when our server was slow (e.g. Render waking up),
    and without this check the user would get the same answer twice.
    """
    with _state_lock:
        if message_id in _seen_ids:
            return True
        _seen_ids[message_id] = True
        if len(_seen_ids) > MAX_SEEN_IDS:
            _seen_ids.popitem(last=False)  # forget the oldest id
        return False


def _remember(sender, role, content):
    """Save one chat turn for this user (only the last HISTORY_LENGTH are kept)."""
    with _state_lock:
        history = user_history.setdefault(sender, deque(maxlen=HISTORY_LENGTH))
        history.append({"role": role, "content": content[:HISTORY_ITEM_CHARS]})


def _get_history(sender):
    with _state_lock:
        return list(user_history.get(sender, ()))


def _handle_lang_command(sender, words):
    """'lang cpp' -> remember the choice and return a short confirmation."""
    choice = LANG_CHOICES.get(words[1]) if len(words) == 2 else None
    if choice is None:
        return "Use: `lang all`, `lang cpp`, `lang c` or `lang python`"
    user_lang[sender] = choice
    if choice == "all":
        return "✅ I'll answer in C++, C and Python."
    return f"✅ I'll answer in {llm.LANGUAGE_NAMES[choice]} only. Send `lang all` to get all three."


def build_reply(sender, text):
    """
    Work out the reply for one text message.
    Returns a list of WhatsApp messages (strings) ready to send.
    """
    text = text.strip()
    words = text.lower().split()

    # --- Commands that don't need the LLM (instant and free) ---
    if not words or text.lower().strip(" !.?") in HELP_WORDS:
        return [HELP_TEXT]
    if words[0] == "lang":
        return [_handle_lang_command(sender, words)]

    lang = user_lang.get(sender, "all")

    # --- Is it a LeetCode problem? Try to download the real one. ---
    kind, value = leetcode.parse_input(text)
    problem = None
    if kind != "text":
        try:
            problem = leetcode.fetch_problem(text)
        except Exception as error:  # LeetCode down/blocked: the LLM can still try
            log.warning("LeetCode fetch failed for %r: %s", text, error)

    question = text
    if problem is None and kind in ("number", "url"):
        # We know the user meant a LeetCode problem, but couldn't download it.
        question = (f"Solve LeetCode problem '{value}' from your own knowledge. If you "
                    f"don't know this problem, say so in the explanation.")

    # --- Ask the LLM ---
    history = _get_history(sender)
    try:
        reply, raw = llm.solve(question, problem=problem, lang=lang, history=history)
    except llm.RateLimited:
        return [RATE_LIMIT_REPLY]

    # --- Pack the answer into as few messages as possible ---
    if reply is not None:
        title = reply["title"]
        if problem:  # use the real title from LeetCode, not the LLM's guess
            title = f"{problem['id']}. {problem['title']} ({problem['difficulty']})"
        blocks = whatsapp.build_blocks(title, reply["explanation"], reply)
    else:
        # JSON parsing failed: just send what the LLM wrote.
        blocks = [{"kind": "text", "body": whatsapp.to_whatsapp_format(raw)}]

    messages = whatsapp.pack_messages(blocks)
    if not messages:
        return [ERROR_REPLY]

    # Remember this exchange so follow-ups like "optimize it" make sense.
    asked = f"LeetCode {problem['id']}. {problem['title']}" if problem else text
    _remember(sender, "user", asked)
    _remember(sender, "assistant", "\n\n".join(messages))
    return messages


def _report_if_stuck(thread_id, started_at):
    """Watchdog: if a reply is still not finished, log WHERE the code is waiting."""
    frame = sys._current_frames().get(thread_id)
    if frame is not None:
        log.warning("Reply still running after %d s. Stuck here:\n%s",
                    time.time() - started_at, "".join(traceback.format_stack(frame)[-12:]))


def handle_message(sender, text):
    """Runs in a background thread. Must never raise."""
    watchdog = threading.Timer(30, _report_if_stuck, [threading.get_ident(), time.time()])
    watchdog.daemon = True
    watchdog.start()
    try:
        _handle_message(sender, text)
    finally:
        watchdog.cancel()


def _handle_message(sender, text):
    try:
        messages = build_reply(sender, text)
    except Exception:
        log.exception("Failed to handle message from %s", sender)
        messages = [ERROR_REPLY]
    try:
        log.info("Sending %d message(s) to ...%s", len(messages), sender[-4:])
        whatsapp.send_messages(sender, messages)
    except Exception:
        log.exception("Failed to send reply to %s", sender)


def run_in_background(function, *args):
    """Start `function(*args)` in a thread so the webhook can return at once."""
    threading.Thread(target=function, args=args, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/whatsapp")
def verify():
    """Meta calls this once when you save the webhook URL in the dashboard."""
    body, status = whatsapp.verify_webhook(
        request.args.get("hub.mode"),
        request.args.get("hub.verify_token"),
        request.args.get("hub.challenge"),
    )
    return body, status


@app.post("/whatsapp")
def webhook():
    """Meta calls this for every incoming message and every status update."""
    try:
        payload = request.get_json(silent=True) or {}
        log.info("Webhook: %s", whatsapp.describe_webhook(payload))
        message = whatsapp.parse_incoming(payload)

        # Not a message (e.g. "delivered"/"read" status) or a retry we've seen.
        if message is None or is_duplicate(message["id"]):
            return "ok", 200

        if message["type"] != "text":
            run_in_background(whatsapp.send_messages, message["from"], [TEXT_ONLY_REPLY])
        else:
            run_in_background(handle_message, message["from"], message["text"])
    except Exception:
        # Whatever happens, answer 200 - otherwise Meta keeps retrying.
        log.exception("Error in webhook")
    return "ok", 200


@app.get("/health")
def health():
    """Pinged by cron-job.org every 10 minutes so Render's free tier stays awake."""
    return "ok", 200


PRIVACY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LeetBot - Privacy Policy</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;
padding:0 16px;line-height:1.6;color:#222}h1,h2{line-height:1.25}</style></head><body>
<h1>LeetBot - Privacy Policy</h1>
<p>LeetBot is a personal, non-commercial WhatsApp chatbot that explains
programming problems. This page describes what happens to the messages you send it.</p>
<h2>What LeetBot receives</h2>
<p>When you message LeetBot, it receives your WhatsApp phone number and the text
of your message through the WhatsApp Business Platform (Meta).</p>
<h2>How it is used</h2>
<ul>
<li>Your message text is sent to an AI language-model provider (Groq) to write the
answer. Your phone number is not sent to that provider.</li>
<li>If you ask for a LeetCode problem, the problem number or name is looked up on
leetcode.com. No personal data is sent.</li>
<li>The answer is sent back to you on WhatsApp.</li>
</ul>
<h2>What is stored</h2>
<p>LeetBot has no database. It keeps only your last 6 messages and your language
choice in the server's memory so follow-up questions work. This memory is erased
whenever the server restarts. Server logs may briefly contain phone numbers and
technical error details.</p>
<h2>Sharing</h2>
<p>Your data is never sold or used for advertising. It is shared only with the
services needed to answer you: Meta (WhatsApp), Groq and the hosting provider (Render).</p>
<h2>Your choices</h2>
<p>Stop messaging the bot at any time and no further data is processed. To ask a
question or request removal of any data, contact the owner through the project page:
<a href="https://github.com/prakharporwal518-collab/leetbot-whatsapp">
github.com/prakharporwal518-collab/leetbot-whatsapp</a>.</p>
</body></html>"""


@app.get("/privacy")
def privacy():
    """Public privacy policy page (Meta requires one before an app can be published)."""
    return PRIVACY_PAGE, 200


@app.get("/")
def home():
    return "LeetBot is running. Message it on WhatsApp!", 200


# Warn early (in the logs) if a key is missing, instead of failing mysteriously later.
for _name in ("WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "VERIFY_TOKEN", "LLM_API_KEY"):
    if not os.getenv(_name):
        log.warning("Environment variable %s is not set - check your .env", _name)


# On Render (which sets the RENDER variable), check once at start-up that Meta
# will really forward messages to this app - see whatsapp.ensure_app_subscribed.
if os.getenv("RENDER") and os.getenv("WHATSAPP_TOKEN"):
    run_in_background(whatsapp.ensure_app_subscribed)


if __name__ == "__main__":
    # Local development server. On Render, gunicorn runs `app` instead.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
