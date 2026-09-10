import json

import pytest

from pr_agent.algo.utils import PRReviewIdentity
from pr_dashboard import comments, providers, registry, store


class TestCredentialStatus:
    def test_github_user_token_present(self, monkeypatch):
        """A configured GitHub user token reports configured with no secret in the detail"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "GITHUB.DEPLOYMENT_TYPE": "user",
            "GITHUB.USER_TOKEN": "ghp_supersecret",
        }.get(key, default))
        status = providers.credential_status("github")
        assert status.configured is True
        assert "ghp_supersecret" not in status.detail

    def test_github_token_missing(self, monkeypatch):
        """A missing GitHub token is reported per provider, not as a generic failure"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "GITHUB.DEPLOYMENT_TYPE": "user",
        }.get(key, default))
        status = providers.credential_status("github")
        assert status.configured is False
        assert "github" in status.detail.lower()

    def test_bitbucket_bearer_token(self, monkeypatch):
        """Bitbucket bearer auth is detected from BITBUCKET.BEARER_TOKEN and never echoed"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "BITBUCKET.AUTH_TYPE": "bearer",
            "BITBUCKET.BEARER_TOKEN": "secret",
        }.get(key, default))
        status = providers.credential_status("bitbucket")
        assert status.configured is True
        assert "secret" not in status.detail

    def test_bitbucket_basic_token(self, monkeypatch):
        """Bitbucket basic auth is detected from BITBUCKET.BASIC_TOKEN and never echoed"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "BITBUCKET.AUTH_TYPE": "basic",
            "BITBUCKET.BASIC_TOKEN": "secret",
        }.get(key, default))
        status = providers.credential_status("bitbucket")
        assert status.configured is True
        assert "secret" not in status.detail

    def test_bitbucket_token_missing(self, monkeypatch):
        """A missing Bitbucket token is reported per provider, not as a generic failure"""
        monkeypatch.setattr(providers, "_setting", lambda key, default=None: {
            "BITBUCKET.AUTH_TYPE": "bearer",
        }.get(key, default))
        status = providers.credential_status("bitbucket")
        assert status.configured is False
        assert "bitbucket" in status.detail.lower()

    def test_unknown_provider(self):
        """An unsupported provider is reported, not crashed on"""
        status = providers.credential_status("gitlab")
        assert status.configured is False
        assert "gitlab" in status.detail.lower()


class TestCache:
    def test_miss_then_hit(self, tmp_path):
        """A second call inside the TTL does not refetch"""
        conn = store.connect(tmp_path / "usage.db")
        calls = []

        def fetch():
            calls.append(1)
            return {"value": len(calls)}

        first, stale_first = providers.cached(conn, "k", 60, fetch)
        second, stale_second = providers.cached(conn, "k", 60, fetch)
        assert first == second == {"value": 1}
        assert stale_first is False and stale_second is False
        assert len(calls) == 1

    def test_expired_entry_refetches(self, tmp_path):
        """Past the TTL the value is refetched"""
        conn = store.connect(tmp_path / "usage.db")
        conn.execute(
            "INSERT INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
            ("k", "2020-01-01T00:00:00+00:00", "2020-01-01T00:01:00+00:00", json.dumps({"old": True})),
        )
        value, stale = providers.cached(conn, "k", 60, lambda: {"new": True})
        assert value == {"new": True}
        assert stale is False

    def test_stale_entry_served_when_fetch_fails(self, tmp_path):
        """When the provider is unreachable, expired data is served and flagged stale"""
        conn = store.connect(tmp_path / "usage.db")
        conn.execute(
            "INSERT INTO provider_cache (key, fetched_at, expires_at, payload) VALUES (?, ?, ?, ?)",
            ("k", "2020-01-01T00:00:00+00:00", "2020-01-01T00:01:00+00:00", json.dumps({"old": True})),
        )

        def fetch():
            raise providers.ProviderError("429 rate limited", status=429, retry_after="60")

        value, stale = providers.cached(conn, "k", 60, fetch)
        assert value == {"old": True}
        assert stale is True

    def test_fetch_failure_with_no_cache_raises(self, tmp_path):
        """With nothing cached, a provider failure surfaces instead of showing empty data"""
        conn = store.connect(tmp_path / "usage.db")

        def fetch():
            raise providers.ProviderError("401 unauthorized", status=401, retry_after=None)

        with pytest.raises(providers.ProviderError):
            providers.cached(conn, "k", 60, fetch)


class TestCommentFiltering:
    def test_only_pr_agent_comments_are_returned(self, monkeypatch):
        """Human comments are filtered out of the review list"""
        raw = [
            {"body": "looks good", "created_at": "2026-09-01T00:00:00Z", "html_url": "u1"},
            {"body": f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n- **Bug** `a.py` [10-12]",
             "created_at": "2026-09-02T00:00:00Z", "html_url": "u2"},
        ]
        monkeypatch.setattr(providers, "_fetch_github_issue_comments", lambda repo, number: raw)
        result, stale = providers.list_pr_agent_comments(registry.Repo("github", "o/r"), 1)
        assert [c.url for c in result] == ["u2"]
        assert result[0].kind is comments.CommentKind.REVIEW
        assert stale is False

    def test_cached_fetch_is_reused(self, tmp_path, monkeypatch):
        """Passing a connection caches the raw fetch, so a second call does not refetch"""
        calls = []
        raw = [{"body": f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n- **Bug** `a.py` [1-2]",
                "created_at": "2026-09-02T00:00:00Z", "html_url": "u2"}]

        def fake_fetch(repo, number):
            calls.append(1)
            return raw

        monkeypatch.setattr(providers, "_fetch_github_issue_comments", fake_fetch)
        conn = store.connect(tmp_path / "usage.db")
        repo = registry.Repo("github", "o/r")
        providers.list_pr_agent_comments(repo, 1, conn=conn)
        result, stale = providers.list_pr_agent_comments(repo, 1, conn=conn)
        assert len(calls) == 1
        assert len(result) == 1
        assert stale is False
