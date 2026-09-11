"""Tests for GET /findings — never call a real provider."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import findings_index, providers, registry, store
from pr_dashboard.registry import Repo

_ORIGIN = "http://127.0.0.1"


def _client(tmp_path, monkeypatch):
    registry_path = tmp_path / "pr_dashboard.toml"
    registry.save([Repo(provider="github", slug="o/r")], registry_path)
    monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
        provider, True, "configured"))
    application = app_module.create_app(registry_path=registry_path, db_path=tmp_path / "usage.db")
    return TestClient(application, base_url=_ORIGIN)


def _seed_cache(conn, repo: Repo, rows: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT OR REPLACE INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
        (
            f"findings_index:{repo.key}",
            now.isoformat(),
            (now + timedelta(seconds=120)).isoformat(),
            json.dumps({"rows": rows, "stale": False, "error": None}),
        ),
    )


class TestFindingsPage:
    def test_cold_cache_makes_zero_provider_calls(self, tmp_path, monkeypatch):
        """GET /findings on a cold cache renders immediately without calling list_pull_requests"""
        def fail(*_args, **_kwargs):
            pytest.fail("provider must not be called during the initial findings page render")

        monkeypatch.setattr(providers, "list_pull_requests", fail)
        monkeypatch.setattr(providers, "list_pr_agent_comments", fail)
        monkeypatch.setattr(findings_index, "refresh", lambda *_a, **_k: None)
        response = _client(tmp_path, monkeypatch).get("/findings")
        assert response.status_code == 200

    def test_loading_repository_polls(self, tmp_path, monkeypatch):
        """A loading repository shows a loading message and the page carries hx-trigger"""
        monkeypatch.setattr(findings_index, "refresh", lambda *_a, **_k: None)
        response = _client(tmp_path, monkeypatch).get("/findings")
        assert "still loading" in response.text
        assert "hx-trigger" in response.text

    def test_fresh_cache_does_not_poll(self, tmp_path, monkeypatch):
        """When every repository is fresh the page does not emit hx-trigger"""
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        repo = Repo(provider="github", slug="o/r")
        _seed_cache(conn, repo, [{
            "repo_key": repo.key, "provider": "github", "slug": "o/r", "pr_number": 1,
            "pr_title": "PR", "finding_title": "Issue", "relevant_file": "a.py",
            "line_start": 1, "line_end": 2, "command": "review",
        }])
        monkeypatch.setattr(findings_index, "refresh", lambda *_a, **_k: None)
        response = client.get("/findings")
        assert "hx-trigger" not in response.text

    def test_filters_narrow_the_table(self, tmp_path, monkeypatch):
        """Query-parameter filters keep only matching rows in the rendered table"""
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        repo = Repo(provider="github", slug="o/r")
        _seed_cache(conn, repo, [
            {
                "repo_key": repo.key, "provider": "github", "slug": "o/r", "pr_number": 1,
                "pr_title": "PR", "finding_title": "Race bug", "relevant_file": "a.py",
                "line_start": 1, "line_end": 2, "command": "review",
            },
            {
                "repo_key": repo.key, "provider": "github", "slug": "o/r", "pr_number": 2,
                "pr_title": "Other", "finding_title": "Docs gap", "relevant_file": None,
                "line_start": None, "line_end": None, "command": "improve",
            },
        ])
        monkeypatch.setattr(findings_index, "refresh", lambda *_a, **_k: None)
        response = client.get("/findings", params={"command": "review"})
        assert "Race bug" in response.text
        assert "Docs gap" not in response.text

    def test_most_recent_n_caveat_is_present(self, tmp_path, monkeypatch):
        """The page states that only the most recent N open pull requests per repository are covered"""
        monkeypatch.setattr(findings_index, "refresh", lambda *_a, **_k: None)
        response = _client(tmp_path, monkeypatch).get("/findings")
        assert "most recent" in response.text.lower()
        assert str(findings_index.MAX_INDEXED_PRS_PER_REPO) in response.text
