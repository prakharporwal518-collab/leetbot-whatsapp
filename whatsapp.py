"""
whatsapp.py - everything that talks to the Meta WhatsApp Cloud API.

What this file does:
  1. verify_webhook()  - answers Meta's one-time "is this really your server?" check.
  2. parse_incoming()  - pulls the sender, message id and text out of Meta's big JSON.
  3. build_blocks() + pack_messages() - pack the explanation and code into
     AS FEW WhatsApp messages as possible (to save the free message quota).
  4. send_text() / send_messages() - actually send the replies, and count them.
"""

import logging
import os
import re
import threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()  # read the .env file into environment variables
log = logging.getLogger(__name__)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")

GRAPH_API_VERSION = "v21.0"

HARD_LIMIT = 4096   # WhatsApp rejects text messages longer than this
PACK_LIMIT = 4000   # we fill each message up to here (small safety margin)
MONTHLY_FREE_MESSAGES = 1000

FENCE = "```"       # WhatsApp shows text between ``` and ``` in monospace
CODE_LABELS = {"cpp": "C++", "c": "C", "python": "Python"}  # also fixes the order


# ---------------------------------------------------------------------------
# 1. Webhook verification
# ---------------------------------------------------------------------------

def verify_webhook(mode, token, challenge):
    """
    When you save the webhook URL in the Meta dashboard, Meta sends a GET with
    hub.mode, hub.verify_token and hub.challenge. If the token matches ours we
    must echo the challenge back; otherwise we refuse with 403.

    Returns (body, http_status).
    """
    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403


# ---------------------------------------------------------------------------
# 2. Reading incoming webhooks
# ---------------------------------------------------------------------------

def parse_incoming(payload):
    """
    Meta's webhook JSON looks like:
        {"entry": [{"changes": [{"value": {"messages": [{"from": "9198...",
            "id": "wamid.XXX", "type": "text", "text": {"body": "two-sum"}}]}}]}]}

    Returns {"from", "id", "type", "text"} for a real message, or None for
    anything else (delivery/read status updates, malformed data...).
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]  # status updates have no "messages" key
        return {
            "from": message["from"],
            "id": message["id"],
            "type": message.get("type", ""),
            "text": (message.get("text") or {}).get("body", ""),
        }
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 3. Formatting + packing replies into as few messages as possible
# ---------------------------------------------------------------------------

def to_whatsapp_format(text):
    """LLMs love Markdown. Convert the common bits to WhatsApp style."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text or "")                 # **bold** -> *bold*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)  # "## Title" -> *Title*
    return text.strip()


def build_blocks(title, explanation, codes):
    """
    Turn a parsed LLM reply into an ordered list of "blocks".
    A block is one unit that we try NOT to split across messages:
        {"kind": "text", "body": "..."}
        {"kind": "code", "header": "*C++*", "body": "int main() {...}"}

    codes is a dict like {"cpp": "...", "c": "...", "python": "..."}.
    Languages with empty code are skipped.
    """
    blocks = []

    intro = ""
    if title:
        intro = f"*{title.strip().strip('*')}*"
    if explanation:
        intro = (intro + "\n\n" if intro else "") + to_whatsapp_format(explanation)
    if intro:
        blocks.append({"kind": "text", "body": intro})

    for lang, label in CODE_LABELS.items():
        code = (codes.get(lang) or "").strip("\n")
        if code.strip():
            blocks.append({"kind": "code", "header": f"*{label}*", "body": code})
    return blocks


def _render_code(header, body):
    """A code block as it appears in WhatsApp: a bold label, then monospace code."""
    return f"{header}\n{FENCE}\n{body}\n{FENCE}"


def _split_lines(text, max_len):
    """
    Split text into chunks of at most max_len characters, cutting only at line
    breaks. (A single line longer than max_len is cut by characters - rare.)
    """
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > max_len:           # monster line: hard cut
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= max_len:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def _block_to_pieces(block, limit):
    """
    Render one block into a list of strings, each at most `limit` characters.
    Normally that's ONE piece. Only a block that is too big for a whole message
    gets split - and for code we close the ``` and reopen it in the next piece,
    so every message still shows proper monospace code.
    """
    if block["kind"] == "text":
        body = block["body"].strip()
        if not body:
            return []
        return [body] if len(body) <= limit else _split_lines(body, limit)

    header, body = block["header"], block["body"]
    whole = _render_code(header, body)
    if len(whole) <= limit:
        return [whole]

    continued_header = f"{header} _(continued)_"
    # Space left for code after the header and the two fences.
    room = limit - len(_render_code(continued_header, ""))
    chunks = _split_lines(body, room)
    return [
        _render_code(header if index == 0 else continued_header, chunk)
        for index, chunk in enumerate(chunks)
    ]


def pack_messages(blocks, limit=PACK_LIMIT):
    """
    Pack blocks into as few WhatsApp messages as possible, keeping their order.

    Greedy idea (same as packing boxes into a truck one by one): keep adding
    pieces to the current message while they fit; when the next piece doesn't
    fit, start a new message. For items that must stay in order, this greedy
    approach gives the minimum number of messages.
    """
    pieces = []
    for block in blocks:
        pieces.extend(_block_to_pieces(block, limit))

    separator = "\n\n"
    messages, current = [], ""
    for piece in pieces:
        candidate = piece if not current else current + separator + piece
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = piece
    if current:
        messages.append(current)
    return messages


# ---------------------------------------------------------------------------
# 4. Sending + the monthly message counter
# ---------------------------------------------------------------------------

_counter = {"month": "", "count": 0}
_counter_lock = threading.Lock()  # several background threads may send at once


def _count_sent_message():
    """Add 1 to this month's counter (it resets when a new month starts)."""
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _counter_lock:
        if _counter["month"] != this_month:
            _counter["month"] = this_month
            _counter["count"] = 0
        _counter["count"] += 1
        return _counter["count"]


def messages_sent_this_month():
    """How many messages the bot has sent this month (since the last restart)."""
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _counter_lock:
        return _counter["count"] if _counter["month"] == this_month else 0


def send_text(to, body):
    """
    Send ONE text message through the WhatsApp Cloud API.
    Returns True on success, False on failure (never raises).
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        # preview_url False = don't build link previews for URLs inside code.
        "text": {"body": body[:HARD_LIMIT], "preview_url": False},
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
    except requests.RequestException as error:
        log.error("Could not reach WhatsApp API: %s", error)
        return False

    if response.status_code >= 400:
        log.error("WhatsApp API error %s: %s", response.status_code, response.text[:500])
        return False

    count = _count_sent_message()
    log.info("Messages sent this month: %d / %d", count, MONTHLY_FREE_MESSAGES)
    if count >= MONTHLY_FREE_MESSAGES * 0.9:
        log.warning("Close to the monthly free message limit!")
    return True


def send_messages(to, messages):
    """Send several messages in order. Returns how many were sent successfully."""
    return sum(1 for message in messages if send_text(to, message))
