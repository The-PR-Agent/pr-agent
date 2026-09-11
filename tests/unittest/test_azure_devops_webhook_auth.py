import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

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
@pytest.mark.parametrize("unset", [None, ""])
async def test_auth_is_skipped_when_no_credentials_are_configured(monkeypatch, unset):
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", unset)
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", unset)

    response = await _post(_build_app())

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("username, password", [("admin", None), (None, "s3cret"), ("admin", "")])
async def test_a_half_configured_pair_fails_closed(monkeypatch, username, password):
    """Only one credential set is a misconfiguration, not a request to skip auth."""
    monkeypatch.setattr(azure_webhook, "WEBHOOK_USERNAME", username)
    monkeypatch.setattr(azure_webhook, "WEBHOOK_PASSWORD", password)
    app = _build_app()

    assert (await _post(app)).status_code == 500
    assert (await _post(app, auth=("admin", "s3cret"))).status_code == 500


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
