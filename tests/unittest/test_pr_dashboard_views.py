from fastapi.testclient import TestClient

from pr_agent.algo.utils import PRReviewIdentity
from pr_dashboard import app as app_module
from pr_dashboard import comments, providers, registry

REVIEW_BODY = (
    f"{PRReviewIdentity.REGULAR.value}\n"
    "## PR Reviewer Guide\n\n"
    "- **Race on profile write** `lib/profile.dart` [120-134]\n"
    "- **Missing ads timeout** `lib/ads.dart` [42-58]\n"
)


def _client(tmp_path, monkeypatch, *, pulls=None, review_comments=None, error=None):
    monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
        provider, True, "configured"))

    def fake_pulls(repo, state="open", limit=50, conn=None):
        if error:
            raise error
        return pulls or [], False

    def fake_comments(repo, number, conn=None):
        if error:
            raise error
        return review_comments or [], False

    monkeypatch.setattr(providers, "list_pull_requests", fake_pulls)
    monkeypatch.setattr(providers, "list_pr_agent_comments", fake_comments)

    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
    client = TestClient(application)
    client.post("/repos", data={"provider": "github", "slug": "samer2373/block_rush"})
    return client


class TestOverview:
    def test_lists_every_registered_repository(self, tmp_path, monkeypatch):
        """The overview shows one card per registered repository"""
        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "Add rush mode", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        response = client.get("/")
        assert response.status_code == 200
        assert "samer2373/block_rush" in response.text
        assert "cached data" not in response.text.lower()

    def test_repo_card_aggregates_tokens_and_cost_over_7_days(self, tmp_path, monkeypatch):
        """The overview card sums tokens and Decimal cost across runs for the repository"""
        from datetime import datetime, timezone

        from pr_dashboard import store

        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        now = datetime.now(timezone.utc).isoformat()
        for tokens, cost in ((1000, "0.01"), (2000, "0.02")):
            run_id = store.start_run(
                conn, provider="github", command="review", pr_url=None,
                repo_slug="samer2373/block_rush", pr_number=None, started_at=now)
            conn.execute(
                "UPDATE runs SET status='ok', total_tokens=?, total_cost_usd=? WHERE id=?",
                (tokens, cost, run_id))
        response = client.get("/")
        assert "3000" in response.text
        assert "0.03" in response.text

    def test_provider_error_renders_a_banner(self, tmp_path, monkeypatch):
        """A provider failure shows a banner instead of a stack trace"""
        client = _client(tmp_path, monkeypatch,
                         error=providers.ProviderError("github returned 429", status=429, retry_after="60"))
        response = client.get("/")
        assert response.status_code == 200
        assert "429" in response.text
        assert "Traceback" not in response.text

    def test_stale_cache_is_labelled(self, tmp_path, monkeypatch):
        """Data served from an expired cache is shown with a stale banner, not silently"""
        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))
        application = app_module.create_app(
            registry_path=tmp_path / "pr_dashboard.toml", db_path=tmp_path / "usage.db")
        client = TestClient(application)
        client.post("/repos", data={"provider": "github", "slug": "o/r"})
        monkeypatch.setattr(providers, "list_pull_requests",
                            lambda repo, state="open", limit=50, conn=None: ([], True))
        response = client.get("/")
        assert "cached data" in response.text.lower()


class TestRepoDetail:
    def test_lists_pull_requests(self, tmp_path, monkeypatch):
        """The repository page lists pull requests with title and author"""
        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "Add rush mode", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        response = client.get("/repos/github/samer2373/block_rush")
        assert "Add rush mode" in response.text
        assert "samer2373" in response.text


class TestPrDetail:
    def test_renders_findings_with_file_and_lines(self, tmp_path, monkeypatch):
        """The pull request page shows each finding with its file path and line range"""
        client = _client(tmp_path, monkeypatch, review_comments=[
            providers.ReviewComment(kind=comments.CommentKind.REVIEW, body=REVIEW_BODY,
                                    created_at="2026-09-10T10:00:00", url="https://example/c1")
        ])
        response = client.get("/pr/github/samer2373/block_rush/1")
        assert response.status_code == 200
        assert "Race on profile write" in response.text
        assert "lib/profile.dart" in response.text
        assert "120" in response.text and "134" in response.text

    def test_run_history_for_the_pull_request(self, tmp_path, monkeypatch):
        """Recorded runs for this pull request appear on its page"""
        from pr_dashboard import store
        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        run_id = store.start_run(
            conn, provider="github", command="review",
            pr_url="https://github.com/samer2373/block_rush/pull/1",
            repo_slug="samer2373/block_rush", pr_number=1, started_at="2026-09-10T10:00:00+00:00")
        conn.execute("UPDATE runs SET status='ok', total_tokens=4321, model_used='gemini/flash' WHERE id=?",
                     (run_id,))
        response = client.get("/pr/github/samer2373/block_rush/1")
        assert "4321" in response.text
        assert "gemini/flash" in response.text

    def test_unregistered_repository_is_404(self, tmp_path, monkeypatch):
        """A pull request under an unregistered repository is not found"""
        client = _client(tmp_path, monkeypatch)
        assert client.get("/pr/github/someone/else/1").status_code == 404


class TestPathValidation:
    def test_unsupported_provider_path_is_404(self, tmp_path, monkeypatch):
        """A provider outside SUPPORTED_PROVIDERS never reaches the registry lookup as a match"""
        client = _client(tmp_path, monkeypatch)
        assert client.get("/repos/gitlab/samer2373/block_rush").status_code == 404

    def test_invalid_slug_path_is_404(self, tmp_path, monkeypatch):
        """A slug that fails SLUG_PATTERN never reaches the registry lookup as a match"""
        client = _client(tmp_path, monkeypatch)
        assert client.get("/repos/github/no-owner-segment").status_code == 404

    def test_guard_rejects_even_an_entry_the_registry_loop_would_match(self, tmp_path, monkeypatch):
        """The explicit provider/slug check runs even if registry.load ever returned unvalidated data"""
        client = _client(tmp_path, monkeypatch)
        monkeypatch.setattr(registry, "load", lambda path: [registry.Repo(provider="gitlab", slug="o/r")])
        response = client.get("/repos/gitlab/o/r")
        assert response.status_code == 404

    def test_script_in_pr_title_is_escaped(self, tmp_path, monkeypatch):
        """A malicious PR title is rendered escaped, never as raw HTML"""
        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "<script>alert(1)</script>", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        response = client.get("/repos/github/samer2373/block_rush")
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
