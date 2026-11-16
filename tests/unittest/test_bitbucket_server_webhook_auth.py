import copy
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

from pr_agent.config_loader import global_settings
from pr_agent.servers import bitbucket_server_webhook as webhook

SECRET = "test-webhook-secret"
PAYLOAD = {
    "eventKey": "pr:comment:added",
    "comment": {"text": "/review"},
    "pullRequest": {
        "id": 7,
        "toRef": {"repository": {"slug": "repo", "project": {"key": "PROJ"}}},
    },
}
BODY = json.dumps(PAYLOAD).encode()


@pytest.fixture
def secured_client(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", SECRET)
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.test")
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    dispatch = AsyncMock()
    logger = Mock()
    monkeypatch.setattr(webhook, "_run_commands_sequentially", dispatch)
    monkeypatch.setattr(webhook, "get_logger", lambda: logger)
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(webhook.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, settings, dispatch, logger


def _signature(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("secret", [None, ""])
@pytest.mark.parametrize("body", [BODY, b"not json", b'{"test": true}'])
def test_unconfigured_secret_rejects_before_processing(secured_client, secret, body):
    client, settings, dispatch, logger = secured_client
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", secret)

    for headers in ({}, {"x-hub-signature": _signature(body)}):
        response = client.post("/webhook", content=body, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Webhook authentication is not configured."}
    dispatch.assert_not_awaited()
    assert logger.error.call_count == 2
    for call in logger.error.call_args_list:
        assert "BITBUCKET_SERVER.WEBHOOK_SECRET" in call.args[0]
    logger.info.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"x-hub-signature": "sha256=invalid"}])
def test_missing_or_wrong_signature_rejects_without_dispatch(secured_client, headers):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=BODY, headers=headers)

    assert response.status_code == 403
    dispatch.assert_not_awaited()


def test_tampered_payload_rejects_without_dispatch(secured_client):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=BODY + b" ", headers={"x-hub-signature": _signature(BODY)})

    assert response.status_code == 403
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("path", ["/webhook", "/"])
def test_valid_signature_dispatches_comment_command(secured_client, path):
    client, _settings, dispatch, _logger = secured_client

    response = client.post(path, content=BODY, headers={"x-hub-signature": _signature(BODY)})

    assert response.status_code == 200
    assert response.json() == {"message": "success"}
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[:2] == (
        ["/review"], "https://bitbucket.example.test/projects/PROJ/repos/repo/pull-requests/7",
    )


def test_configured_connection_test_does_not_dispatch(secured_client):
    client, _settings, dispatch, _logger = secured_client

    response = client.post("/webhook", content=b'{"test": true}')

    assert response.status_code == 200
    assert response.json() == {"message": "connection test successful"}
    dispatch.assert_not_awaited()
