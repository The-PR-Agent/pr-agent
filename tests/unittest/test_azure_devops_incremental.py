import datetime as _dt
from unittest.mock import MagicMock, patch

from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.git_providers.azuredevops_provider import (
    _AzureCommitAdapter,
    _to_naive_utc,
)
from pr_agent.git_providers.git_provider import IncrementalPR


def _raw_commit(commit_id, comment, author_date, parents=None):
    raw = MagicMock()
    raw.commit_id = commit_id
    raw.comment = comment
    raw.author = MagicMock()
    raw.author.date = author_date
    raw.parents = parents or []
    return raw


def _comment(body, published_date):
    c = MagicMock()
    c.body = body
    c.content = body
    c.published_date = published_date
    c.thread_id = 7
    return c


class TestNaiveUtc:
    def test_strips_tz_from_aware(self):
        aware = _dt.datetime(2024, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)
        naive = _to_naive_utc(aware)
        assert naive.tzinfo is None
        assert naive == _dt.datetime(2024, 1, 1, 12, 0)

    def test_passes_naive_through(self):
        naive = _dt.datetime(2024, 1, 1, 12, 0)
        assert _to_naive_utc(naive) == naive

    def test_none_returns_none(self):
        assert _to_naive_utc(None) is None


class TestAzureCommitAdapter:
    def test_exposes_github_shape(self):
        date = _dt.datetime(2024, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)
        raw = _raw_commit("abc123", "fix bug", date, parents=["p1"])
        adapter = _AzureCommitAdapter(raw)
        assert adapter.sha == "abc123"
        assert adapter.commit_id == "abc123"
        assert adapter.commit.message == "fix bug"
        assert adapter.commit.author.date.tzinfo is None
        assert adapter.parents == ["p1"]

    def test_handles_missing_author(self):
        raw = MagicMock()
        raw.commit_id = "x"
        raw.comment = ""
        raw.author = None
        raw.parents = None
        adapter = _AzureCommitAdapter(raw)
        assert adapter.commit.author.date is None
        assert adapter.parents == []


class TestGetIncrementalCommits:
    def _make_provider(self):
        with patch.object(
            AzureDevopsProvider, "_get_azure_devops_client",
            return_value=(MagicMock(), MagicMock()),
        ):
            provider = AzureDevopsProvider()
        provider.workspace_slug = "ws"
        provider.repo_slug = "repo"
        provider.pr_num = 1
        provider.pr_url = "https://dev.azure.com/o/ws/_git/repo/pullrequest/1"
        return provider

    def test_no_previous_review_disables_incremental(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_pull_request_commits = MagicMock(return_value=[])
        provider.get_issue_comments = MagicMock(return_value=[])

        inc = IncrementalPR(True)
        provider.get_incremental_commits(inc)

        assert provider.incremental.is_incremental is False
        assert provider.previous_review is None

    def test_populates_commits_range_and_files(self):
        provider = self._make_provider()

        review_time = _dt.datetime(2024, 6, 1, 10, 0, tzinfo=_dt.timezone.utc)
        old = _raw_commit(
            "old1", "first", _dt.datetime(2024, 5, 1, tzinfo=_dt.timezone.utc), parents=["p0"],
        )
        new1 = _raw_commit(
            "new1", "second", _dt.datetime(2024, 6, 2, tzinfo=_dt.timezone.utc), parents=["old1"],
        )
        new2 = _raw_commit(
            "new2", "third", _dt.datetime(2024, 6, 3, tzinfo=_dt.timezone.utc), parents=["new1"],
        )
        # Azure returns newest-first.
        provider.azure_devops_client.get_pull_request_commits = MagicMock(
            return_value=[new2, new1, old]
        )

        prev = _comment("## PR Reviewer Guide\nbody", review_time)
        provider.get_issue_comments = MagicMock(return_value=[prev])

        changes_obj = MagicMock()
        changes_obj.changes = [
            {"item": {"path": "/foo.py", "gitObjectType": "blob"}},
            {"item": {"path": "/bar.py", "gitObjectType": "blob"}},
            {"item": {"path": "/somedir", "gitObjectType": "tree"}},
        ]
        provider.azure_devops_client.get_changes = MagicMock(return_value=changes_obj)

        inc = IncrementalPR(True)
        provider.get_incremental_commits(inc)

        assert provider.incremental.is_incremental
        assert len(provider.incremental.commits_range) == 2
        assert provider.incremental.first_new_commit.sha == "new1"
        assert provider.incremental.last_seen_commit.sha == "old1"
        assert provider.incremental.last_seen_commit_sha == "old1"
        assert "/foo.py" in provider.unreviewed_files_map
        assert "/bar.py" in provider.unreviewed_files_map
        assert "/somedir" not in provider.unreviewed_files_map
        assert prev.html_url == provider.get_comment_url(prev)

    def test_skips_merge_commits(self):
        provider = self._make_provider()
        review_time = _dt.datetime(2024, 6, 1, tzinfo=_dt.timezone.utc)
        merge = _raw_commit(
            "m1", "merge", _dt.datetime(2024, 6, 2, tzinfo=_dt.timezone.utc),
            parents=["a", "b"],
        )
        old = _raw_commit("o", "old", _dt.datetime(2024, 5, 1, tzinfo=_dt.timezone.utc))
        provider.azure_devops_client.get_pull_request_commits = MagicMock(
            return_value=[merge, old]
        )
        provider.get_issue_comments = MagicMock(
            return_value=[_comment("## PR Reviewer Guide", review_time)]
        )
        provider.azure_devops_client.get_changes = MagicMock()

        provider.get_incremental_commits(IncrementalPR(True))
        provider.azure_devops_client.get_changes.assert_not_called()

    def test_all_merge_commits_falls_back_to_full(self):
        provider = self._make_provider()
        review_time = _dt.datetime(2024, 6, 1, tzinfo=_dt.timezone.utc)
        merge1 = _raw_commit(
            "m1", "merge1", _dt.datetime(2024, 6, 2, tzinfo=_dt.timezone.utc),
            parents=["a", "b"],
        )
        merge2 = _raw_commit(
            "m2", "merge2", _dt.datetime(2024, 6, 3, tzinfo=_dt.timezone.utc),
            parents=["c", "d"],
        )
        provider.azure_devops_client.get_pull_request_commits = MagicMock(
            return_value=[merge2, merge1]
        )
        provider.get_issue_comments = MagicMock(
            return_value=[_comment("## PR Reviewer Guide", review_time)]
        )
        provider.azure_devops_client.get_changes = MagicMock()

        provider.get_incremental_commits(IncrementalPR(True))

        assert provider.incremental.is_incremental is False
        provider.azure_devops_client.get_changes.assert_not_called()


class TestPrReviewerGuard:
    def test_can_run_returns_false_when_commits_range_none(self):
        from pr_agent.tools.pr_reviewer import PRReviewer
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.is_auto = False
        reviewer.pr_url = "u"
        reviewer.incremental = IncrementalPR(True)
        # incremental.commits_range stays None — provider has the method but didn't populate it.
        reviewer.git_provider = MagicMock(spec=["get_incremental_commits"])
        # Should not raise NoneType len() error.
        assert reviewer._can_run_incremental_review() is False
