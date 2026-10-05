from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.middleware import Middleware
from starlette_context.middleware import RawContextMiddleware

import pr_agent.servers.azuredevops_server_webhook as azure_webhook


def _build_app():
    router = APIRouter()

    @router.post("/", dependencies=[Depends(azure_webhook.authorize)])
    async def _hook():
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", "admin")
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", "s3cret")
    return _build_app()


async def _post(app, **kwargs):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/", **kwargs)


@pytest.mark.asyncio
async def test_missing_authorization_header_is_rejected_with_401(app):
    """Reject a request that carries no Authorization header with 401, since
    HTTPBasic(auto_error=False) yields None rather than raising."""
    response = await _post(app)

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Basic"


@pytest.mark.asyncio
async def test_wrong_credentials_are_rejected_with_401(app):
    response = await _post(app, auth=("admin", "wrong"))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_correct_credentials_are_accepted(app):
    response = await _post(app, auth=("admin", "s3cret"))

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("username, password", [
    (None, None), ("", ""), ("admin", None), (None, "s3cret"), ("admin", ""), ("", "s3cret"),
])
async def test_an_unconfigured_pair_rejects_requests_before_dispatch(monkeypatch, username, password):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", username)
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", password)
    dispatch = AsyncMock()
    logger = Mock()
    monkeypatch.setattr(azure_webhook, "handle_request_azure", dispatch)
    monkeypatch.setattr(azure_webhook, "get_logger", lambda: logger)
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(azure_webhook.router)

    for auth in (None, ("admin", "s3cret")):
        response = await _post(app, json={"eventType": "git.pullrequest.created"}, auth=auth)
        assert response.status_code == 403
        assert response.json() == {"detail": "Webhook authentication is not configured."}
    dispatch.assert_not_awaited()
    assert logger.error.call_count == 2
    for call in logger.error.call_args_list:
        assert "azure_devops_server.webhook_username" in call.args[0]
        assert "azure_devops_server.webhook_password" in call.args[0]


@pytest.mark.asyncio
async def test_a_non_ascii_configured_password_rejects_with_401_not_500(monkeypatch):
    """secrets.compare_digest raises TypeError on non-ASCII str, which turned every
    authenticated call into a 500 once the configured password held such a character.
    FastAPI decodes the header as ASCII, so such a password can never match; the
    contract is a clean 401."""
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", "admin")
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", "şifre")

    response = await _post(_build_app(), auth=("admin", "sifre"))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_non_string_configured_credential_is_compared_as_text(monkeypatch):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", "admin")
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", 123456)
    app = _build_app()

    assert (await _post(app, auth=("admin", "123456"))).status_code == 200
    assert (await _post(app, auth=("admin", "654321"))).status_code == 401
