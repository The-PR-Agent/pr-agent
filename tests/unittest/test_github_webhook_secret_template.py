"""The shipped secrets template must not carry a usable GitHub webhook secret.

``get_body`` in ``pr_agent.servers.github_app`` rejects every GitHub webhook
with HTTP 403 while ``github.webhook_secret`` is unset. An unedited copy of
``pr_agent/settings/.secrets_template.toml`` has to hit that check: a non-empty
placeholder would instead be accepted as the real secret, and any request
signed with the public placeholder string would pass ``verify_signature``.
"""

import copy
import hashlib
import hmac
import json
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

from pr_agent.config_loader import global_settings
from pr_agent.servers import github_app

TEMPLATE = Path(__file__).resolve().parents[2] / "pr_agent" / "settings" / ".secrets_template.toml"
BODY = json.dumps({"installation": {"id": 1}, "action": "created"}).encode()


def _template_github_webhook_secret():
    with TEMPLATE.open("rb") as handle:
        return tomllib.load(handle)["github"]["webhook_secret"]


def _signature(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def webhook_client(monkeypatch):
    settings = copy.deepcopy(global_settings)
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(github_app.lambda_handoff, "handoff_enabled", lambda: False)
    handle_request = AsyncMock()
    monkeypatch.setattr(github_app, "handle_request", handle_request)
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(github_app.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, settings, handle_request


def test_template_github_webhook_secret_is_empty():
    assert _template_github_webhook_secret() == ""


def test_webhook_signed_with_the_template_secret_is_rejected(webhook_client):
    client, settings, handle_request = webhook_client
    template_secret = _template_github_webhook_secret()
    settings.set("GITHUB.WEBHOOK_SECRET", template_secret)

    response = client.post(
        "/api/v1/github_webhooks",
        content=BODY,
        headers={"x-hub-signature-256": _signature(template_secret, BODY)},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Webhook secret not configured"}
    handle_request.assert_not_awaited()


def test_webhook_signed_with_a_configured_secret_is_accepted(webhook_client):
    client, settings, handle_request = webhook_client
    settings.set("GITHUB.WEBHOOK_SECRET", "operator-secret")

    response = client.post(
        "/api/v1/github_webhooks",
        content=BODY,
        headers={"x-hub-signature-256": _signature("operator-secret", BODY)},
    )

    assert response.status_code == 200
    handle_request.assert_awaited_once()


def test_webhook_with_a_wrong_signature_is_rejected(webhook_client):
    client, settings, handle_request = webhook_client
    settings.set("GITHUB.WEBHOOK_SECRET", "operator-secret")

    response = client.post(
        "/api/v1/github_webhooks",
        content=BODY,
        headers={"x-hub-signature-256": _signature("<WEBHOOK SECRET>", BODY)},
    )

    assert response.status_code == 403
    handle_request.assert_not_awaited()
