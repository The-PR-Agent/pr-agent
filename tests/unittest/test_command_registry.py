"""Tests for the single command registry (issue #4723).

The registry is the one place where command spellings, aliases, and the
per-command facts the entry points need live. These tests pin the refactor:
the registry must cover exactly the dispatched commands, aliases must share
their canonical command's command and check run, and an automatic ``/review_pr``
must open and complete the ``review`` check run like ``/review`` does.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent.command_registry import (
    COMMAND_SPECS,
    command_check_run,
    command_takes_comment_id,
    resolve_command,
)
from pr_agent.agent.pr_agent import command2class, commands
from pr_agent.cli import (
    _JSON_DESCRIBE_COMMANDS,
    _JSON_LABEL_COMMANDS,
    _JSON_REVIEW_COMMANDS,
    _PLAIN_DIFF_MARKDOWN_COMMANDS,
)
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from pr_agent.servers import github_app


def test_registry_covers_exactly_the_dispatched_commands():
    assert set(COMMAND_SPECS) == set(command2class)
    assert set(COMMAND_SPECS) == set(commands)
    for name, spec in COMMAND_SPECS.items():
        assert spec.command is command2class[name]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("review_pr", "review"),
        ("describe_pr", "describe"),
        ("improve_code", "improve"),
        ("ask_question", "ask"),
        ("settings", "config"),
    ],
)
def test_aliases_share_their_canonical_command(alias, canonical):
    assert resolve_command(alias).command is resolve_command(canonical).command


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("/review", "review"),
        ("/review_pr", "review"),
        ("review_pr", "review"),
        ("/REVIEW_PR", "review"),
        ("/describe", "describe"),
        ("/describe_pr", "describe"),
        ("/improve", "review"),
        ("/improve_code", "review"),
        ("/ask", None),
        ("/help", None),
        ("/unknown_command", None),
    ],
)
def test_command_check_run_resolution(spelling, expected):
    assert command_check_run(spelling) == expected
    assert github_app._auto_command_check_run(spelling)[1] == expected


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("/review", True),
        ("/review_pr", True),
        ("/improve", True),
        ("/improve_code", True),
        ("/describe", False),
        ("/ask", True),
        ("/ask_question", True),
        ("/answer", False),
        ("/auto_review", False),
        ("/unknown_command", False),
    ],
)
def test_command_takes_comment_id(spelling, expected):
    assert command_takes_comment_id(spelling) == expected


def test_cli_command_sets_match_the_registry():
    assert _PLAIN_DIFF_MARKDOWN_COMMANDS == {
        "review", "review_pr",
        "describe", "describe_pr",
        "improve", "improve_code",
        "ask", "ask_question",
        "config", "settings", "help",
    }
    assert _JSON_REVIEW_COMMANDS == {"review", "review_pr", "improve", "improve_code"}
    assert _JSON_DESCRIBE_COMMANDS == {"describe", "describe_pr"}
    assert _JSON_LABEL_COMMANDS == {"generate_labels"}


@pytest.mark.parametrize("name", ["improve", "improve_code"])
def test_improve_resolves_to_review_spec(name):
    spec = resolve_command(name)
    review = resolve_command("review")

    assert spec.command is PRReviewer
    assert spec.check_run == review.check_run == "review"
    assert spec.json_output == review.json_output == "review"
    assert command2class[name] is PRReviewer


def test_no_suggestions_check_run_for_improve():
    assert "suggestions" not in {spec.check_run for spec in COMMAND_SPECS.values()}
    assert github_app._auto_command_check_run("/improve")[1] == "review"


@pytest.mark.parametrize("name", ["answer", "auto_review"])
def test_removed_commands_are_not_registered(name):
    assert resolve_command(name) is None
    assert name not in command2class
    assert name not in commands


@pytest.mark.parametrize("succeeded", [True, False])
async def test_auto_review_pr_opens_and_completes_the_review_check_run(monkeypatch, succeeded):
    """Issue #4723 done-when: an automatic /review_pr opens and completes the review check run."""
    settings = deepcopy(get_settings())
    settings.set("github_app.feedback_on_draft_pr", True)
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", True)
    provider = SimpleNamespace(
        start_check_run=Mock(), finish_check_run=Mock(),
    )
    api_url = "https://api.github.com/repos/org/repo/pulls/1"

    class Agent:
        async def handle_request(self, api_url, command, notify=None):
            if notify:
                notify()
            return succeeded

    monkeypatch.setattr(github_app, "get_pr_commands", lambda _name: ["/review_pr"])
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "init_run_details", lambda: None)
    monkeypatch.setattr(github_app, "command_failed", lambda: False)

    with request_cycle_context({"settings": settings, "git_provider": {api_url: provider}}):
        result = await github_app._perform_auto_commands_github(
            "pr_commands", Agent(), {"action": "opened", "pull_request": {"draft": False}}, api_url, {}
        )

    assert result is succeeded
    provider.start_check_run.assert_called_once_with("review", "PR-Agent is running /review_pr")
    provider.finish_check_run.assert_called_once_with(
        "review", "success" if succeeded else "failure",
        "PR-Agent ran /review_pr" if succeeded else "PR-Agent could not finish /review_pr",
    )
