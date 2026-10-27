"""Regression tests for slash command tokenization in ``PRAgent._run_command``.

``pr_agent/agent/pr_agent.py`` splits a string request with ``shlex`` before
dispatching to a tool. ``shlex`` defaults ``commenters`` to ``"#"``, so it treated
the hash as a shell comment and discarded the rest of the line. On a code review
bot, ``#123`` style references are among the most common things to type in a
question, and the tool silently received a truncated one with no error raised.

``_run_command`` is the single parser behind every entry point that passes a
string: the webhook servers, the Gerrit plugin and the CLI.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.agent.pr_agent import PRAgent

from ._settings_helpers import restore_settings, snapshot_settings

PR_URL = "https://github.com/org/repo/pull/1"


@pytest.fixture(autouse=True)
def _isolate_settings():
    """Restore the Dynaconf leaves ``update_settings_from_args`` writes, for test isolation."""
    snapshot = snapshot_settings(["pr_reviewer.extra_instructions"])
    yield
    restore_settings(snapshot)


def _tokenize(request):
    """Run the real ``_run_command`` and return the ``args`` the tool received."""
    captured = {}

    class _FakeAsk:
        def __init__(self, pr_url, ai_handler=None, args=None):
            captured["args"] = list(args or [])

        async def run(self):
            return True

    span = MagicMock()
    with (
        patch("pr_agent.agent.pr_agent.apply_repo_settings"),
        patch.dict(
            "pr_agent.agent.pr_agent.command2class",
            {"ask": _FakeAsk},
            clear=True,
        ),
    ):
        agent = PRAgent.__new__(PRAgent)
        agent.ai_handler = None
        asyncio.run(agent._run_command(PR_URL, request, None, span))

    return captured["args"]


class TestHashIsNotAComment:
    def test_issue_reference_survives_tokenization(self):
        # "123 do?" used to be dropped, leaving the tool with "what does".
        assert _tokenize("/ask what does #123 do?") == ["what", "does", "#123", "do?"]

    def test_trailing_hash_keeps_the_rest_of_the_question(self):
        assert _tokenize("/ask is this a bug in #42") == ["is", "this", "a", "bug", "in", "#42"]

    def test_multiple_hashes_are_all_preserved(self):
        assert _tokenize("/ask compare #1 and #2") == ["compare", "#1", "and", "#2"]

    def test_hash_inside_double_quotes_still_collapses_to_one_token(self):
        # Quote handling is unchanged; only the comment character is neutralised.
        assert _tokenize('/ask "what about #7 here"') == ["what about #7 here"]

    def test_leading_hash_is_kept_as_a_token(self):
        assert _tokenize("/ask #hashtag") == ["#hashtag"]

    def test_c_sharp_hash_is_not_special(self):
        assert _tokenize("/ask does C# support async?") == ["does", "C#", "support", "async?"]


class TestExistingTokenizationIsUnchanged:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("/ask", []),
            ("/ask what changed here?", ["what", "changed", "here?"]),
            ("/ask 'single quoted words'", ["'single", "quoted", "words'"]),
            ('/ask "double quoted words"', ["double quoted words"]),
            ("/ask   extra   spaces", ["extra", "spaces"]),
        ],
    )
    def test_tokenization_matches_previous_behaviour(self, command, expected):
        assert _tokenize(command) == expected

    def test_unclosed_quote_still_raises(self):
        span = MagicMock()
        with (
            patch("pr_agent.agent.pr_agent.apply_repo_settings"),
                pytest.raises(ValueError),
        ):
            agent = PRAgent.__new__(PRAgent)
            agent.ai_handler = None
            asyncio.run(agent._run_command(PR_URL, '/ask "unfinished', None, span))
