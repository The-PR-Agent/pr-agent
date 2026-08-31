"""Reject a blank /ask before it dispatches a command with no question attached."""
import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.testclient import TestClient
from starlette_context.middleware import RawContextMiddleware

import pr_agent.servers.gerrit_server as gerrit_server


class SettingsStub:
    """No webhook credentials configured, so authorize() lets the request through."""

    def get(self, key, default=None):
        if key == "gerrit":
            return {"webhook_username": "", "webhook_password": ""}
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
    )

    assert response.status_code == 400
    assert calls == []


def test_dispatch_an_ask_with_the_question_taken_from_msg(client):
    http, calls = client

    response = http.post(
        "/api/v1/gerrit/ask",
        json={"refspec": "refs/changes/1", "project": "p", "msg": "  why is this slow? "},
    )

    assert response.status_code == 200
    assert calls == [("p:refs/changes/1", "/ask why is this slow?")]
