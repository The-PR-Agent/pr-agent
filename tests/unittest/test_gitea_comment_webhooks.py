import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks
from starlette.requests import Request
from starlette.responses import Response

from pr_agent.servers import gitea_app

PR_URL = "https://gitea.example.test/owner/repo/pulls/7"
WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def agent(monkeypatch):
    """Isolate command execution and request settings without bypassing signature validation."""
    agent = SimpleNamespace(handle_request=AsyncMock())
    settings = SimpleNamespace(gitea=SimpleNamespace(webhook_secret=WEBHOOK_SECRET))
    monkeypatch.setattr(gitea_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(gitea_app, "get_settings", lambda: settings)
    monkeypatch.setattr(gitea_app, "global_settings", settings)
    monkeypatch.setattr(gitea_app, "context", {})
    return agent


def _payload(**overrides):
    """Build the native payload for a newly created PR timeline comment."""
    return {
        "action": "created",
        "is_pull": True,
        "issue": {"number": 7},
        "pull_request": {"url": PR_URL},
        "comment": {"body": "/review"},
        **overrides,
    }


def _request(payload, event="issue_comment", event_type="pull_request_comment", signature="valid"):
    """Sign a request using Gitea's normalized event and specific event-type headers."""
    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"x-gitea-event", event.encode("ascii")),
        (b"x-gitea-event-type", event_type.encode("ascii")),
    ]
    if signature is not None:
        digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers.append((b"x-gitea-signature", (digest if signature == "valid" else signature).encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/v1/gitea_webhooks", "headers": headers}, receive)


async def _deliver(request):
    """Execute the webhook's real dispatch and queued background task."""
    tasks = BackgroundTasks()
    result = await gitea_app.handle_gitea_webhooks(tasks, request, Response())
    assert result == {}
    assert len(tasks.tasks) == 1
    await tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/review", '/ask "Explain this change"', "/improve"])
async def test_signed_pr_timeline_comment_dispatches_once(agent, command):
    # Gitea normalizes HookEventPullRequestComment to X-Gitea-Event: issue_comment.
    await _deliver(_request(_payload(comment={"body": command})))

    agent.handle_request.assert_awaited_once_with(PR_URL, command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "edited"},
        {"action": "deleted"},
        {"action": None},
        {"comment": {"body": "A regular comment"}},
        {"comment": {"body": ""}},
        {"comment": {}},
        {"pull_request": {}},
    ],
)
async def test_ineligible_pr_timeline_comment_does_not_dispatch(agent, overrides):
    await _deliver(_request(_payload(**overrides)))

    agent.handle_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_only_comment_does_not_dispatch(agent):
    payload = _payload(is_pull=False)
    del payload["pull_request"]

    await _deliver(_request(payload, event_type="issue_comment"))

    agent.handle_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_comment_event_is_not_a_timeline_command(agent):
    payload = {
        "action": "reviewed",
        "pull_request": {"url": PR_URL},
        "review": {"type": "comment", "content": "/review"},
    }
    await _deliver(_request(payload, event="pull_request_comment", event_type="pull_request_review_comment"))

    agent.handle_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("signature", "status"), [(None, 400), ("0" * 64, 401)])
async def test_unsigned_or_invalid_comment_is_rejected_before_dispatch(agent, signature, status):
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as caught:
        await gitea_app.handle_gitea_webhooks(tasks, _request(_payload(), signature=signature), Response())

    assert caught.value.status_code == status
    assert tasks.tasks == []
    agent.handle_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_secret_rejects_every_webhook(monkeypatch):
    # Like the GitHub app, the Gitea webhook must fail closed when the secret is
    # not configured instead of accepting unauthenticated events that any caller
    # could forge to trigger expensive commands.
    settings = SimpleNamespace(gitea=SimpleNamespace(webhook_secret=""))
    monkeypatch.setattr(gitea_app, "get_settings", lambda: settings)
    monkeypatch.setattr(gitea_app, "global_settings", settings)
    monkeypatch.setattr(gitea_app, "context", {})
    agent = SimpleNamespace(handle_request=AsyncMock())
    monkeypatch.setattr(gitea_app, "PRAgent", lambda: agent)

    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as caught:
        await gitea_app.handle_gitea_webhooks(tasks, _request(_payload()), Response())

    assert caught.value.status_code == 403
    assert tasks.tasks == []
    agent.handle_request.assert_not_awaited()
