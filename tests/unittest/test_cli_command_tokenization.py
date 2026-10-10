import shlex
from unittest.mock import Mock

import pytest

from pr_agent import cli
from pr_agent.agent.pr_agent import _reencode_quoted_setting_args


@pytest.mark.parametrize(
    ("command", "expected_command", "expected_args"),
    [
        (
            "/review --pr_reviewer.extra_instructions='be concise please'",
            "review",
            ['--pr_reviewer.extra_instructions="be concise please"'],
        ),
        (
            '/review --pr_reviewer.extra_instructions="true"',
            "review",
            ['--pr_reviewer.extra_instructions="true"'],
        ),
        ('/ask "What changed here?"', "ask", ["What changed here?"]),
    ],
)
def test_run_command_preserves_quoted_arguments(monkeypatch, command, expected_command, expected_args):
    run = Mock(return_value=0)
    monkeypatch.setattr(cli, "run", run)
    pr_url = "https://example.com/org/repo/pull/1?label=needs%20review&sort=asc"

    assert cli.run_command(pr_url, command) == 0

    run.assert_called_once()
    args = run.call_args.kwargs["args"]
    assert args.pr_url == pr_url
    assert args.command == expected_command
    assert args.rest == expected_args


def test_run_command_rejects_unclosed_quote_before_dispatch(monkeypatch):
    run = Mock()
    monkeypatch.setattr(cli, "run", run)

    with pytest.raises(ValueError, match="No closing quotation"):
        cli.run_command("https://example.com/org/repo/pull/1", '/ask "unfinished')

    run.assert_not_called()


def _tokenize_like_string_request(command):
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.commenters = ''
    action, *args = list(lexer)
    return action, _reencode_quoted_setting_args(command, args)


@pytest.mark.parametrize(
    ("command", "expected_args"),
    [
        (
            '/review --pr_reviewer.extra_instructions="Note: be strict"',
            ['--pr_reviewer.extra_instructions="Note: be strict"'],
        ),
        (
            '/review --pr_reviewer.extra_instructions="true"',
            ['--pr_reviewer.extra_instructions="true"'],
        ),
        (
            '/review --pr_reviewer.extra_instructions="yes, be strict"',
            ["--pr_reviewer.extra_instructions=yes, be strict"],
        ),
        (
            "/review --pr_reviewer.num_max_findings=3",
            ["--pr_reviewer.num_max_findings=3"],
        ),
        (
            '/ask what does "#123" do?',
            ["what", "does", "#123", "do?"],
        ),
    ],
)
def test_string_request_preserves_quoted_setting_values(command, expected_args):
    action, args = _tokenize_like_string_request(command)
    assert action.lstrip("/") in {"review", "ask"}
    assert args == expected_args


@pytest.fixture
def settings_snapshot():
    from pr_agent.config_loader import get_settings

    settings = get_settings()
    keys = ["pr_reviewer.extra_instructions", "pr_reviewer.num_max_findings"]
    saved = {key: settings.get(key, None) for key in keys}
    yield settings
    for key, value in saved.items():
        settings.set(key, value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Note: be strict", "Note: be strict"),
        ("true", "true"),
        ("no", "no"),
    ],
)
def test_string_request_applies_quoted_overrides_as_strings(settings_snapshot, value, expected):
    from pr_agent.algo.utils import update_settings_from_args

    _, quoted_args = _tokenize_like_string_request(
        f'/review --pr_reviewer.extra_instructions="{value}" --pr_reviewer.num_max_findings=3'
    )
    update_settings_from_args(quoted_args)

    assert settings_snapshot.get("pr_reviewer.extra_instructions") == expected
    assert settings_snapshot.get("pr_reviewer.num_max_findings") == 3
