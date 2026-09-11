"""A structured payload must never make the log call itself raise.

loguru formats the message with `str.format(*args, **kwargs)` as soon as a keyword argument
is passed, so `get_logger().error(f"... {e}", artifact=...)` re-formats text that already
carries the exception. Every PyGithub error renders as `<status> <json body>` and LiteLLM
wraps the provider's JSON, so the braces in that text turn the handler into the failure.
"""
from unittest.mock import MagicMock

import pytest
from github import GithubException

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.log import get_logger
from pr_agent.servers.utils import RateLimitExceeded
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_description import PRDescription

RATE_LIMITED = GithubException(
    403,
    {"message": "API rate limit exceeded for installation ID 1.",
     "documentation_url": "https://docs.github.com/rest#rate-limiting"},
    None,
)
NOT_ACCESSIBLE = GithubException(
    403, {"message": "Resource not accessible by integration", "documentation_url": "https://docs.github.com"}, None,
)


@pytest.fixture
def captured_records():
    records = []
    handler_id = get_logger().add(lambda message: records.append(message.record), level="TRACE")
    yield records
    get_logger().remove(handler_id)


@pytest.mark.parametrize("message", [
    'Failed: 403 {"message": "API rate limit exceeded"}',
    "Failed to parse config override PR_REVIEWER.EXTRA_INSTRUCTIONS=`{}` preferred",
    "Unbalanced brace { in the model answer",
    "Value {0} and {placeholder} came from the model",
])
def test_a_braced_message_with_a_payload_does_not_raise(captured_records, message):
    get_logger().error(message, artifact={"traceback": "..."})

    assert captured_records[-1]["message"] == message
    assert captured_records[-1]["extra"]["artifact"] == {"traceback": "..."}


def test_positional_templating_still_formats(captured_records):
    get_logger().info("Cleaned up temp repo at {}", "/tmp/repo")

    assert captured_records[-1]["message"] == "Cleaned up temp repo at /tmp/repo"


def test_named_templating_still_formats(captured_records):
    get_logger().error("Azure failed to publish, error: {error}", error="boom")

    assert captured_records[-1]["message"] == "Azure failed to publish, error: boom"
    assert captured_records[-1]["extra"]["error"] == "boom"


def test_payload_without_a_placeholder_is_still_bound(captured_records):
    get_logger().info("PR-Agent request handler started", analytics=True)

    assert captured_records[-1]["extra"]["analytics"] is True


def test_the_caller_module_is_still_recorded(captured_records):
    get_logger().warning("a message with a { brace", artifact={"k": "v"})

    assert captured_records[-1]["name"] == __name__


# --------------------------------------------------------------------------------------
# The three handlers whose contract the formatting failure broke.
# --------------------------------------------------------------------------------------
@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)


@pytest.fixture
def publishing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.config, "publish_output", True)
    monkeypatch.setattr(settings.config, "publish_output_progress", True)
    monkeypatch.setattr(settings.config, "is_auto_command", False, raising=False)
    monkeypatch.setattr(settings.config, "propagate_tool_errors", False, raising=False)
    return settings


def test_a_rate_limited_diff_fetch_is_retried(monkeypatch, no_sleep):
    """The handler converts the 403 into RateLimitExceeded, which `retry_call` retries."""
    get_settings().set("GITHUB.RATELIMIT_RETRIES", 3)
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: MagicMock())
    provider = GithubProvider(pr_url=None)
    provider.pr = MagicMock()
    attempts = []

    def rate_limited():
        attempts.append(1)
        raise RATE_LIMITED

    provider.pr.get_files.side_effect = rate_limited

    with pytest.raises(RateLimitExceeded):
        provider.get_diff_files()
    assert len(attempts) == 3


async def test_describe_swallows_a_provider_error(publishing):
    """`propagate_tool_errors` is false by default, so `run()` logs and returns."""
    provider = MagicMock()
    provider.publish_comment.side_effect = NOT_ACCESSIBLE
    tool = PRDescription.__new__(PRDescription)
    tool.git_provider = provider
    tool.pr_id = "org/repo/1"

    await tool.run()


async def test_improve_reports_a_provider_error_to_the_pr(publishing):
    provider = MagicMock()
    provider.supports_code_suggestion_state = lambda: False
    provider.get_files.side_effect = RATE_LIMITED
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None

    await tool.run()

    provider.publish_comment.assert_called_once_with("Failed to generate code suggestions for PR")


async def test_one_unreadable_ticket_link_keeps_the_others(monkeypatch):
    """A 404 on one linked issue must not discard every extracted ticket."""
    from types import SimpleNamespace

    from pr_agent.tools.ticket_pr_compliance_check import extract_tickets

    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: MagicMock())
    provider = GithubProvider(pr_url=None)
    provider.repo = "org/repo"
    provider.repo_obj = MagicMock()

    def get_issue(number):
        if number == 1:
            raise GithubException(404, {"message": "Not Found"}, None)
        return SimpleNamespace(number=number, title="Issue", body="Body", labels=[])

    provider.repo_obj.get_issue.side_effect = get_issue
    provider.get_user_description = lambda: "Fixes #1 and closes #2"
    provider.get_pr_branch = lambda: "fix/retry"
    provider.fetch_sub_issues = lambda issue_url: []

    tickets = await extract_tickets(provider)

    assert [ticket["ticket_id"] for ticket in tickets] == [2]
