import subprocess
import sys

import pytest

import pr_agent.servers.github_app as github_app
import pr_agent.servers.github_common as github_common

QUOTE_REPLY_WITH_DOUBLE_ASK = (
    "> ![image][image-1]\n\n/review please\n\n/ask why does /ask appear twice here?"
)

_IMPORT_WITHOUT_GITHUB_APP = """
import sys

import pr_agent.servers.github_action_runner

assert 'pr_agent.servers.github_app' not in sys.modules, 'github_app was imported'
assert 'pr_agent.servers.github_common' in sys.modules

from pr_agent.config_loader import get_settings

assert get_settings().get('GITHUB.DEPLOYMENT_TYPE') != 'app'
"""


def test_github_app_reexports_the_shared_helpers():
    assert github_app.handle_line_comments is github_common.handle_line_comments
    assert github_app.matches_review_state is github_common.matches_review_state
    assert github_app._reformat_quote_ask_command is github_common._reformat_quote_ask_command


@pytest.mark.parametrize(
    "comment_body",
    [
        QUOTE_REPLY_WITH_DOUBLE_ASK,
        "> ![image][image-1]\n\n/ask what does this do?",
        "plain comment with no command",
        "/review already a command",
    ],
)
def test_reformat_quote_ask_command_behavior(comment_body):
    reformatted = github_common._reformat_quote_ask_command(comment_body)

    if "/ask" not in comment_body or not comment_body.strip().startswith("> ![image]"):
        assert reformatted is None
    else:
        assert reformatted.startswith("/ask")
        # The #3670 contract: everything after the FIRST /ask is kept, including
        # any further /ask occurrence the old split-based copy dropped.
        before, _, after = comment_body.partition("/ask")
        assert reformatted == "/ask" + after + " \n" + before.strip().lstrip(">")


def test_import_action_runner_without_github_app():
    """The Action runner must import without github_app's side effects.

    github_app's import switches logs to JSON and overrides
    GITHUB.DEPLOYMENT_TYPE to "app". A subprocess keeps the assertion honest
    because the test session has already imported github_app."""
    result = subprocess.run([sys.executable, "-c", _IMPORT_WITHOUT_GITHUB_APP],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"importing the action runner pulled in github_app:\n{result.stderr}"


class _Provider:
    def __init__(self, base_merges):
        self.base_merges = base_merges
        self.looked_up = []

    def is_base_branch_merge(self, sha, previous_head):
        self.looked_up.append((sha, previous_head))
        if sha == "unreadable":
            raise RuntimeError("commit unavailable")
        return (sha, previous_head) in self.base_merges


@pytest.mark.parametrize(
    ("provider", "before_sha", "after_sha", "expected"),
    [
        (_Provider({("merge", "head")}), "head", "merge", True),
        (_Provider({("merge", "head")}), "older", "merge", False),
        (_Provider({("merge", "head")}), "head", "ordinary", False),
        (_Provider(set()), "head", "unreadable", False),
        (None, "head", "merge", False),
        (object(), "head", "merge", False),
        (_Provider({("merge", "head")}), None, "merge", False),
        (_Provider({("merge", "head")}), "", "merge", False),
        (_Provider({("merge", "head")}), "head", None, False),
        (_Provider({("merge", "head")}), "head", "", False),
    ],
    ids=[
        "base-branch-merge",
        "other-previous-head",
        "not-a-base-branch-merge",
        "lookup-fails",
        "no-provider",
        "provider-without-lookup",
        "no-before",
        "empty-before",
        "no-after",
        "empty-after",
    ],
)
def test_pushed_base_branch_merge(provider, before_sha, after_sha, expected):
    assert github_common.pushed_base_branch_merge(provider, before_sha, after_sha) is expected
    if isinstance(provider, _Provider):
        assert provider.looked_up == ([(after_sha, before_sha)] if before_sha and after_sha else [])
