"""Reject a blank /ask before it dispatches a command with no question attached."""
import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

import pr_agent.servers.gerrit_server as gerrit_server

AUTH = ("admin", "s3cret")


class SettingsStub:
    """Webhook credentials configured, since authorize() rejects every call without them."""

    def get(self, key, default=None):
        if key == "gerrit":
            return {"webhook_username": AUTH[0], "webhook_password": AUTH[1]}
        return default


class FakeAgent:
    def __init__(self, calls):
        self.calls = calls

    async def handle_request(self, url, body):
        self.calls.append((url, body))


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(gerrit_server, "get_settings", SettingsStub)
    monkeypatch.setattr(gerrit_server, "PRAgent", lambda: FakeAgent(calls))
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(gerrit_server.router)
    return TestClient(app, raise_server_exceptions=False), calls


@pytest.mark.parametrize("msg", ["", " ", "\t\n  "])
def test_reject_an_ask_whose_msg_is_only_whitespace(client, msg):
    http, calls = client

    response = http.post(
        "/api/v1/gerrit/ask",
        json={"refspec": "refs/changes/1", "project": "p", "msg": msg},
        auth=AUTH,
    )

    assert response.status_code == 400
    assert calls == []


def test_dispatch_an_ask_with_the_question_taken_from_msg(client):
    http, calls = client

    response = http.post(
        "/api/v1/gerrit/ask",
        json={"refspec": "refs/changes/1", "project": "p", "msg": "  why is this slow? "},
        auth=AUTH,
    )

    assert response.status_code == 200
    assert calls == [("p:refs/changes/1", "/ask why is this slow?")]


@pytest.mark.parametrize("action", ["review", "describe", "improve", "answer"])
def test_dispatch_a_supported_action(client, action):
    http, calls = client

    response = http.post(
        f"/api/v1/gerrit/{action}",
        json={"refspec": "refs/changes/1", "project": "p"},
        auth=AUTH,
    )

    assert response.status_code == 200
    assert calls == [("p:refs/changes/1", f"/{action}")]


def test_reject_reflect_before_constructing_an_agent(client, monkeypatch):
    http, calls = client

    def construct_agent():
        raise AssertionError("PRAgent must not be constructed for an unsupported action")

    monkeypatch.setattr(gerrit_server, "PRAgent", construct_agent)

    response = http.post(
        "/api/v1/gerrit/reflect",
        json={"refspec": "refs/changes/1", "project": "p"},
        auth=AUTH,
    )

    assert response.status_code == 422
    assert calls == []


def test_openapi_does_not_advertise_reflect(client):
    http, _ = client

    response = http.get("/openapi.json")

    assert response.status_code == 200
    action_schema = response.json()["components"]["schemas"]["Action"]
    assert set(action_schema["enum"]) == {"review", "describe", "ask", "improve", "answer"}
