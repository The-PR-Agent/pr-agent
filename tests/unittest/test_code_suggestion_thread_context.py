import json

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    DEFAULT_DISCUSSION_CONTEXT_CHARS,
    CodeSuggestionThread,
    GitProvider,
)
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_BUDGET_KEY = "pr_code_suggestions.max_discussion_context_chars"
_SUGGESTION = "**Suggestion:** Rename this [best practice, importance: 5]\n```suggestion\nx = 1\n```"


class _ThreadProvider(GitProvider):
    def __init__(self, threads):
        self.threads = threads
        self.iterated = False

    def _iter_code_suggestion_threads(self):
        self.iterated = True
        yield from self.threads


_ThreadProvider.__abstractmethods__ = frozenset()


def _thread(thread_id=1, suggestion=_SUGGESTION, replies=None, authored_by_agent=None):
    return CodeSuggestionThread(
        thread_id=thread_id,
        status="open",
        file="src/app.py",
        start_line=12,
        end_line=14,
        suggestion=suggestion,
        replies=replies or [],
        authored_by_agent=authored_by_agent,
    )


@pytest.fixture(autouse=True)
def _restore_budget():
    snapshot = snapshot_settings((_BUDGET_KEY,))
    yield
    restore_settings(snapshot)


def test_serializes_threads_in_provider_order():
    provider = _ThreadProvider([
        _thread(thread_id="new", replies=[("Alice", "  Not needed.  ")]),
        _thread(thread_id="old"),
    ])

    assert json.loads(provider.get_code_suggestion_thread_context()) == [
        {
            "thread_id": "new",
            "status": "open",
            "file": "src/app.py",
            "start_line": 12,
            "end_line": 14,
            "suggestion": _SUGGESTION,
            "replies": [{"author": "Alice", "message": "Not needed."}],
        },
        {
            "thread_id": "old",
            "status": "open",
            "file": "src/app.py",
            "start_line": 12,
            "end_line": 14,
            "suggestion": _SUGGESTION,
            "replies": [],
        },
    ]


def test_base_provider_has_no_context():
    assert _ThreadProvider([]).get_code_suggestion_thread_context() == ""


def test_budget_is_checked_on_the_returned_text():
    threads = [_thread(thread_id=i, suggestion=_SUGGESTION + "x" * 300) for i in range(5)]
    entry = json.loads(_ThreadProvider(threads[:1]).get_code_suggestion_thread_context())[0]
    # Two threads fit when measured as compact JSON, but not in the indented form that is returned.
    budget = len(json.dumps([entry, entry], ensure_ascii=False)) + 1
    get_settings().set(_BUDGET_KEY, budget)

    result = _ThreadProvider(threads).get_code_suggestion_thread_context()

    assert len(result) <= budget
    assert len(json.loads(result)) == 1


def test_zero_budget_disables_context_without_reading_threads():
    get_settings().set(_BUDGET_KEY, 0)
    provider = _ThreadProvider([_thread()])

    assert provider.get_code_suggestion_thread_context() == ""
    assert provider.iterated is False


def test_invalid_budget_falls_back_to_default():
    get_settings().set(_BUDGET_KEY, "not-a-number")
    threads = [_thread(thread_id=i, suggestion="**Suggestion:** " + "x" * 740) for i in range(60)]

    result = _ThreadProvider(threads).get_code_suggestion_thread_context()

    assert 0 < len(result) <= DEFAULT_DISCUSSION_CONTEXT_CHARS


def test_keeps_suggestion_text_that_quotes_a_pr_agent_comment():
    quoted = "**Suggestion:** Keep the marker\n```suggestion\nMARKER = \"<!-- pr-agent-response -->\"\n```"
    body = (quoted + "\n\n<!-- pr-agent-dedup: aabbccddeeff -->\n"
            "[pr-agent-dedup-code: 112233445566]: https://github.com/The-PR-Agent/pr-agent")

    discussions = json.loads(_ThreadProvider([_thread(suggestion=body)]).get_code_suggestion_thread_context())

    assert discussions[0]["suggestion"] == quoted


def test_skips_threads_opened_by_someone_else():
    provider = _ThreadProvider([
        _thread(thread_id="human", authored_by_agent=False),
        _thread(thread_id="bot", authored_by_agent=True),
        _thread(thread_id="unknown", authored_by_agent=None),
    ])

    discussions = json.loads(provider.get_code_suggestion_thread_context())

    assert [discussion["thread_id"] for discussion in discussions] == ["bot", "unknown"]


def test_caps_replies_after_dropping_empty_messages():
    replies = [("Alice", f"reply {i}") for i in range(12)] + [("Bob", "   "), ("Bob", "")]

    discussions = json.loads(_ThreadProvider([_thread(replies=replies)]).get_code_suggestion_thread_context())

    assert [reply["message"] for reply in discussions[0]["replies"]] == [f"reply {i}" for i in range(2, 12)]


def test_unknown_reply_author_is_labelled():
    discussions = json.loads(
        _ThreadProvider([_thread(replies=[(None, "No.")])]).get_code_suggestion_thread_context()
    )

    assert discussions[0]["replies"] == [{"author": "Unknown", "message": "No."}]
