"""Tests for leetcode.py - LeetCode's servers are replaced by fakes (mocks)."""
from unittest.mock import MagicMock, patch

import pytest

import leetcode

FAKE_ALL_PROBLEMS = {
    "stat_status_pairs": [
        {"stat": {"frontend_question_id": 1, "question__title_slug": "two-sum"}},
        {"stat": {"frontend_question_id": 206, "question__title_slug": "reverse-linked-list"}},
    ]
}

FAKE_QUESTIONS = {
    "two-sum": {
        "questionFrontendId": "1",
        "title": "Two Sum",
        "difficulty": "Easy",
        "isPaidOnly": False,
        "content": "<p>Given an array <code>nums</code> where "
                   "<code>2 &lt;= nums.length &lt;= 10<sup>4</sup></code>.</p>"
                   "<ul><li>Return&nbsp;indices</li></ul>",
        "codeSnippets": [
            {"langSlug": "cpp", "code": "class Solution { vector<int> twoSum(); };"},
            {"langSlug": "java", "code": "class Solution {}"},
            {"langSlug": "c", "code": "int* twoSum(int* nums, int numsSize, int target, int* returnSize) {}"},
            {"langSlug": "python3", "code": "class Solution:\n    def twoSum(self): pass"},
        ],
    },
    "reverse-linked-list": {
        "questionFrontendId": "206", "title": "Reverse Linked List",
        "difficulty": "Easy", "isPaidOnly": False, "content": "<p>Reverse it.</p>",
        "codeSnippets": [],
    },
}


def fake_get(url, **kwargs):
    response = MagicMock()
    response.json.return_value = FAKE_ALL_PROBLEMS
    return response


def fake_post(url, json=None, **kwargs):
    slug = json["variables"]["titleSlug"]
    response = MagicMock()
    response.json.return_value = {"data": {"question": FAKE_QUESTIONS.get(slug)}}
    return response


@pytest.fixture(autouse=True)
def fake_leetcode():
    """Every test starts with an empty cache and fake network functions."""
    leetcode._slug_cache = {}
    leetcode._slug_cache_time = 0.0
    with patch("leetcode.requests.get", side_effect=fake_get) as get, \
         patch("leetcode.requests.post", side_effect=fake_post) as post:
        yield get, post


# ---- parse_input ----

@pytest.mark.parametrize("text, expected", [
    ("1", ("number", "1")),
    ("#206", ("number", "206")),
    ("  # 15 ", ("number", "15")),
    ("two-sum", ("slug", "two-sum")),
    ("Two-Sum", ("slug", "two-sum")),
    ("https://leetcode.com/problems/two-sum/description/", ("url", "two-sum")),
    ("solve leetcode.com/problems/3sum please", ("url", "3sum")),
    ("print a hollow diamond of stars", ("text", "print a hollow diamond of stars")),
    ("explain step 2", ("text", "explain step 2")),
    ("expalin leetcode problem 75", ("number", "75")),
    ("LC 206 in python please", ("number", "206")),
    ("solve problem #15", ("number", "15")),
    ("print numbers from 1 to 10", ("text", "print numbers from 1 to 10")),
])
def test_parse_input(text, expected):
    assert leetcode.parse_input(text) == expected


# ---- fetch_problem ----

def test_fetch_by_number():
    problem = leetcode.fetch_problem("1")
    assert problem["id"] == "1"
    assert problem["title"] == "Two Sum"
    assert problem["slug"] == "two-sum"
    assert problem["difficulty"] == "Easy"
    assert problem["url"] == "https://leetcode.com/problems/two-sum/"


def test_fetch_by_hash_number():
    assert leetcode.fetch_problem("#206")["title"] == "Reverse Linked List"


def test_fetch_by_slug():
    assert leetcode.fetch_problem("two-sum")["id"] == "1"


def test_fetch_by_url():
    problem = leetcode.fetch_problem("https://leetcode.com/problems/two-sum/description/")
    assert problem["title"] == "Two Sum"


def test_plain_text_returns_none_without_network(fake_leetcode):
    get, post = fake_leetcode
    assert leetcode.fetch_problem("print a hollow diamond of stars") is None
    get.assert_not_called()
    post.assert_not_called()


def test_unknown_number_and_unknown_slug_return_none():
    assert leetcode.fetch_problem("99999") is None
    assert leetcode.fetch_problem("thanks") is None


def test_snippets_only_keep_cpp_c_python():
    snippets = leetcode.fetch_problem("1")["snippets"]
    assert set(snippets) == {"cpp", "c", "python"}
    assert "returnSize" in snippets["c"]
    assert "def twoSum" in snippets["python"]


def test_html_is_stripped():
    statement = leetcode.fetch_problem("1")["statement"]
    assert "<" not in statement.replace("<=", "")   # no tags left
    assert "2 <= nums.length <= 10^4" in statement  # entities + <sup> handled
    assert "- Return indices" in statement


def test_number_map_is_cached(fake_leetcode):
    get, _ = fake_leetcode
    leetcode.fetch_problem("1")
    leetcode.fetch_problem("206")
    assert get.call_count == 1  # downloaded the big list only once
