import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import providers

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        providers,
        "credential_status",
        lambda provider: providers.CredentialStatus(provider, True, f"{provider} token configured"),
    )
    application = app_module.create_app(registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
    return TestClient(application)


class TestReposPage:
    def test_empty_registry_renders_guidance(self, tmp_path, monkeypatch):
        """With nothing registered the page explains how to add a repository"""
        response = _client(tmp_path, monkeypatch).get("/repos")
        assert response.status_code == 200
        assert "add a repository" in response.text.lower()
        assert "samer2373/block_rush" not in response.text

    def test_add_repository(self, tmp_path, monkeypatch):
        """Posting a valid repository registers it and it appears in the list"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={"provider": "github", "slug": "samer2373/block_rush"})
        assert response.status_code == 200
        assert "samer2373/block_rush" in response.text
        # And it persists across a fresh GET, not just in the POST response fragment.
        follow_up = client.get("/repos")
        assert "samer2373/block_rush" in follow_up.text

    def test_add_invalid_slug_shows_the_error(self, tmp_path, monkeypatch):
        """An invalid slug is reported in the page, not raised as a 500"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={"provider": "github", "slug": "no-owner"})
        assert response.status_code == 200
        assert "owner/name" in response.text
        # The rejected entry must not have been registered.
        follow_up = client.get("/repos")
        assert "no-owner" not in follow_up.text

    def test_add_unsupported_provider_shows_the_error(self, tmp_path, monkeypatch):
        """An unsupported provider is reported in the page, not raised as a 500"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={"provider": "gitlab", "slug": "o/r"})
        assert response.status_code == 200
        assert "gitlab" in response.text

    def test_add_missing_fields_does_not_500(self, tmp_path, monkeypatch):
        """A malformed post (missing fields) is handled, not a 500 from a KeyError"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos", data={})
        assert response.status_code == 200
        assert "unsupported provider" in response.text

    def test_delete_repository(self, tmp_path, monkeypatch):
        """A registered repository can be removed"""
        client = _client(tmp_path, monkeypatch)
        client.post("/repos", data={"provider": "github", "slug": "o/r"})
        response = client.post("/repos/github/o/r/delete")
        assert response.status_code == 200
        assert "o/r" not in response.text
        # Confirm it is actually gone from the registry, not just this response fragment.
        follow_up = client.get("/repos")
        assert "o/r" not in follow_up.text

    def test_delete_unknown_repository_shows_the_error(self, tmp_path, monkeypatch):
        """Deleting a repository that was never registered is reported, not a 500"""
        client = _client(tmp_path, monkeypatch)
        response = client.post("/repos/github/no/such/delete")
        assert response.status_code == 200
        assert "not registered" in response.text.lower()

    def test_credential_status_is_shown_without_the_token(self, tmp_path, monkeypatch):
        """The page reports credential state and never renders the secret"""
        monkeypatch.setattr(
            providers,
            "credential_status",
            lambda provider: providers.CredentialStatus(
                provider, False, "no token configured for bitbucket; set BITBUCKET.BEARER_TOKEN in .secrets.toml"
            ),
        )
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db"
        )
        client = TestClient(application)
        client.post("/repos", data={"provider": "bitbucket", "slug": "team/svc"})
        response = client.get("/repos")
        assert "no token configured for bitbucket" in response.text

    def test_configured_credentials_never_render_the_token(self, tmp_path, monkeypatch):
        """A configured provider shows its status without echoing the token value"""
        monkeypatch.setattr(
            providers,
            "_setting",
            lambda key, default=None: {
                "BITBUCKET.AUTH_TYPE": "bearer",
                "BITBUCKET.BEARER_TOKEN": "s3cr3t-sentinel-token",
            }.get(key, default),
        )
        client = TestClient(
            app_module.create_app(registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
        )
        client.post("/repos", data={"provider": "bitbucket", "slug": "team/svc"})
        response = client.get("/repos")
        assert "bitbucket bearer token configured" in response.text
        assert "s3cr3t-sentinel-token" not in response.text


class TestCreateApp:
    def test_two_apps_do_not_share_state(self, tmp_path, monkeypatch):
        """Each create_app call is isolated to its own registry and db paths"""
        monkeypatch.setattr(
            providers,
            "credential_status",
            lambda provider: providers.CredentialStatus(provider, True, f"{provider} token configured"),
        )
        app_a = app_module.create_app(registry_path=tmp_path / "a.toml", db_path=tmp_path / "a.db")
        app_b = app_module.create_app(registry_path=tmp_path / "b.toml", db_path=tmp_path / "b.db")
        client_a = TestClient(app_a)
        client_b = TestClient(app_b)
        client_a.post("/repos", data={"provider": "github", "slug": "only/inA"})
        assert "only/inA" in client_a.get("/repos").text
        assert "only/inA" not in client_b.get("/repos").text


class TestWheelPackaging:
    def test_wheel_ships_templates_and_static(self, tmp_path):
        """A built wheel actually contains the templates and static assets create_app() needs.

        This is the packaging failure class from Task 3: packages.find only discovers Python
        packages, so templates/*.html and static/* silently disappear from a real install unless
        [tool.setuptools.package-data] names them. A test that only re-reads pyproject.toml would
        assert a string equals itself; this builds the actual wheel and inspects its contents.
        """
        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; cannot build a wheel to inspect")

        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.skip(f"uv build failed in this environment, cannot verify wheel contents: {result.stderr[-500:]}")

        wheels = list(tmp_path.glob("*.whl"))
        assert wheels, "uv build reported success but produced no .whl file"

        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()

        assert "pr_dashboard/templates/base.html" in names
        assert "pr_dashboard/templates/repos.html" in names
        assert "pr_dashboard/static/htmx.min.js" in names
