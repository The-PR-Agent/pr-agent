"""Require credentials on the Gerrit webhook endpoint, which runs arbitrary commands."""
import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

import pr_agent.servers.gerrit_server as gerrit_server

PAYLOAD = {"refspec": "refs/changes/1", "project": "p", "msg": "review"}


class SettingsStub:
    def __init__(self, username, password):
        self._values = {"gerrit": {"webhook_username": username, "webhook_password": password}}

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeAgent:
    def __init__(self, calls):
        self.calls = calls

    async def handle_request(self, url, body):
        self.calls.append((url, body))


def build_client(monkeypatch, username, password):
    calls = []
    monkeypatch.setattr(gerrit_server, "get_settings", lambda: SettingsStub(username, password))
    monkeypatch.setattr(gerrit_server, "PRAgent", lambda: FakeAgent(calls))
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(gerrit_server.router)
    return TestClient(app, raise_server_exceptions=False), calls


@pytest.fixture
def secured_client(monkeypatch):
    return build_client(monkeypatch, "admin", "s3cret")


def test_reject_an_unauthenticated_request(secured_client):
    client, calls = secured_client

    response = client.post("/api/v1/gerrit/review", json=PAYLOAD)

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Basic"
    assert calls == []


def test_reject_wrong_credentials(secured_client):
    client, calls = secured_client

    response = client.post("/api/v1/gerrit/review", json=PAYLOAD, auth=("admin", "wrong"))

    assert response.status_code == 401
    assert calls == []


def test_accept_correct_credentials(secured_client):
    client, calls = secured_client

    response = client.post("/api/v1/gerrit/review", json=PAYLOAD, auth=("admin", "s3cret"))

    assert response.status_code == 200
    assert calls


def test_an_unconfigured_deployment_keeps_the_previous_behaviour(monkeypatch):
    client, calls = build_client(monkeypatch, "", "")

    response = client.post("/api/v1/gerrit/review", json=PAYLOAD)

    assert response.status_code == 200
    assert calls


@pytest.mark.parametrize("username, password", [("admin", ""), ("", "s3cret")])
def test_reject_a_half_configured_deployment(monkeypatch, username, password):
    """Fail closed when only one credential is set, rather than reverting to open access."""
    client, calls = build_client(monkeypatch, username, password)

    response = client.post("/api/v1/gerrit/review", json=PAYLOAD)

    assert response.status_code == 500
    assert response.json() == {"detail": "Webhook authentication is misconfigured."}
    assert calls == []


@pytest.mark.parametrize("username, password", [("admin", ""), ("", "s3cret")])
def test_the_misconfiguration_response_names_no_settings(monkeypatch, username, password):
    """Keep configuration key names out of a response an unauthenticated caller can read."""
    client, _ = build_client(monkeypatch, username, password)

    body = client.post("/api/v1/gerrit/review", json=PAYLOAD).text

    assert "webhook_username" not in body
    assert "webhook_password" not in body
