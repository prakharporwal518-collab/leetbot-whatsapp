"""
leetcode.py - fetch real LeetCode problems.

What this file does:
  1. Looks at the user's message and decides: is it a problem NUMBER ("1", "#206"),
     a SLUG ("two-sum"), a URL (leetcode.com/problems/two-sum) or just plain TEXT?
  2. Turns a number into a slug using LeetCode's public "all problems" list
     (downloaded once and cached in memory).
  3. Downloads the problem (title, difficulty, statement, starter code) using
     LeetCode's public GraphQL API.
  4. Removes the HTML tags from the statement so the LLM gets clean text.

Everything here is free - these are the same public endpoints the LeetCode website uses.
"""

import html
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

ALL_PROBLEMS_URL = "https://leetcode.com/api/problems/all/"
GRAPHQL_URL = "https://leetcode.com/graphql"

# LeetCode blocks requests that don't look like they come from a browser,
# so we send a normal browser User-Agent and a Referer.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://leetcode.com/problemset/",
    "Content-Type": "application/json",
}
TIMEOUT = 15  # seconds to wait for LeetCode before giving up

# The GraphQL query: "give me these fields of the question with this slug".
QUESTION_QUERY = """
query getQuestion($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    questionFrontendId
    title
    difficulty
    isPaidOnly
    content
    codeSnippets { langSlug code }
  }
}
"""

# LeetCode's name for each language -> the name we use in this project.
LANG_SLUGS = {"cpp": "cpp", "c": "c", "python3": "python"}

# In-memory cache of {problem number (str) -> slug}. Refreshed once a day
# so newly added problems show up.
_slug_cache = {}
_slug_cache_time = 0.0
CACHE_SECONDS = 24 * 60 * 60

URL_PATTERN = re.compile(r"leetcode\.(?:com|cn)/problems/([a-z0-9-]+)", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"^#?\s*(\d{1,5})$")
# A problem number inside a sentence: "explain leetcode problem 75", "lc 75",
# "solve problem #206". (Plain "explain step 2" must NOT match.)
NUMBER_IN_TEXT_PATTERN = re.compile(
    r"\b(?:leetcode|lc)\s*(?:problem|question|number|no\.?)?\s*#?\s*(\d{1,5})\b"
    r"|\b(?:problem|question)\s*(?:number|no\.?)?\s*#?\s*(\d{1,5})\b",
    re.IGNORECASE)
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_input(text):
    """
    Decide what kind of message the user sent.

    Returns a pair (kind, value):
      ("url", "two-sum")      for a leetcode.com/problems/... link
      ("number", "206")       for "206" or "#206"
      ("slug", "two-sum")     for a single lowercase word-with-hyphens
      ("text", original)      for everything else (a free-form question)
    """
    text = (text or "").strip()

    url_match = URL_PATTERN.search(text)
    if url_match:
        return "url", url_match.group(1).lower()

    number_match = NUMBER_PATTERN.match(text)
    if number_match:
        return "number", str(int(number_match.group(1)))  # int() drops leading zeros

    in_text_match = NUMBER_IN_TEXT_PATTERN.search(text)
    if in_text_match:
        number = in_text_match.group(1) or in_text_match.group(2)
        return "number", str(int(number))

    # A slug has no spaces: "two-sum", "permutations".
    # Single words that are not real problems (like "thanks") simply won't be
    # found on LeetCode, and the app then treats them as normal text.
    lowered = text.lower()
    if len(lowered) >= 3 and SLUG_PATTERN.match(lowered):
        return "slug", lowered

    return "text", text


def _load_slug_map():
    """Download the list of all problems and build {number: slug}. Cached."""
    global _slug_cache, _slug_cache_time

    cache_is_fresh = time.time() - _slug_cache_time < CACHE_SECONDS
    if _slug_cache and cache_is_fresh:
        return _slug_cache

    response = requests.get(ALL_PROBLEMS_URL, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()

    mapping = {}
    for pair in data.get("stat_status_pairs", []):
        stat = pair.get("stat", {})
        number = stat.get("frontend_question_id")
        slug = stat.get("question__title_slug")
        if number is not None and slug:
            mapping[str(number)] = slug

    _slug_cache = mapping
    _slug_cache_time = time.time()
    log.info("Cached %d LeetCode problem slugs", len(mapping))
    return mapping


# Backup way to turn a number into a slug: LeetCode's search, through GraphQL.
SEARCH_QUERY = """
query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int,
                             $filters: QuestionListFilterInput) {
  problemsetQuestionList: questionList(categorySlug: $categorySlug, limit: $limit,
                                       skip: $skip, filters: $filters) {
    questions: data { frontendQuestionId: questionFrontendId titleSlug }
  }
}
"""


def _search_slug(number):
    """Ask LeetCode's search for problem `number` and return its slug (or None)."""
    payload = {"query": SEARCH_QUERY, "variables": {
        "categorySlug": "", "skip": 0, "limit": 50,
        "filters": {"searchKeywords": str(number)}}}
    response = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    result = (response.json().get("data") or {}).get("problemsetQuestionList") or {}
    for question in result.get("questions") or []:
        if str(question.get("frontendQuestionId")) == str(number):
            return question.get("titleSlug")
    return None


def number_to_slug(number):
    """'1' -> 'two-sum'. Returns None if there is no problem with that number."""
    number = str(number)
    if number in _slug_cache:
        return _slug_cache[number]
    try:
        return _load_slug_map().get(number)
    except requests.RequestException as error:
        # LeetCode sometimes refuses the big "all problems" download from cloud
        # servers (HTTP 403). Fall back to looking up just this one number.
        log.warning("All-problems list unavailable (%s); searching for #%s instead", error, number)
        slug = _search_slug(number)
        if slug:
            _slug_cache[number] = slug  # remember it for next time
        return slug


def strip_html(raw_html):
    """Turn LeetCode's HTML statement into plain readable text."""
    text = raw_html or ""
    # 10<sup>4</sup> must become 10^4, otherwise it would read as "104"!
    text = re.sub(r"<sup>(.*?)</sup>", r"^\1", text, flags=re.DOTALL)
    text = re.sub(r"<sub>(.*?)</sub>", r"_\1", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>", "- ", text)
    # Tags that end a line/paragraph become a newline.
    text = re.sub(r"<br\s*/?>|</p>|</li>|</pre>|</div>", "\n", text)
    # Every other tag is simply removed.
    text = re.sub(r"<[^>]+>", "", text)
    # &lt; -> <, &nbsp; -> space, &quot; -> " ...
    text = html.unescape(text).replace("\xa0", " ")
    # Tidy up: no trailing spaces, at most one blank line in a row.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_question(slug):
    """Ask the GraphQL API for one problem. Returns the raw dict or None."""
    payload = {"query": QUESTION_QUERY, "variables": {"titleSlug": slug}}
    response = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return (response.json().get("data") or {}).get("question")


def fetch_problem(text):
    """
    Main function of this file.

    text can be a number, a slug, a URL or anything else.
    Returns a dict describing the problem:
        {
          "id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy",
          "statement": "Given an array...",       # "" for premium problems
          "snippets": {"cpp": "...", "c": "...", "python": "..."},
          "premium": False,
          "url": "https://leetcode.com/problems/two-sum/",
        }
    Returns None when the text is not a LeetCode problem (plain text, unknown
    number, unknown slug). Network errors are raised - the caller handles them.
    """
    kind, value = parse_input(text)
    if kind == "text":
        return None

    slug = number_to_slug(value) if kind == "number" else value
    if not slug:
        return None

    question = _fetch_question(slug)
    if not question:
        return None

    # Keep only the starter code for the 3 languages we support.
    snippets = {}
    for snippet in question.get("codeSnippets") or []:
        our_name = LANG_SLUGS.get(snippet.get("langSlug"))
        if our_name:
            snippets[our_name] = snippet.get("code", "")

    return {
        "id": str(question.get("questionFrontendId", "")),
        "title": question.get("title", slug),
        "slug": slug,
        "difficulty": question.get("difficulty", ""),
        "statement": strip_html(question.get("content")),
        "snippets": snippets,
        "premium": bool(question.get("isPaidOnly")),
        "url": f"https://leetcode.com/problems/{slug}/",
    }
