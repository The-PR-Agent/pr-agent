"""Tests for the cross-repo findings index — never call a real provider."""
from __future__ import annotations

import threading
import time

import pytest

from pr_agent.algo.utils import PRReviewIdentity, add_pr_review_identity, convert_to_markdown_v2
from pr_agent.config_loader import get_settings
from pr_dashboard import comments, providers, store
from pr_dashboard.findings_index import (
    MAX_INDEXED_PRS_PER_REPO,
    REPO_DEADLINE_SECONDS,
    _db_key,
    _refresh_one_repo,
    filter_rows,
    index_snapshot,
    refresh,
)
from pr_dashboard.registry import Repo


def _review_body(title: str, file_path: str, start: int, end: int) -> str:
    review = {
        "key_issues_to_review": [
            {
                "relevant_file": file_path,
                "issue_header": title,
                "issue_content": title,
                "start_line": start,
                "end_line": end,
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


def _pull(number: int, title: str = "PR title") -> providers.PullRequestSummary:
    return providers.PullRequestSummary(
        number, title, "author", "open", f"https://github.com/o/r/pull/{number}", "2026-09-11T10:00:00",
    )


def _comment(body: str) -> providers.ReviewComment:
    return providers.ReviewComment(
        kind=comments.CommentKind.REVIEW, body=body, created_at="2026-09-11T10:00:00", url="https://example/c",
    )


def _conn(tmp_path):
    return store.connect(tmp_path / "usage.db")


class TestIndexSnapshot:
    def test_performs_no_provider_call(self, tmp_path, monkeypatch):
        """index_snapshot reads cache only and never invokes list_pull_requests or list_pr_agent_comments"""
        def fail(*_args, **_kwargs):
            pytest.fail("provider must not be called from index_snapshot")

        monkeypatch.setattr(providers, "list_pull_requests", fail)
        monkeypatch.setattr(providers, "list_pr_agent_comments", fail)
        repos = [Repo(provider="github", slug="o/r")]
        snapshot = index_snapshot(_conn(tmp_path), repos)
        assert snapshot["repos"][0]["state"].status == "loading"


class TestRefreshDeadlines:
    def test_repo_over_deadline_reports_loading_and_returns_quickly(self, tmp_path, monkeypatch):
        """A repository whose fetch exceeds REPO_DEADLINE_SECONDS stays loading and refresh returns before waiting"""
        def slow_pulls(*_args, **_kwargs):
            time.sleep(REPO_DEADLINE_SECONDS + 2)
            return [], False

        monkeypatch.setattr(providers, "list_pull_requests", slow_pulls)
        repos = [Repo(provider="github", slug="o/r")]
        conn = _conn(tmp_path)
        started = time.monotonic()
        refresh(conn, repos)
        elapsed = time.monotonic() - started
        snapshot = index_snapshot(conn, repos)
        assert elapsed < REPO_DEADLINE_SECONDS + 1
        assert snapshot["repos"][0]["state"].status == "loading"


class TestSingleFlight:
    def test_two_concurrent_refreshes_of_one_repo_make_one_provider_call(self, tmp_path, monkeypatch):
        """Two concurrent refresh calls for the same repository coalesce to exactly one list_pull_requests"""
        calls = 0
        gate = threading.Event()
        started = threading.Event()

        def counted_pulls(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            gate.wait(timeout=5)
            return [], False

        monkeypatch.setattr(providers, "list_pull_requests", counted_pulls)
        monkeypatch.setattr(providers, "list_pr_agent_comments", lambda *_a, **_k: ([], False))
        repos = [Repo(provider="github", slug="o/r")]

        def run_refresh() -> None:
            conn = _conn(tmp_path)
            deadline = time.monotonic() + REPO_DEADLINE_SECONDS
            _refresh_one_repo(conn, repos[0], _db_key(conn), deadline)

        t1 = threading.Thread(target=run_refresh)
        t2 = threading.Thread(target=run_refresh)
        t1.start()
        assert started.wait(timeout=5), "first refresh never reached the provider"
        t2.start()
        time.sleep(0.05)
        assert calls == 1, "concurrent refresh must not start a second provider fetch"
        gate.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert calls == 1


class TestPerRepoFailure:
    def test_failed_repository_is_attributed_while_healthy_rows_remain(self, tmp_path, monkeypatch):
        """One repository's ProviderError is failed with its message and the other repository's rows still render"""
        good = Repo(provider="github", slug="good/r")
        bad = Repo(provider="github", slug="bad/r")
        body = _review_body("Leak in auth", "lib/auth.dart", 10, 20)

        def fake_pulls(repo, state="open", limit=50, conn=None):
            if repo.slug == "bad/r":
                raise providers.ProviderError("github returned 500")
            return [_pull(1, "Healthy PR")], False

        def fake_comments(repo, number, conn=None):
            return [_comment(body)], False

        monkeypatch.setattr(providers, "list_pull_requests", fake_pulls)
        monkeypatch.setattr(providers, "list_pr_agent_comments", fake_comments)
        conn = _conn(tmp_path)
        refresh(conn, [good, bad])
        snapshot = index_snapshot(conn, [good, bad])
        by_key = {entry["repo"].key: entry for entry in snapshot["repos"]}
        assert by_key["github:bad/r"]["state"].status == "failed"
        assert "500" in (by_key["github:bad/r"]["state"].message or "")
        assert any(row.finding_title == "Leak in auth" for row in snapshot["rows"])


class TestPerRepoCap:
    def test_only_max_indexed_open_prs_are_fetched(self, tmp_path, monkeypatch):
        """With 30 open PRs only MAX_INDEXED_PRS_PER_REPO list_pull_requests limits are honoured"""
        seen_limits = []

        def fake_pulls(repo, state="open", limit=50, conn=None):
            seen_limits.append(limit)
            return [_pull(number) for number in range(1, limit + 1)], False

        monkeypatch.setattr(providers, "list_pull_requests", fake_pulls)
        monkeypatch.setattr(providers, "list_pr_agent_comments", lambda *_a, **_k: ([], False))
        conn = _conn(tmp_path)
        refresh(conn, [Repo(provider="github", slug="o/r")])
        assert seen_limits == [MAX_INDEXED_PRS_PER_REPO]


class TestFilterRows:
    def _sample_rows(self):
        from pr_dashboard.findings_index import FindingRow

        with_file = FindingRow(
            repo_key="github:o/r", provider="github", slug="o/r", pr_number=1, pr_title="PR",
            finding_title="Race on write", relevant_file="lib/a.dart", line_start=1, line_end=2, command="review",
        )
        without_file = FindingRow(
            repo_key="github:o/r", provider="github", slug="o/r", pr_number=2, pr_title="Other",
            finding_title="Docs gap", relevant_file=None, line_start=None, line_end=None, command="improve",
        )
        other_repo = FindingRow(
            repo_key="github:other/r", provider="github", slug="other/r", pr_number=3, pr_title="Elsewhere",
            finding_title="Other repo issue", relevant_file="x.py", line_start=4, line_end=5, command="review",
        )
        return [with_file, without_file, other_repo]

    def test_repository_filter_narrows_rows(self):
        """The repository filter keeps only rows for that repo key"""
        filtered = filter_rows(self._sample_rows(), repository="github:o/r")
        assert len(filtered) == 2
        assert all(row.repo_key == "github:o/r" for row in filtered)

    def test_command_filter_narrows_rows(self):
        """The command filter keeps only review or improve rows"""
        filtered = filter_rows(self._sample_rows(), command="improve")
        assert len(filtered) == 1
        assert filtered[0].command == "improve"

    def test_has_file_filter_narrows_rows(self):
        """The has_file filter keeps only rows with or without a relevant_file"""
        rows = self._sample_rows()
        assert len(filter_rows(rows, has_file="yes")) == 2
        assert len(filter_rows(rows, has_file="no")) == 1
        assert filter_rows(rows, has_file="no")[0].relevant_file is None

    def test_title_filter_narrows_rows(self):
        """The title filter matches a case-insensitive substring on the finding title"""
        rows = self._sample_rows()
        assert filter_rows(rows, title="race")
        assert filter_rows(rows, title="missing phrase") == []

    def test_unknown_command_filter_returns_empty(self):
        """An unrecognised command filter value returns no rows instead of passing everything through"""
        assert filter_rows(self._sample_rows(), command="destroy") == []

    def test_unknown_has_file_filter_returns_empty(self):
        """An unrecognised has_file filter value returns no rows instead of passing everything through"""
        assert filter_rows(self._sample_rows(), has_file="maybe") == []


class TestRefreshTouchesTheDatabaseFromItsWorker:
    """The gap that let every other test in this file pass against a broken refresh.

    A sqlite3.Connection may only be used in the thread that created it, and
    providers.list_pull_requests takes conn= to reach its TTL cache. Every test that stubs
    the provider at the list_* level never touches the database from the worker thread, so a
    refresh that hands the caller's connection across a thread boundary looks fine. These
    stub at the raw-fetch level instead, leaving the real caching path -- and its database
    access -- in place.
    """

    def test_rows_survive_a_refresh_that_uses_the_cache_layer(self, tmp_path, monkeypatch):
        """A refresh whose provider path reaches the DB still indexes its rows"""
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="o/r")
        body = _review_body("a real finding", "src/app.py", 10, 12)
        monkeypatch.setattr(
            providers, "_fetch_github_pull_requests",
            lambda repo, state, limit: [{
                "number": 1, "title": "PR title", "author": "author", "state": "open",
                "url": "https://github.com/o/r/pull/1", "updated_at": "2026-09-11T10:00:00",
            }],
        )
        monkeypatch.setattr(
            providers, "_fetch_github_issue_comments",
            lambda repo, number: [{
                "body": body, "created_at": "2026-09-11T10:00:00",
                "html_url": "https://example/c",
            }],
        )

        refresh(conn, [repo])

        snapshot = index_snapshot(conn, [repo])
        # Asserting the state alone would pass the broken version: it caches an empty result
        # with no error and reports the repository as fresh.
        assert snapshot["repos"][0]["state"].status == "fresh"
        assert [row.finding_title for row in snapshot["rows"]] == ["a real finding"]

    def test_a_worker_failure_is_reported_not_cached_as_fresh(self, tmp_path, monkeypatch):
        """An unexpected exception during refresh marks the repo failed rather than empty-fresh"""
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="o/r")

        def boom(*_args, **_kwargs):
            raise RuntimeError("something unexpected")

        monkeypatch.setattr(providers, "list_pull_requests", boom)

        refresh(conn, [repo])

        snapshot = index_snapshot(conn, [repo])
        assert snapshot["repos"][0]["state"].status == "failed"
        assert "something unexpected" in (snapshot["repos"][0]["state"].message or "")
