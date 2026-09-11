from fastapi.testclient import TestClient

from pr_agent.algo.utils import PRReviewIdentity, add_pr_review_identity, convert_to_markdown_v2
from pr_agent.config_loader import get_settings
from pr_dashboard import app as app_module
from pr_dashboard import comments, providers, registry


def _build_review_body() -> str:
    """Render a real review comment (via convert_to_markdown_v2) with two focus-area findings.

    Built from the actual renderer rather than hand-typed, per item 8/1 of the fix plan: a
    hand-typed body let a test pass even against a parse_findings gutted to `return []`,
    because pr_detail.html also dumps the raw comment body into a <pre> the test's assertions
    could match against instead of the parsed findings table.
    """
    review = {
        "key_issues_to_review": [
            {
                "relevant_file": "lib/profile.dart",
                "issue_header": "Race on profile write",
                "issue_content": "Race on profile write.",
                "start_line": 120,
                "end_line": 134,
            },
            {
                "relevant_file": "lib/ads.dart",
                "issue_header": "Missing ads timeout",
                "issue_content": "Missing ads timeout.",
                "start_line": 42,
                "end_line": 58,
            },
        ],
    }
    previous_layout = get_settings().get("pr_reviewer.findings_layout", "details")
    try:
        get_settings().set("pr_reviewer.findings_layout", "expanded")
        rendered = convert_to_markdown_v2({"review": review}, gfm_supported=True)
    finally:
        get_settings().set("pr_reviewer.findings_layout", previous_layout)
    return add_pr_review_identity(rendered, PRReviewIdentity.REGULAR.value)


REVIEW_BODY = _build_review_body()


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

    registry_path = tmp_path / "pr_dashboard.toml"
    application = app_module.create_app(
        registry_path=registry_path, db_path=tmp_path / "usage.db")
    registry.add(registry.Repo(provider="github", slug="samer2373/block_rush"), registry_path)
    return TestClient(application)


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

    def test_two_repo_failures_are_both_shown_and_attributed(self, tmp_path, monkeypatch):
        """Two repos failing with different provider errors both appear, each attributed to its own repo"""
        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))

        def fake_pulls(repo, state="open", limit=50, conn=None):
            # Deliberately NOT naming the repo in the message: the attribution asserted
            # below must come from the route's own errors.append(f"{repo.key}: {exc}"),
            # not from text the provider happened to include.
            raise providers.ProviderError("exploded")

        monkeypatch.setattr(providers, "list_pull_requests", fake_pulls)
        registry_path = tmp_path / "pr_dashboard.toml"
        application = app_module.create_app(
            registry_path=registry_path, db_path=tmp_path / "usage.db")
        client = TestClient(application)
        registry.add(registry.Repo(provider="github", slug="samer2373/block_rush"), registry_path)
        registry.add(registry.Repo(provider="github", slug="other/repo"), registry_path)
        response = client.get("/")
        assert "github:samer2373/block_rush: exploded" in response.text
        assert "github:other/repo: exploded" in response.text

    def test_zero_tokens_renders_as_zero_not_dash(self, tmp_path, monkeypatch):
        """A repo whose 7-day runs recorded zero tokens shows 0, not the no-data dash"""
        from datetime import datetime, timezone

        from pr_dashboard import store

        client = _client(tmp_path, monkeypatch, pulls=[
            providers.PullRequestSummary(1, "Add rush mode", "samer2373", "open",
                                         "https://github.com/samer2373/block_rush/pull/1", "2026-09-10T10:00:00")
        ])
        conn = store.connect(tmp_path / "usage.db")
        now = datetime.now(timezone.utc).isoformat()
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug="samer2373/block_rush", pr_number=None, started_at=now)
        conn.execute("UPDATE runs SET status='ok', total_tokens=0 WHERE id=?", (run_id,))
        response = client.get("/")
        # Assert the cell directly rather than counting em-dashes on the whole page: the
        # "Reviewed" column is a second data-backed column now (see reviewed_prs below).
        assert "<td>0</td>" in response.text

    def test_reviewed_prs_counts_distinct_pull_requests_per_repo(self, tmp_path, monkeypatch):
        """Reviewed counts distinct PR numbers touched in the 7-day window, per repository"""
        from datetime import datetime, timezone

        from pr_dashboard import store

        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))
        registry_path = tmp_path / "pr_dashboard.toml"
        application = app_module.create_app(
            registry_path=registry_path, db_path=tmp_path / "usage.db")
        client = TestClient(application)
        monkeypatch.setattr(providers, "list_pull_requests",
                            lambda repo, state="open", limit=50, conn=None: ([], False))
        registry.add(registry.Repo(provider="github", slug="samer2373/block_rush"), registry_path)
        registry.add(registry.Repo(provider="github", slug="other/repo"), registry_path)

        conn = store.connect(tmp_path / "usage.db")
        now = datetime.now(timezone.utc).isoformat()
        # Two runs against the same PR (#1) and one run against a different PR (#2), all for
        # samer2373/block_rush: reviewed must count the distinct PR numbers (2), not the runs
        # (3). One run with no PR number must not be counted at all.
        for pr_number in (1, 1, 2):
            store.start_run(
                conn, provider="github", command="review", pr_url=None,
                repo_slug="samer2373/block_rush", pr_number=pr_number, started_at=now)
        store.start_run(
            conn, provider="github", command="ask", pr_url=None,
            repo_slug="samer2373/block_rush", pr_number=None, started_at=now)
        # A run against the other registered repo must not bleed into block_rush's count.
        store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug="other/repo", pr_number=9, started_at=now)

        response = client.get("/")
        rows = response.text.split("<tbody>")[1].split("</tbody>")[0].split("<tr>")[1:]
        by_repo = {}
        for row in rows:
            cells = row.split("<td>")
            slug = cells[1].split("</a>")[0].split(">")[-1]
            reviewed = cells[3].split("</td>")[0]
            by_repo[slug] = reviewed
        assert by_repo["samer2373/block_rush"] == "2"
        assert by_repo["other/repo"] == "1"

    def test_open_prs_beyond_the_display_limit_shows_plus(self, tmp_path, monkeypatch):
        """A repo with more than 50 open PRs shows "50+", not the silently truncated count"""
        pulls = [
            providers.PullRequestSummary(i, f"PR {i}", "samer2373", "open",
                                         f"https://github.com/samer2373/block_rush/pull/{i}",
                                         "2026-09-10T10:00:00")
            for i in range(1, 52)
        ]
        client = _client(tmp_path, monkeypatch, pulls=pulls)
        response = client.get("/")
        assert "<td>50+</td>" in response.text
        assert "<td>51</td>" not in response.text

    def test_open_prs_at_the_display_limit_shows_the_exact_count(self, tmp_path, monkeypatch):
        """Exactly 50 open PRs shows 50, not 50+ -- the boundary is not truncated"""
        pulls = [
            providers.PullRequestSummary(i, f"PR {i}", "samer2373", "open",
                                         f"https://github.com/samer2373/block_rush/pull/{i}",
                                         "2026-09-10T10:00:00")
            for i in range(1, 51)
        ]
        client = _client(tmp_path, monkeypatch, pulls=pulls)
        response = client.get("/")
        assert "<td>50</td>" in response.text
        assert "50+" not in response.text

    def test_repo_registered_with_different_case_still_matches_its_runs(self, tmp_path, monkeypatch):
        """A repo registered as Owner/Repo still finds runs recorded as owner/repo"""
        from datetime import datetime, timezone

        from pr_dashboard import store

        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))
        monkeypatch.setattr(providers, "list_pull_requests",
                            lambda repo, state="open", limit=50, conn=None: ([], False))
        registry_path = tmp_path / "pr_dashboard.toml"
        application = app_module.create_app(
            registry_path=registry_path, db_path=tmp_path / "usage.db")
        client = TestClient(application)
        registry.add(registry.Repo(provider="github", slug="Samer2373/Block_Rush"), registry_path)

        conn = store.connect(tmp_path / "usage.db")
        now = datetime.now(timezone.utc).isoformat()
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug="samer2373/block_rush", pr_number=1, started_at=now)
        conn.execute("UPDATE runs SET status='ok', total_tokens=500, total_cost_usd='0.05' WHERE id=?", (run_id,))

        response = client.get("/")
        assert "<td>500</td>" in response.text
        assert "0.05" in response.text
        assert "<td>1</td>" in response.text  # reviewed_prs

    def test_corrupt_cost_row_does_not_500_the_overview(self, tmp_path, monkeypatch):
        """A malformed total_cost_usd value is guarded, not raised as an unhandled 500"""
        from datetime import datetime, timezone

        from pr_dashboard import store

        client = _client(tmp_path, monkeypatch)
        conn = store.connect(tmp_path / "usage.db")
        now = datetime.now(timezone.utc).isoformat()
        run_id = store.start_run(
            conn, provider="github", command="review", pr_url=None,
            repo_slug="samer2373/block_rush", pr_number=1, started_at=now)
        conn.execute(
            "UPDATE runs SET status='ok', total_tokens=10, total_cost_usd='not-a-decimal' WHERE id=?",
            (run_id,))
        response = client.get("/")
        assert response.status_code == 200

    def test_stale_cache_is_labelled(self, tmp_path, monkeypatch):
        """Data served from an expired cache is shown with a stale banner, not silently"""
        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))
        registry_path = tmp_path / "pr_dashboard.toml"
        application = app_module.create_app(
            registry_path=registry_path, db_path=tmp_path / "usage.db")
        client = TestClient(application)
        registry.add(registry.Repo(provider="github", slug="o/r"), registry_path)
        monkeypatch.setattr(providers, "list_pull_requests",
                            lambda repo, state="open", limit=50, conn=None: ([], True))
        response = client.get("/")
        assert "cached data" in response.text.lower()

    def test_fresh_data_is_not_labelled_stale(self, tmp_path, monkeypatch):
        """Fresh data carries no stale banner, so the banner is conditional and not decoration"""
        # The negative case is the half that proves the banner means something: a template
        # that always rendered it would satisfy test_stale_cache_is_labelled on its own.
        monkeypatch.setattr(providers, "credential_status", lambda provider: providers.CredentialStatus(
            provider, True, "configured"))
        registry_path = tmp_path / "pr_dashboard.toml"
        application = app_module.create_app(
            registry_path=registry_path, db_path=tmp_path / "usage.db")
        client = TestClient(application)
        registry.add(registry.Repo(provider="github", slug="o/r"), registry_path)
        monkeypatch.setattr(providers, "list_pull_requests",
                            lambda repo, state="open", limit=50, conn=None: ([], False))
        response = client.get("/")
        assert "cached data" not in response.text.lower()


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
        # Scope the assertion to the Findings table region: the raw comment body is also
        # dumped into a <pre> under Comments and contains the same text, so asserting against
        # the whole page would pass even with parse_findings gutted to `return []`.
        findings_section = response.text.split("<h2>Findings</h2>", 1)[1].split("<h2>Runs</h2>", 1)[0]
        assert "Race on profile write" in findings_section
        assert "lib/profile.dart" in findings_section
        assert "120" in findings_section and "134" in findings_section

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
        """A provider/slug combination that was never registered under that provider returns 404"""
        client = _client(tmp_path, monkeypatch)
        assert client.get("/repos/gitlab/samer2373/block_rush").status_code == 404

    def test_invalid_slug_path_is_404(self, tmp_path, monkeypatch):
        """A single-segment slug, which was never registered, returns 404"""
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
