"""Tests for whatsapp.py - webhook verification, parsing and the message packer."""
from unittest.mock import MagicMock, patch

import whatsapp


def make_code(lines, width=40, name="line"):
    """Fake code: `lines` lines, each `width` characters long."""
    return "\n".join(f"{name}{i:04d};".ljust(width, "x") for i in range(lines))


def fences_balanced(message):
    """Every ``` that opens in a message must also close in that message."""
    return message.count("```") % 2 == 0


# ---- webhook verification ----

def test_verify_ok():
    assert whatsapp.verify_webhook("subscribe", "my-verify-token", "abc123") == ("abc123", 200)


def test_verify_wrong_token():
    assert whatsapp.verify_webhook("subscribe", "wrong", "abc123")[1] == 403


def test_verify_missing_token():
    assert whatsapp.verify_webhook("subscribe", None, "abc123")[1] == 403


# ---- parse_incoming ----

def test_parse_text_message():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919999", "id": "wamid.1", "type": "text", "text": {"body": "two-sum"}}]}}]}]}
    assert whatsapp.parse_incoming(payload) == {
        "from": "919999", "id": "wamid.1", "type": "text", "text": "two-sum"}


def test_parse_status_update_is_ignored():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    assert whatsapp.parse_incoming(payload) is None


def test_parse_garbage_is_ignored():
    assert whatsapp.parse_incoming({}) is None
    assert whatsapp.parse_incoming({"entry": []}) is None


# ---- packer ----

def test_small_answer_is_one_message():
    blocks = whatsapp.build_blocks(
        "1. Two Sum (Easy)", "*Idea:* use a hash map.",
        {"cpp": make_code(15), "c": make_code(20), "python": make_code(8)})
    messages = whatsapp.pack_messages(blocks)
    assert len(messages) == 1
    text = messages[0]
    assert text.startswith("*1. Two Sum (Easy)*")
    # all three languages, in the order C++, C, Python
    assert text.index("*C++*") < text.index("*C*") < text.index("*Python*")
    assert text.count("```") == 6


def test_empty_languages_are_skipped():
    blocks = whatsapp.build_blocks("T", "E", {"cpp": "int x;", "c": "", "python": "  "})
    text = "\n".join(whatsapp.pack_messages(blocks))
    assert "*C++*" in text and "*C*" not in text and "*Python*" not in text


def test_code_blocks_are_never_split_when_they_fit():
    # explanation 1000 chars + three code blocks of ~2500 chars each
    codes = {"cpp": make_code(60, name="cpp"), "c": make_code(61, name="c"),
             "python": make_code(62, name="py")}
    blocks = whatsapp.build_blocks("Title", "e" * 1000, codes)
    messages = whatsapp.pack_messages(blocks)

    assert all(len(m) <= whatsapp.HARD_LIMIT for m in messages)
    assert all(fences_balanced(m) for m in messages)
    # each code block appears complete inside exactly one message
    for code in codes.values():
        assert sum(code in m for m in messages) == 1
    # ~8600 chars can't fit in 2 messages of 4000; a whole-block packing needs 3
    assert len(messages) == 3


def test_packs_into_minimum_number_of_messages():
    # 800 + 1500 + 1500 + 3000 characters: the first three share one
    # message (3800 <= 4000), the last gets its own -> 2 messages, not 4.
    blocks = [
        {"kind": "text", "body": "e" * 800},
        {"kind": "code", "header": "*C++*", "body": make_code(36)},
        {"kind": "code", "header": "*C*", "body": make_code(36)},
        {"kind": "code", "header": "*Python*", "body": make_code(73)},
    ]
    messages = whatsapp.pack_messages(blocks)
    assert len(messages) == 2
    assert all(len(m) <= 4000 for m in messages)


def test_huge_code_block_is_split_with_fences_closed_and_reopened():
    code = make_code(250)  # ~10,000 characters: can't fit in one message
    blocks = whatsapp.build_blocks("", "", {"cpp": code})
    messages = whatsapp.pack_messages(blocks)

    assert len(messages) == 3  # the minimum possible for 10,000 chars
    assert all(len(m) <= whatsapp.HARD_LIMIT for m in messages)
    assert all(fences_balanced(m) and m.count("```") == 2 for m in messages)
    assert "_(continued)_" in messages[1]
    # put the pieces back together: no line of code was lost or cut in half
    rebuilt = []
    for message in messages:
        inside = message.split("```")[1].strip("\n")
        rebuilt.extend(inside.split("\n"))
    assert rebuilt == code.split("\n")


def test_very_long_single_line_still_fits_limit():
    blocks = [{"kind": "text", "body": "x" * 9000}]
    messages = whatsapp.pack_messages(blocks)
    assert all(len(m) <= 4000 for m in messages)
    assert "".join(messages) == "x" * 9000


def test_markdown_is_converted_to_whatsapp_style():
    assert whatsapp.to_whatsapp_format("## Idea\n**bold** text") == "*Idea*\n*bold* text"


# ---- sending ----

def test_send_text_calls_graph_api_and_counts():
    ok = MagicMock(status_code=200)
    before = whatsapp.messages_sent_this_month()
    with patch("whatsapp.requests.post", return_value=ok) as post:
        assert whatsapp.send_text("919999", "hello") is True
    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://graph.facebook.com/v21.0/12345/messages"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["json"]["messaging_product"] == "whatsapp"
    assert kwargs["json"]["type"] == "text"
    assert kwargs["json"]["text"]["body"] == "hello"
    assert whatsapp.messages_sent_this_month() == before + 1


def test_send_text_failure_returns_false_and_is_not_counted():
    bad = MagicMock(status_code=401, text="bad token")
    before = whatsapp.messages_sent_this_month()
    with patch("whatsapp.requests.post", return_value=bad):
        assert whatsapp.send_text("919999", "hello") is False
    assert whatsapp.messages_sent_this_month() == before


def test_verify_ignores_stray_whitespace_from_copy_paste():
    assert whatsapp.verify_webhook("subscribe", " my-verify-token\n", "abc")[1] == 200


def test_ensure_app_subscribed_subscribes_the_account_that_owns_our_number():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url.split("/v21.0/")[1]))
        answers = {
            ("GET", "debug_token"): {"data": {"granular_scopes": [
                {"scope": "whatsapp_business_management", "target_ids": ["111", "222"]}]}},
            ("GET", "111/phone_numbers"): {"data": [{"id": "99999"}]},
            ("GET", "222/phone_numbers"): {"data": [{"id": "12345"}]},   # our PHONE_NUMBER_ID
            ("GET", "222/subscribed_apps"): {"data": []},
            ("POST", "222/subscribed_apps"): {"success": True},
        }
        return MagicMock(json=lambda: answers[calls[-1]])

    with patch("whatsapp.requests.request", side_effect=fake_request):
        whatsapp.ensure_app_subscribed()
    assert ("POST", "222/subscribed_apps") in calls
    assert ("POST", "111/subscribed_apps") not in calls


def test_ensure_app_subscribed_never_raises():
    with patch("whatsapp.requests.request", side_effect=ConnectionError("down")):
        whatsapp.ensure_app_subscribed()
