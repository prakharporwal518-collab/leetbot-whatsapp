"""Tests for llm.py - JSON parsing, prompts, and the 429 retry/fallback logic."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import openai
import pytest

import llm

GOOD_REPLY = {
    "title": "1. Two Sum (Easy)",
    "explanation": "*Idea:* hash map",
    "cpp": "class Solution {\npublic:\n    // code\n};",
    "c": "int* twoSum(int* nums, int numsSize, int target, int* returnSize) {\n}",
    "python": "class Solution:\n    pass",
}


def rate_limit_error():
    """Build the same error the openai package raises on HTTP 429."""
    # A tiny stand-in for the HTTP response object (only the parts that are used).
    response = SimpleNamespace(request=None, status_code=429, headers={"retry-after": "2"})
    return openai.RateLimitError("rate limited", response=response, body=None)


# ---- parse_reply ----

def test_parse_three_language_json():
    reply = llm.parse_reply(json.dumps(GOOD_REPLY))
    assert reply == GOOD_REPLY


def test_parse_json_wrapped_in_fence_and_chatter():
    raw = "Sure! Here you go:\n```json\n" + json.dumps(GOOD_REPLY) + "\n```\nHope it helps"
    assert llm.parse_reply(raw) == GOOD_REPLY


def test_parse_accepts_alternative_key_names_and_strips_code_fences():
    raw = json.dumps({"title": "T", "explanation": "E",
                      "C++": "```cpp\nint x;\n```", "python3": "x = 1"})
    reply = llm.parse_reply(raw)
    assert reply["cpp"] == "int x;"
    assert reply["python"] == "x = 1"
    assert reply["c"] == ""


def test_parse_allows_real_newlines_inside_strings():
    raw = '{"title": "T", "explanation": "line1\nline2", "cpp": "", "c": "", "python": ""}'
    assert llm.parse_reply(raw)["explanation"] == "line1\nline2"


@pytest.mark.parametrize("raw", [
    "", "Just some plain text answer", "{broken json", "[1, 2, 3]",
    '{"title": "only a title"}',
])
def test_parse_failure_returns_none(raw):
    assert llm.parse_reply(raw) is None


# ---- prompts ----

PROBLEM = {
    "id": "1", "title": "Two Sum", "difficulty": "Easy", "statement": "Given an array...",
    "snippets": {"cpp": "CPP_SNIPPET", "c": "C_SNIPPET", "python": "PY_SNIPPET"},
}


def test_prompt_includes_statement_and_all_signatures_by_default():
    prompt = llm.build_user_prompt("1", PROBLEM, "all")
    for expected in ("Two Sum", "Given an array...", "CPP_SNIPPET", "C_SNIPPET", "PY_SNIPPET"):
        assert expected in prompt
    assert "all three" in prompt


def test_prompt_for_single_language_only_has_that_signature():
    prompt = llm.build_user_prompt("1", PROBLEM, "c")
    assert "C_SNIPPET" in prompt
    assert "CPP_SNIPPET" not in prompt and "PY_SNIPPET" not in prompt
    assert "only C " in prompt


def test_messages_contain_system_history_and_question():
    history = [{"role": "user", "content": "two-sum"}, {"role": "assistant", "content": "answer"}]
    messages = llm.build_messages("optimize it", history)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"] == "optimize it"


# ---- retry + fallback ----

def test_chat_returns_first_success():
    with patch("llm._call_model", return_value="hello") as call:
        assert llm.chat([]) == "hello"
    assert call.call_count == 1


def test_chat_retries_twice_on_429_then_succeeds():
    with patch("llm._call_model", side_effect=[rate_limit_error(), rate_limit_error(), "ok"]) as call, \
         patch("llm.time.sleep") as sleep:
        assert llm.chat([]) == "ok"
    assert [c.args[0] for c in call.call_args_list] == ["main-model"] * 3
    assert sleep.call_count == 2
    sleep.assert_called_with(2.0)  # used the server's Retry-After header


def test_chat_uses_fallback_model_after_retries():
    errors = [rate_limit_error()] * 3
    with patch("llm._call_model", side_effect=errors + ["from fallback"]) as call, \
         patch("llm.time.sleep"):
        assert llm.chat([]) == "from fallback"
    assert [c.args[0] for c in call.call_args_list] == ["main-model"] * 3 + ["fallback-model"]


def test_chat_raises_rate_limited_when_everything_is_429():
    with patch("llm._call_model", side_effect=[rate_limit_error()] * 4) as call, \
         patch("llm.time.sleep"):
        with pytest.raises(llm.RateLimited):
            llm.chat([])
    assert call.call_count == 4  # 3 on main model + 1 on fallback, then stop


def test_chat_other_error_goes_straight_to_fallback():
    with patch("llm._call_model", side_effect=[openai.OpenAIError("boom"), "ok"]) as call:
        assert llm.chat([]) == "ok"
    assert call.call_count == 2


def test_chat_raises_llm_error_when_both_models_fail():
    with patch("llm._call_model", side_effect=openai.OpenAIError("boom")):
        with pytest.raises(llm.LLMError):
            llm.chat([])


# ---- retired models: auto-pick a living one ----

def not_found_error():
    response = SimpleNamespace(request=None, status_code=404, headers={})
    return openai.NotFoundError("model does not exist", response=response, body=None)


@pytest.fixture
def no_discovered_model():
    llm._discovered_model = None
    yield
    llm._discovered_model = None


def test_chat_picks_an_available_model_when_configured_ones_are_retired(no_discovered_model):
    listed = [SimpleNamespace(id="whisper-large-v3"), SimpleNamespace(id="new/chat-model")]
    fake_client = SimpleNamespace(models=SimpleNamespace(list=lambda: listed))
    with patch("llm._get_client", return_value=fake_client), \
         patch("llm._call_model", side_effect=[not_found_error(), not_found_error(), "ok"]) as call:
        assert llm.chat([]) == "ok"
        assert call.call_args_list[-1].args[0] == "new/chat-model"   # skipped the speech model
        # the next question goes straight to the model that worked
        call.side_effect = ["again"]
        assert llm.chat([]) == "again"
        assert call.call_args_list[-1].args[0] == "new/chat-model"


def test_chat_raises_llm_error_when_no_model_can_be_found(no_discovered_model):
    fake_client = SimpleNamespace(models=SimpleNamespace(list=lambda: []))
    with patch("llm._get_client", return_value=fake_client), \
         patch("llm._call_model", side_effect=not_found_error()):
        with pytest.raises(llm.LLMError):
            llm.chat([])
