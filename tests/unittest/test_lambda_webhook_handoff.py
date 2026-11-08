"""Unit tests for the Lambda webhook hand-off (#4748).

The Lambda webhook images acknowledge a webhook after signature verification
and hand the verified payload to a second, asynchronous invocation of the same
function. These tests drive ``lambda_handler`` with Function URL events and
hand-off events; every AWS interaction is mocked, so nothing touches the
network.
"""

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")

from pr_agent.config_loader import get_settings
from pr_agent.servers import (
    github_app,
    gitlab_webhook,
    lambda_handoff,
)

pytest.importorskip("mangum", reason="the Lambda entry points need the optional 'lambda' extra")
github_lambda = pytest.importorskip("pr_agent.servers.github_lambda_webhook")
gitlab_lambda = pytest.importorskip("pr_agent.servers.gitlab_lambda_webhook")

WEBHOOK_SECRET = "test-webhook-secret"
MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/39"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _function_url_event(path: str, body: dict, headers: dict) -> dict:
    raw = json.dumps(body).encode("utf-8")
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json", **headers},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "url",
            "domainName": "url.lambda-url.example.com",
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "1.2.3.4",
            },
            "requestId": "req-1",
            "stage": "$default",
            "time": "01/Jan/2025:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": raw.decode("utf-8"),
        "isBase64Encoded": False,
    }


def _github_function_url_event(body: dict, delivery: str = "delivery-1") -> dict:
    raw = json.dumps(body).encode("utf-8")
    return _function_url_event(
        "/api/v1/github_webhooks",
        body,
        {
            "x-github-event": "issue_comment",
            "x-github-delivery": delivery,
            "x-hub-signature-256": _sign(raw),
        },
    )


def _gitlab_function_url_event(body: dict, token: str = "secret-token") -> dict:
    return _function_url_event(
        "/webhook",
        body,
        {"x-gitlab-token": token, "x-gitlab-event-uuid": "uuid-1"},
    )


@pytest.fixture
def aws_lambda(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "pr-agent-test-function")


@pytest.fixture
def invoke_client(monkeypatch):
    client = mock.Mock()
    monkeypatch.setattr("boto3.client", lambda service_name, **kwargs: client)
    return client


def _invoke_payload(client) -> dict:
    client.invoke.assert_called_once()
    _, kwargs = client.invoke.call_args
    assert kwargs["FunctionName"] == "pr-agent-test-function"
    assert kwargs["InvocationType"] == "Event"
    return json.loads(kwargs["Payload"])


class _Request:
    def __init__(self, headers):
        self.headers = headers


def test_extract_handoff_recognizes_only_self_invocations():
    assert lambda_handoff.extract_handoff(None) is None
    assert lambda_handoff.extract_handoff("not a dict") is None
    assert lambda_handoff.extract_handoff({"version": "2.0", "body": "{}"}) is None
    event = lambda_handoff.build_handoff_event("github", {"action": "created"}, {})
    assert lambda_handoff.extract_handoff(event)["provider"] == "github"


def test_github_lambda_handler_acknowledges_before_the_review_runs(aws_lambda, invoke_client, monkeypatch):
    """A Function URL delivery is 200ed after verification, before any review work."""
    monkeypatch.setitem(get_settings().github, "webhook_secret", WEBHOOK_SECRET)
    body = {"action": "created", "installation": {"id": 42}}
    dispatcher_started = []

    async def slow_handle_request(*args, **kwargs):
        # The review outlives the ~10s webhook timeout: if the first invocation
        # ever awaited it, lambda_handler below would not return in time.
        dispatcher_started.append(True)
        await asyncio.sleep(30)

    monkeypatch.setattr(github_app, "handle_request", slow_handle_request)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def call_with_event_loop():
        # Real Lambda runs the handler on the runtime's main thread, where
        # Mangum finds an event loop; give the worker thread one as well.
        asyncio.set_event_loop(asyncio.new_event_loop())
        return github_lambda.lambda_handler(_github_function_url_event(body), SimpleNamespace())

    try:
        future = pool.submit(call_with_event_loop)
        result = future.result(timeout=15)
    finally:
        pool.shutdown(wait=False)

    assert result["statusCode"] == 200
    assert not dispatcher_started, "the review must not run in the acknowledging invocation"
    assert _invoke_payload(invoke_client) == {
        lambda_handoff.HANDOFF_EVENT_KEY: {
            "provider": "github",
            "body": body,
            "headers": {"x-github-event": "issue_comment", "x-github-delivery": "delivery-1"},
        }
    }


def test_github_lambda_handler_runs_the_dispatcher_for_a_handoff_event(aws_lambda, invoke_client, monkeypatch):
    handle_request = mock.AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "handle_request", handle_request)
    body = {"action": "created", "installation": {"id": 42}}

    event = lambda_handoff.build_handoff_event(
        "github", body, {"x-github-event": "issue_comment", "x-github-delivery": "delivery-1"})
    result = github_lambda.lambda_handler(event, SimpleNamespace())

    assert result == {}
    handle_request.assert_awaited_once_with(body, event="issue_comment", delivery_id="delivery-1")
    invoke_client.invoke.assert_not_called()


def test_handoff_does_not_break_the_next_webhook_on_a_warm_container(aws_lambda, invoke_client, monkeypatch):
    """A warm container must still serve webhooks after a hand-off invocation."""
    monkeypatch.setitem(get_settings().github, "webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(github_app, "handle_request", mock.AsyncMock(return_value={}))

    handoff = lambda_handoff.build_handoff_event("github", {"action": "created"}, {})
    assert github_lambda.lambda_handler(handoff, SimpleNamespace()) == {}

    # Same thread, like a warm Lambda container: the next webhook still works,
    # including Mangum's asyncio.get_event_loop() based dispatch.
    result = github_lambda.lambda_handler(
        _github_function_url_event({"action": "created", "installation": {"id": 42}}), SimpleNamespace())
    assert result["statusCode"] == 200


def test_github_lambda_falls_back_to_inline_processing_when_the_invoke_fails(aws_lambda, monkeypatch):
    monkeypatch.setitem(get_settings().github, "webhook_secret", WEBHOOK_SECRET)
    handle_request = mock.AsyncMock(return_value={})
    monkeypatch.setattr(github_app, "handle_request", handle_request)
    failing_client = mock.Mock()
    failing_client.invoke.side_effect = RuntimeError("AccessDeniedException")

    with mock.patch("boto3.client", lambda service_name, **kwargs: failing_client):
        result = github_lambda.lambda_handler(
            _github_function_url_event({"action": "created", "installation": {"id": 42}}), SimpleNamespace())

    assert result["statusCode"] == 200
    handle_request.assert_awaited_once_with(
        {"action": "created", "installation": {"id": 42}},
        event="issue_comment",
        delivery_id="delivery-1",
    )


def test_gitlab_lambda_handler_acknowledges_and_hands_off(aws_lambda, invoke_client, monkeypatch):
    authenticate = mock.AsyncMock(return_value=None)
    process = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", authenticate)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    body = {"object_kind": "merge_request",
            "object_attributes": {"action": "open", "url": MR_URL}}

    result = gitlab_lambda.lambda_handler(_gitlab_function_url_event(body), SimpleNamespace())

    assert result["statusCode"] == 200
    authenticate.assert_awaited_once()
    process.assert_not_awaited()
    assert _invoke_payload(invoke_client) == {
        lambda_handoff.HANDOFF_EVENT_KEY: {
            "provider": "gitlab",
            "body": body,
            "headers": {"x-gitlab-token": "secret-token", "x-gitlab-event-uuid": "uuid-1"},
        }
    }


def test_gitlab_lambda_handler_runs_the_dispatcher_for_a_handoff_event(aws_lambda, invoke_client, monkeypatch):
    authenticate = mock.AsyncMock(return_value=None)
    process = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", authenticate)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)
    body = {"object_kind": "merge_request",
            "object_attributes": {"action": "open", "url": MR_URL}}

    event = lambda_handoff.build_handoff_event(
        "gitlab", body, {"x-gitlab-token": "project-secret:provider-token", "x-gitlab-event-uuid": "uuid-1"})
    result = gitlab_lambda.lambda_handler(event, SimpleNamespace())

    assert result == {}
    process.assert_awaited_once_with(body, {"server_type": "gitlab_app"})
    # The webhook token travels with the hand-off so the second invocation can
    # re-establish the request-scoped credentials.
    request = authenticate.await_args.args[0]
    assert request.headers.get("X-Gitlab-Token") == "project-secret:provider-token"
    invoke_client.invoke.assert_not_called()


def test_gitlab_lambda_drops_the_handoff_when_authentication_fails(aws_lambda, invoke_client, monkeypatch):
    from fastapi.responses import JSONResponse

    authenticate = mock.AsyncMock(return_value=JSONResponse(status_code=401, content={"message": "unauthorized"}))
    process = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", authenticate)
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)

    event = lambda_handoff.build_handoff_event("gitlab", {"object_kind": "push"}, {"x-gitlab-token": "revoked"})
    result = gitlab_lambda.lambda_handler(event, SimpleNamespace())

    assert result == {}
    process.assert_not_awaited()


async def test_github_route_keeps_background_tasks_outside_lambda(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    body = {"action": "created"}

    async def fake_get_body(_request):
        return body

    monkeypatch.setattr(github_app, "get_body", fake_get_body)
    handle_request = mock.AsyncMock()
    monkeypatch.setattr(github_app, "handle_request", handle_request)
    background_tasks = BackgroundTasks()
    request = _Request({"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "delivery-1"})

    with mock.patch("boto3.client") as boto_client, request_cycle_context({}):
        await github_app.handle_github_webhooks(background_tasks, request, object())
        await background_tasks()

    handle_request.assert_awaited_once_with(body, event="issue_comment", delivery_id="delivery-1")
    boto_client.assert_not_called()


async def test_gitlab_route_keeps_background_tasks_outside_lambda(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", mock.AsyncMock(return_value=None))
    process = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "process_gitlab_event", process)

    class _GitlabRequest:
        headers = {"X-Gitlab-Token": "secret-token", "X-Gitlab-Event-UUID": "uuid-1"}

        async def json(self):
            return {"object_kind": "merge_request"}

    background_tasks = BackgroundTasks()
    with mock.patch("boto3.client") as boto_client, request_cycle_context({}):
        await gitlab_webhook.gitlab_webhook(background_tasks, _GitlabRequest())
        await background_tasks()

    process.assert_awaited_once_with({"object_kind": "merge_request"}, {"server_type": "gitlab_app"})
    boto_client.assert_not_called()
