import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.testclient import TestClient

import pr_agent.servers.azuredevops_server_webhook as azure_webhook


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", "admin")
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", "s3cret")

    router = APIRouter()

    @router.post("/", dependencies=[Depends(azure_webhook.authorize)])
    async def _hook():
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_missing_authorization_header_is_rejected_with_401(client):
    """Reject a request that carries no Authorization header with 401, since
    HTTPBasic(auto_error=False) yields None rather than raising."""
    response = client.post("/")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Basic"


def test_wrong_credentials_are_rejected_with_401(client):
    response = client.post("/", auth=("admin", "wrong"))

    assert response.status_code == 401


def test_correct_credentials_are_accepted(client):
    response = client.post("/", auth=("admin", "s3cret"))

    assert response.status_code == 200


def test_auth_is_skipped_when_no_credentials_are_configured(monkeypatch):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", None)
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", None)

    router = APIRouter()

    @router.post("/", dependencies=[Depends(azure_webhook.authorize)])
    async def _hook():
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)

    assert TestClient(app, raise_server_exceptions=False).post("/").status_code == 200
