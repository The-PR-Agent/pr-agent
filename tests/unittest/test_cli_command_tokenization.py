from unittest.mock import Mock

import pytest

from pr_agent import cli


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
