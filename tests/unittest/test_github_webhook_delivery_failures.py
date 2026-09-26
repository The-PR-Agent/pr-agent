"""Exercise failed delivery retries through the real GitHub event handlers."""

import asyncio
import copy
from unittest.mock import AsyncMock, Mock, call

import pytest
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import github_app
from pr_agent.servers import utils as servers_utils

EVENTS = [
    ("issue_comment", "created"),
    ("pull_request", "opened"),
    ("pull_request_review", "submitted"),
    ("pull_request", "synchronize"),
]
AUTO_EVENTS = EVENTS[1:]
PR_URL = "https://api.github.com/repos/org/repo/pulls/1"


@pytest.fixture
def delivery_agent(monkeypatch):
    settings = copy.deepcopy(get_settings())
    settings.set("GITHUB_APP.WEBHOOK_DELIVERY_DEDUPLICATION", True)
    settings.set("GITHUB_APP.HANDLE_PR_ACTIONS", ["opened"])
    settings.set("GITHUB_APP.HANDLE_PUSH_TRIGGER", True)
    settings.set("GITHUB_APP.PUSH_TRIGGER_IGNORE_MERGE_COMMITS", False)
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", False)
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.PR_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.PUSH_COMMANDS", ["/review"])
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", False)
    agent = Mock(handle_request=AsyncMock(return_value=True))
    identity = Mock()
    identity.verify_eligibility.return_value = Eligibility.ELIGIBLE
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(github_app, "get_identity_provider", lambda: identity)
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **kwargs: Mock())
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda api_url: None)
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})
    monkeypatch.setattr(
        github_app,
        "_completed_webhook_deliveries",
        servers_utils.DefaultDictWithTimeout(
            float, ttl=github_app._WEBHOOK_DELIVERY_TTL, update_key_time_on_get=False
        ),
    )
    with request_cycle_context({"settings": settings}):
        yield agent, "delivery-1"


def _body(action):
    return {
        "action": action,
        "installation": {"id": 1},
        "sender": {"id": 2, "login": "user", "type": "User"},
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "url": PR_URL,
            "state": "open",
            "draft": False,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        "comment": {"body": "/review", "pull_request_url": PR_URL, "id": 42},
        "review": {"state": "changes_requested", "user": {"type": "User"}},
        "before": "before-sha",
        "after": "after-sha",
    }


@pytest.mark.parametrize(("event", "action"), EVENTS)
async def test_false_agent_result_can_be_retried(delivery_agent, event, action):
    agent, delivery_id = delivery_agent
    agent.handle_request.side_effect = [False, True]
    body = _body(action)

    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)

    assert agent.handle_request.await_count == 2


@pytest.mark.parametrize(("event", "action"), AUTO_EVENTS)
@pytest.mark.parametrize("failure", [False, RuntimeError("agent failed")], ids=["false", "exception"])
async def test_auto_commands_continue_after_failure_and_allow_retry(delivery_agent, event, action, failure):
    agent, delivery_id = delivery_agent
    for commands in ("PR_COMMANDS", "REVIEW_COMMANDS", "PUSH_COMMANDS"):
        get_settings().set(f"GITHUB_APP.{commands}", ["/review", "/describe"])
    agent.handle_request.side_effect = [failure, True, True, True]
    body = _body(action)

    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)

    assert agent.handle_request.await_args_list == [
        call(PR_URL, ["/review"]),
        call(PR_URL, ["/describe"]),
        call(PR_URL, ["/review"]),
        call(PR_URL, ["/describe"]),
    ]


@pytest.mark.parametrize(("event", "action"), EVENTS)
async def test_cancelled_agent_call_propagates_and_allows_retry(delivery_agent, event, action):
    agent, delivery_id = delivery_agent
    agent.handle_request.side_effect = [asyncio.CancelledError(), True]
    body = _body(action)

    with pytest.raises(asyncio.CancelledError):
        await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)

    assert agent.handle_request.await_count == 2


@pytest.mark.parametrize(("event", "action"), EVENTS)
async def test_intentionally_ignored_delivery_is_completed(delivery_agent, monkeypatch, event, action):
    agent, delivery_id = delivery_agent
    body = _body(action)
    if event == "issue_comment":
        body["comment"]["body"] = "Thanks for the review"
    else:
        body["pull_request"]["draft"] = True
    dispatch = AsyncMock(wraps=github_app._dispatch_request)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)

    await github_app.handle_request(body, event, delivery_id)
    await github_app.handle_request(body, event, delivery_id)

    dispatch.assert_awaited_once()
    agent.handle_request.assert_not_awaited()
