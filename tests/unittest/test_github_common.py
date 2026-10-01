import copy
import json
import subprocess
import sys

import pytest

import pr_agent.servers.github_action_runner as github_action_runner
import pr_agent.servers.github_app as github_app
import pr_agent.servers.github_common as github_common
from pr_agent.config_loader import get_settings

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


async def test_action_reformats_quote_reply_with_double_ask(monkeypatch, tmp_path):
    """The Action must apply the same #3670 fix as the app: a quoted mobile reply
    keeps everything after the first /ask instead of dropping the text between
    the first and second occurrence."""
    handled = []
    settings = get_settings()
    had_github = "GITHUB" in settings
    original_github = copy.deepcopy(settings.get("GITHUB", None))

    class FakeProvider:
        def __init__(self, pr_url=None):
            self.pr_url = pr_url

        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            return None

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "get_git_provider", lambda: FakeProvider)
    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)

    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": QUOTE_REPLY_WITH_DOUBLE_ASK, "id": 123},
        "issue": {
            "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/1"},
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": "User"},
    }))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    try:
        await github_action_runner.run_action()
    finally:
        if had_github:
            settings.set("GITHUB", original_github)
        else:
            settings.store.pop("GITHUB", None)

    assert len(handled) == 1
    url, body = handled[0]
    assert url == "https://api.github.com/repos/org/repo/pulls/1"
    assert body.startswith("/ask")
    assert "why does /ask appear twice here?" in body
    assert "/review please" in body
