"""CSRF and Origin/Host guard tests for the PR-Agent dashboard."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import providers, registry

_ORIGIN = "http://127.0.0.1"
_SAFE_HEADERS = {"Origin": _ORIGIN}


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        providers,
        "credential_status",
        lambda provider: providers.CredentialStatus(provider, True, f"{provider} token configured"),
    )
    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
    return TestClient(application, base_url=_ORIGIN)


def _csrf_token(client: TestClient) -> str:
    response = client.get("/repos")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, "csrf_token input missing from /repos page"
    return match.group(1)


def _registered_slugs(tmp_path) -> set[str]:
    path = tmp_path / "pr_dashboard.toml"
    if not path.exists():
        return set()
    return {repo.slug for repo in registry.load(path)}


class TestRequireSafeRequest:
    def test_foreign_origin_is_refused_and_adds_nothing(self, tmp_path, monkeypatch):
        """A foreign Origin is 403 and the repository is not added"""
        client = _client(tmp_path, monkeypatch)
        token = _csrf_token(client)
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "evil/repo", "csrf_token": token},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert "evil/repo" not in _registered_slugs(tmp_path)

    def test_missing_origin_is_refused_and_adds_nothing(self, tmp_path, monkeypatch):
        """A missing Origin is 403 and the repository is not added"""
        client = _client(tmp_path, monkeypatch)
        token = _csrf_token(client)
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "no/origin", "csrf_token": token},
        )
        assert response.status_code == 403
        assert "no/origin" not in _registered_slugs(tmp_path)

    def test_missing_token_is_refused_and_adds_nothing(self, tmp_path, monkeypatch):
        """A missing csrf_token is 403 and the repository is not added"""
        client = _client(tmp_path, monkeypatch)
        _csrf_token(client)  # mint session cookie
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "no/token"},
            headers=_SAFE_HEADERS,
        )
        assert response.status_code == 403
        assert "no/token" not in _registered_slugs(tmp_path)

    def test_wrong_token_is_refused_and_adds_nothing(self, tmp_path, monkeypatch):
        """A wrong csrf_token is 403 and the repository is not added"""
        client = _client(tmp_path, monkeypatch)
        _csrf_token(client)
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "bad/token", "csrf_token": "not-the-real-token"},
            headers=_SAFE_HEADERS,
        )
        assert response.status_code == 403
        assert "bad/token" not in _registered_slugs(tmp_path)

    def test_valid_same_origin_request_with_token_passes(self, tmp_path, monkeypatch):
        """A same-origin POST with the session csrf_token registers the repository"""
        client = _client(tmp_path, monkeypatch)
        token = _csrf_token(client)
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "ok/repo", "csrf_token": token},
            headers=_SAFE_HEADERS,
        )
        assert response.status_code == 200
        assert "ok/repo" in response.text
        assert "ok/repo" in _registered_slugs(tmp_path)

    def test_existing_post_repos_still_works(self, tmp_path, monkeypatch):
        """The S1+S2 POST /repos flow still works when the CSRF token is supplied"""
        client = _client(tmp_path, monkeypatch)
        token = _csrf_token(client)
        response = client.post(
            "/repos",
            data={"provider": "github", "slug": "samer2373/block_rush", "csrf_token": token},
            headers=_SAFE_HEADERS,
        )
        assert response.status_code == 200
        assert "samer2373/block_rush" in response.text
        follow_up = client.get("/repos")
        assert "samer2373/block_rush" in follow_up.text
