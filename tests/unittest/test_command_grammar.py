"""Tests for ``parse_and_normalize_command``, the single request parser behind ``PRAgent._run_command``."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.agent.pr_agent import PRAgent, command2class, parse_and_normalize_command, parse_command
from pr_agent.algo.utils import _fix_key_value

PR_URL = "https://github.com/org/repo/pull/1"


def test_cli_list_and_webhook_comment_normalize_to_the_same_command():
    cli = parse_and_normalize_command(["review", "--pr_reviewer.extra_instructions=focus on auth"])
    comment = parse_and_normalize_command('/review --pr_reviewer.extra_instructions="focus on auth"')

    assert cli.is_valid and comment.is_valid
    assert (cli.canonical_action, cli.args) == (comment.canonical_action, comment.args)
    assert comment.args == ["--pr_reviewer.extra_instructions=focus on auth"]
    assert comment.action == "/review"
    assert comment.raw == '/review --pr_reviewer.extra_instructions="focus on auth"'


def test_action_is_canonicalized_but_not_alias_collapsed():
    parsed = parse_and_normalize_command("/Review_PR")
    assert parsed.canonical_action == "review_pr"
    assert parsed.canonical_action in command2class


@pytest.mark.parametrize(
    "command",
    [
        ["describe", '--pr_description.extra_instructions=["[1] a", "[2] b"]'],
        r'/describe --pr_description.extra_instructions="[\"[1] a\", \"[2] b\"]"',
    ],
)
def test_bracketed_list_override_stays_a_list(command):
    parsed = parse_and_normalize_command(command)

    assert parsed.is_valid
    (argument,) = parsed.args
    key, value = argument.split("=", 1)
    assert _fix_key_value(key, value)[1] == ["[1] a", "[2] b"]


def test_quoted_scalar_keeps_its_string_type():
    parsed = parse_and_normalize_command('/review --pr_reviewer.extra_instructions="true"')
    assert parsed.args == ['--pr_reviewer.extra_instructions="true"']


@pytest.mark.parametrize("command", ['/ask "unfinished', '/review --pr_reviewer.extra_instructions="a b'])
def test_unbalanced_quotes_return_an_invalid_command_instead_of_raising(command):
    parsed = parse_and_normalize_command(command)

    assert not parsed.is_valid
    assert parsed.action == ""
    assert parsed.error == "No closing quotation"


@pytest.mark.parametrize("command", ["", "   ", []])
def test_empty_command_is_invalid(command):
    parsed = parse_and_normalize_command(command)
    assert not parsed.is_valid and parsed.action == ""


def test_forbidden_setting_is_reported_with_the_offending_param():
    parsed = parse_and_normalize_command("/review --openai.key=x")
    assert not parsed.is_valid
    assert "openai.key" in parsed.error


def test_parse_command_is_unchanged():
    assert parse_command("/review --pr_reviewer.extra_instructions='be concise'") == [
        "/review",
        '--pr_reviewer.extra_instructions="be concise"',
    ]


def test_unknown_command_is_rejected_before_any_tool_or_provider_is_created():
    parsed = parse_and_normalize_command("/frobnicate --pr_reviewer.extra_instructions=x")
    assert parsed.is_valid
    assert parsed.canonical_action not in command2class

    tool = MagicMock()
    span = MagicMock()
    agent = PRAgent.__new__(PRAgent)
    agent.ai_handler = None
    with (
        patch("pr_agent.agent.pr_agent.apply_repo_settings"),
        patch("pr_agent.agent.pr_agent.update_settings_from_args", side_effect=lambda args: args),
        patch("pr_agent.agent.pr_agent.get_git_provider_with_context") as get_provider,
        patch.dict("pr_agent.agent.pr_agent.command2class", {"review": tool}),
    ):
        result = asyncio.run(agent._run_command(PR_URL, "/frobnicate", None, span))

    assert result is False
    tool.assert_not_called()
    get_provider.assert_not_called()
    span.set_attribute.assert_any_call("error.type", "unknown_command")


def test_run_command_still_raises_on_unbalanced_quotes():
    agent = PRAgent.__new__(PRAgent)
    agent.ai_handler = None
    with (
        patch("pr_agent.agent.pr_agent.apply_repo_settings"),
        pytest.raises(ValueError, match="No closing quotation"),
    ):
        asyncio.run(agent._run_command(PR_URL, '/ask "unfinished', None, MagicMock()))
