import shlex
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent import cli
from pr_agent.agent import pr_agent as agent_module


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


@pytest.mark.parametrize(
    ("command", "expected_args"),
    [
        (
            "/review --ignore.glob=\"['*.py', '*.lock']\"",
            ["--ignore.glob=['*.py', '*.lock']"],
        ),
        (
            "/review --pr_reviewer.extra_instructions='be concise please'",
            ['--pr_reviewer.extra_instructions="be concise please"'],
        ),
    ],
)
def test_parse_command_keeps_quoted_lists_as_lists(command, expected_args):
    parsed = agent_module.parse_command(command)
    assert parsed[0].lstrip("/") == "review"
    assert parsed[1:] == expected_args


def test_prepare_command_applies_quoted_list_as_list(settings_snapshot):
    agent_module.prepare_command("/review --ignore.glob=\"['*.py', '*.lock']\"")
    assert settings_snapshot.get("ignore.glob") == ["*.py", "*.lock"]


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
    return action, agent_module._reencode_quoted_setting_args(command, args)


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
            '/review --pr_reviewer.extra_instructions="Be \\"strict\\": x"',
            ['--pr_reviewer.extra_instructions="Be \\"strict\\": x"'],
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
        (
            '/review --pr_reviewer.extra_instructions="true" --pr_reviewer.extra_instructions=true',
            ['--pr_reviewer.extra_instructions="true"', '--pr_reviewer.extra_instructions=true'],
        ),
        (
            '/review --pr_reviewer.extra_instructions=true --pr_reviewer.extra_instructions="true"',
            ['--pr_reviewer.extra_instructions=true', '--pr_reviewer.extra_instructions="true"'],
        ),
        (
            "/review --pr_reviewer.extra_instructions='be strict' --pr_reviewer.extra_instructions=\"true\"",
            ["--pr_reviewer.extra_instructions='be", "strict'", '--pr_reviewer.extra_instructions="true"'],
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
    keys = ["pr_reviewer.extra_instructions", "pr_reviewer.num_max_findings", "ignore.glob"]
    saved = {key: settings.get(key, None) for key in keys}
    yield settings
    for key, value in saved.items():
        settings.set(key, value)


@pytest.mark.parametrize(
    ("argument", "key", "expected"),
    [
        ('--pr_reviewer.extra_instructions="Note: be strict"', "pr_reviewer.extra_instructions", "Note: be strict"),
        ('--pr_reviewer.extra_instructions="true"', "pr_reviewer.extra_instructions", "true"),
        ('--pr_reviewer.extra_instructions="no"', "pr_reviewer.extra_instructions", "no"),
        ('--pr_reviewer.extra_instructions="Be \\"strict\\": x"', "pr_reviewer.extra_instructions", 'Be "strict": x'),
        ("--ignore.glob=\"['*.py']\"", "ignore.glob", ["*.py"]),
        ("'twas", "pr_reviewer.num_max_findings", 3),
    ],
)
async def test_string_request_applies_quoted_overrides(monkeypatch, settings_snapshot, argument, key, expected):
    review = Mock(return_value=AsyncMock())

    async def run_sync(func):
        func()

    monkeypatch.setattr(agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(agent_module, "enforce_request_policy", lambda pr_url: None)
    # Keep telemetry cleanup off the thread pool so the test loop can close promptly.
    monkeypatch.setattr(agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setattr(agent_module.asyncio, "to_thread", run_sync)
    monkeypatch.setitem(agent_module.command2class, "review", review)

    command = f"/review {argument} --pr_reviewer.num_max_findings=3"
    assert await agent_module.PRAgent().handle_request("https://github.com/org/repo/pull/1", command) is True

    review.assert_called_once()
    assert settings_snapshot.get(key) == expected
    assert settings_snapshot.get("pr_reviewer.num_max_findings") == 3
