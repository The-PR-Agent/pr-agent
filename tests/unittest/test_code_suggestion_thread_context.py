# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared code-suggestion thread context block.

`GitProvider.get_code_suggestion_thread_context()` is the single place that bounds and
serializes the prior suggestion threads injected into the /improve prompt. Providers supply
`_iter_code_suggestion_threads()`; the shared code owns the limits, the marker cleanup and
the budget, so the block is measured on the exact string a provider returns.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

from pr_agent.git_providers.git_provider import (
    _DEFAULT_DISCUSSION_CONTEXT_CHARS,
    _MAX_DISCUSSION_MESSAGE_CHARS,
    _MAX_DISCUSSION_REPLIES,
    _MAX_DISCUSSION_THREADS,
    GitProvider,
)

SUGGESTION = "**Suggestion:** guard the value\n```suggestion\nsafe()\n```"


class _StubProvider(GitProvider):
    """A provider whose only thread source is the shared iterator hook."""

    def __init__(self, threads=(), budget=_DEFAULT_DISCUSSION_CONTEXT_CHARS):
        self._threads = list(threads)
        self._budget = budget
        self.iterated = False

    def _iter_code_suggestion_threads(self):
        self.iterated = True
        return iter(self._threads)

    def is_supported(self, capability: str) -> bool:
        return False

    def get_files(self) -> list:
        return []

    def get_diff_files(self) -> list:
        return []

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return False

    def get_languages(self):
        return {}

    def get_pr_branch(self):
        return ""

    def get_user_id(self):
        return ""

    def get_pr_description_full(self) -> str:
        return ""

    def get_repo_settings(self):
        return b""

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(
        self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None
    ):
        pass

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return False

    def get_commit_messages(self) -> str:
        return ""


@contextmanager
def _configured_budget(value):
    with patch("pr_agent.git_providers.git_provider.get_settings") as settings:
        settings.return_value.pr_code_suggestions.get.side_effect = lambda key, default=None: (
            value if key == "max_discussion_context_chars" else default
        )
        yield


def _thread(thread_id="d1", suggestion=SUGGESTION, replies=()):
    return {
        "thread_id": thread_id,
        "status": "open",
        "file": "src/app.py",
        "start_line": 4,
        "end_line": 4,
        "suggestion": suggestion,
        "replies": list(replies),
    }


def _reply(index, message=None):
    return {"author": f"Author {index}", "message": message or f"Reply {index}"}


def _rendered(threads, indent=2):
    return json.dumps(
        [GitProvider._code_suggestion_context_thread(thread) for thread in threads],
        ensure_ascii=False,
        indent=indent,
    )


def test_base_provider_reports_no_threads():
    provider = _StubProvider()

    assert provider.get_code_suggestion_thread_context() == ""


def test_budget_is_measured_on_the_returned_indented_json():
    """The block is sized as returned; a compact-only check would let a thread overrun it."""
    threads = [_thread(f"d{index}", suggestion="**Suggestion:** use value") for index in range(10)]
    budget = len(_rendered(threads[:5]))
    # The compact form of six threads is what Azure DevOps used to measure, and it fits the budget.
    assert len(json.dumps(
        [GitProvider._code_suggestion_context_thread(thread) for thread in threads[:6]], ensure_ascii=False
    )) <= budget

    provider = _StubProvider(threads)
    with _configured_budget(budget):
        result = provider.get_code_suggestion_thread_context()

    assert len(result) <= budget
    assert len(json.loads(result)) == 5


def test_zero_budget_disables_the_block_without_reading_threads():
    provider = _StubProvider([_thread()])

    with _configured_budget(0):
        assert provider.get_code_suggestion_thread_context() == ""

    assert provider.iterated is False


def test_non_numeric_budget_falls_back_to_the_default():
    provider = _StubProvider([_thread()])

    with _configured_budget("not-a-number"):
        result = provider.get_code_suggestion_thread_context()

    assert len(result) <= _DEFAULT_DISCUSSION_CONTEXT_CHARS
    assert json.loads(result)[0]["thread_id"] == "d1"


def test_thread_count_is_capped():
    provider = _StubProvider([_thread(f"d{index}") for index in range(80)])

    assert len(json.loads(provider.get_code_suggestion_thread_context())) == _MAX_DISCUSSION_THREADS


def test_replies_are_capped_to_the_last_ones():
    provider = _StubProvider([_thread(replies=[_reply(index) for index in range(25)])])

    replies = json.loads(provider.get_code_suggestion_thread_context())[0]["replies"]

    assert len(replies) == _MAX_DISCUSSION_REPLIES
    assert replies[0]["author"] == "Author 15"
    assert replies[-1]["author"] == "Author 24"


def test_blank_replies_are_dropped():
    provider = _StubProvider([_thread(replies=[_reply(0), {"author": "A", "message": "  "}, _reply(1)])])

    replies = json.loads(provider.get_code_suggestion_thread_context())[0]["replies"]

    assert replies == [_reply(0), _reply(1)]


def test_long_messages_are_truncated():
    provider = _StubProvider([_thread(suggestion="x" * 2000, replies=[_reply(0, "y" * 2000)])])

    discussion = json.loads(provider.get_code_suggestion_thread_context())[0]

    assert len(discussion["suggestion"]) == _MAX_DISCUSSION_MESSAGE_CHARS
    assert len(discussion["replies"][0]["message"]) == _MAX_DISCUSSION_MESSAGE_CHARS


def test_only_trailing_markers_are_stripped():
    """Quoted marker syntax inside a suggestion is kept; the appended marker lines are not."""
    body = (
        "**Suggestion:** drop the marker\n```suggestion\n"
        'body = "<!-- pr-agent-dedup: aabbccddeeff -->"\n```\n\n'
        "<!-- pr-agent-dedup: aabbccddeeff -->\n"
        "[pr-agent-dedup-code: 112233445566]: https://github.com/The-PR-Agent/pr-agent"
    )
    provider = _StubProvider([_thread(suggestion=body)])

    discussion = json.loads(provider.get_code_suggestion_thread_context())[0]

    assert discussion["suggestion"] == (
        "**Suggestion:** drop the marker\n```suggestion\n"
        'body = "<!-- pr-agent-dedup: aabbccddeeff -->"\n```'
    )


def test_missing_fields_do_not_break_the_block():
    provider = _StubProvider([{"thread_id": "d1", "suggestion": SUGGESTION}])

    discussion = json.loads(provider.get_code_suggestion_thread_context())[0]

    assert discussion == {
        "thread_id": "d1",
        "status": None,
        "file": None,
        "start_line": None,
        "end_line": None,
        "suggestion": SUGGESTION,
        "replies": [],
    }
