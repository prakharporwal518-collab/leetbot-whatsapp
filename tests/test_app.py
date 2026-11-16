"""Tests for app.py - the webhook routes, duplicate handling and reply logic."""
import json
from unittest.mock import patch

import pytest

import app as leetbot
import llm

LLM_JSON = json.dumps({
    "title": "Two Sum", "explanation": "*Idea:* hash map",
    "cpp": "// cpp", "c": "// c", "python": "# python",
})


def text_payload(message_id="wamid.1", body="two-sum", sender="919999"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": message_id, "type": "text", "text": {"body": body}}]}}]}]}


@pytest.fixture
def client():
    """A fake browser for our Flask app, with clean memory and no real threads."""
    leetbot._seen_ids.clear()
    leetbot.user_lang.clear()
    leetbot.user_history.clear()
    # Run "background" work immediately so tests can check the result.
    with patch.object(leetbot, "run_in_background", side_effect=lambda f, *a: f(*a)):
        yield leetbot.app.test_client()


@pytest.fixture
def sent():
    """Capture outgoing WhatsApp messages instead of sending them."""
    messages = []
    with patch("whatsapp.send_text", side_effect=lambda to, body: messages.append((to, body)) or True):
        yield messages


# ---- GET /whatsapp (verification) and /health ----

def test_verification_returns_challenge(client):
    response = client.get("/whatsapp?hub.mode=subscribe&hub.verify_token=my-verify-token&hub.challenge=777")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "777"


def test_verification_wrong_token_is_403(client):
    response = client.get("/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=777")
    assert response.status_code == 403


def test_health(client):
    response = client.get("/health")
    assert (response.status_code, response.get_data(as_text=True)) == (200, "ok")


# ---- POST /whatsapp ----

def test_duplicate_message_is_answered_only_once(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", return_value=LLM_JSON) as chat:
        first = client.post("/whatsapp", json=text_payload("wamid.A"))
        retry = client.post("/whatsapp", json=text_payload("wamid.A"))   # Meta's retry
        other = client.post("/whatsapp", json=text_payload("wamid.B"))   # a new message
    assert first.status_code == retry.status_code == other.status_code == 200
    assert chat.call_count == 2
    assert len(sent) == 2


def test_only_last_500_message_ids_are_remembered():
    leetbot._seen_ids.clear()
    for number in range(600):
        assert leetbot.is_duplicate(f"id-{number}") is False
    assert len(leetbot._seen_ids) == 500
    assert leetbot.is_duplicate("id-599") is True    # recent: still remembered
    assert leetbot.is_duplicate("id-0") is False     # very old: forgotten


def test_status_update_is_ignored(client, sent):
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}
    assert client.post("/whatsapp", json=payload).status_code == 200
    assert sent == []


def test_non_text_message_gets_text_only_reply(client, sent):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919999", "id": "wamid.img", "type": "image", "image": {"id": "1"}}]}}]}]}
    assert client.post("/whatsapp", json=payload).status_code == 200
    assert sent == [("919999", "Please send text only")]


def test_garbage_body_never_crashes(client, sent):
    assert client.post("/whatsapp", data="not json").status_code == 200
    assert sent == []


def test_webhook_returns_before_the_work_is_done():
    """The real run_in_background must not wait for the slow work."""
    import threading
    release = threading.Event()
    leetbot._seen_ids.clear()
    with patch.object(leetbot, "handle_message", side_effect=lambda *a: release.wait(5)):
        response = leetbot.app.test_client().post("/whatsapp", json=text_payload("wamid.slow"))
        assert response.status_code == 200  # got here while the "work" is still blocked
        release.set()


# ---- reply logic ----

def test_hi_sends_one_help_message_without_calling_llm(client, sent):
    with patch("llm.chat") as chat:
        client.post("/whatsapp", json=text_payload(body="Hi!"))
    chat.assert_not_called()
    assert len(sent) == 1 and "LeetBot" in sent[0][1]


def test_default_answer_has_all_three_languages_in_one_message(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), patch("llm.chat", return_value=LLM_JSON):
        client.post("/whatsapp", json=text_payload(body="print a hollow diamond of stars"))
    assert len(sent) == 1  # no "solving..." message, no greeting - just the answer
    body = sent[0][1]
    assert "*C++*" in body and "*C*" in body and "*Python*" in body


def test_leetcode_problem_uses_real_title(client, sent):
    problem = {"id": "1", "title": "Two Sum", "difficulty": "Easy", "statement": "s",
               "snippets": {}, "slug": "two-sum"}
    with patch("leetcode.fetch_problem", return_value=problem), patch("llm.chat", return_value=LLM_JSON):
        client.post("/whatsapp", json=text_payload(body="1"))
    assert sent[0][1].startswith("*1. Two Sum (Easy)*")


def test_lang_command_changes_what_the_llm_is_asked(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", return_value=LLM_JSON) as chat:
        client.post("/whatsapp", json=text_payload("m1", "lang cpp"))
        client.post("/whatsapp", json=text_payload("m2", "print a star pyramid"))
    assert "C++ only" in sent[0][1]
    assert chat.call_count == 1  # the lang command itself costs no LLM call
    prompt = chat.call_args.args[0][-1]["content"]
    assert "only C++" in prompt


def test_unknown_lang_shows_usage(client, sent):
    client.post("/whatsapp", json=text_payload(body="lang java"))
    assert "lang cpp" in sent[0][1]


def test_follow_up_sends_history_and_keeps_only_last_6(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", return_value=LLM_JSON) as chat:
        for number in range(5):
            client.post("/whatsapp", json=text_payload(f"m{number}", f"question number {number}"))
    messages = chat.call_args.args[0]
    # system + 6 remembered messages + the new question
    assert len(messages) == 8
    assert messages[1]["content"] == "question number 1"   # question 0 was forgotten
    assert messages[-2]["role"] == "assistant"
    assert len(leetbot.user_history["919999"]) == 6


def test_invalid_json_falls_back_to_raw_text(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", return_value="Plain answer, **not** JSON"):
        client.post("/whatsapp", json=text_payload(body="what is a heap in dsa"))
    assert sent == [("919999", "Plain answer, *not* JSON")]


def test_rate_limit_sends_one_message(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", side_effect=llm.RateLimited()):
        client.post("/whatsapp", json=text_payload(body="what is a heap in dsa"))
    assert sent == [("919999", "Too many requests, try again in a minute.")]


def test_any_crash_sends_friendly_error(client, sent):
    with patch("leetcode.fetch_problem", return_value=None), \
         patch("llm.chat", side_effect=RuntimeError("boom")):
        response = client.post("/whatsapp", json=text_payload(body="what is a heap in dsa"))
    assert response.status_code == 200
    assert len(sent) == 1 and "went wrong" in sent[0][1]


def test_leetcode_being_down_still_answers(client, sent):
    with patch("leetcode.fetch_problem", side_effect=ConnectionError("blocked")), \
         patch("llm.chat", return_value=LLM_JSON) as chat:
        client.post("/whatsapp", json=text_payload(body="#206"))
    assert "LeetCode problem '206'" in chat.call_args.args[0][-1]["content"]
    assert len(sent) == 1
