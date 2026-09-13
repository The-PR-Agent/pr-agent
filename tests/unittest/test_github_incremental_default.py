"""The default `IncrementalPR` is per call, not one object shared since import."""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.github_provider import GithubProvider


def _provider() -> GithubProvider:
    with (
        patch("pr_agent.git_providers.github_provider.get_settings") as get_settings,
        patch.object(GithubProvider, "_get_github_client", return_value=MagicMock()),
    ):
        get_settings.return_value.get.side_effect = lambda _key, default=None: default
        return GithubProvider(pr_url=None)


def test_two_providers_do_not_share_one_default_incremental():
    """A long-lived server handles many pull requests through one process."""
    first, second = _provider(), _provider()

    first.get_incremental_commits()
    second.get_incremental_commits()

    assert first.incremental is not second.incremental

    # The writes the incremental path makes, on the object the default supplied.
    first.incremental.is_incremental = True
    first.incremental.commits_range = ["c1"]
    first.incremental.last_seen_commit = "sha"

    assert second.incremental.is_incremental is False
    assert second.incremental.commits_range is None
    assert second.incremental.last_seen_commit is None


def test_two_calls_on_one_provider_do_not_share_it_either():
    provider = _provider()

    provider.get_incremental_commits()
    first = provider.incremental
    first.commits_range = ["c1"]

    provider.get_incremental_commits()

    assert provider.incremental is not first
    assert provider.incremental.commits_range is None


def test_an_incremental_passed_in_is_the_one_used():
    """The fix must not replace what a caller handed over: `/review -i` reads
    `commits_range` back off the object it passed."""
    provider = _provider()
    given = IncrementalPR(False)

    provider.get_incremental_commits(given)

    assert provider.incremental is given


def test_can_run_incremental_review_handles_aware_commit_dates():
    """Regression: PyGithub 2.x returns timezone-aware commit timestamps,
    but _can_run_incremental_review compared them against a naive
    datetime.now(), raising TypeError on every second run."""
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.is_auto = False
    reviewer.git_provider = MagicMock(spec=["get_incremental_commits"])
    reviewer.pr_url = "https://github.com/test/repo/pull/1"

    aware_date = datetime.datetime(2026, 9, 10, 12, 0, tzinfo=datetime.timezone.utc)
    commit = SimpleNamespace(commit=SimpleNamespace(author=SimpleNamespace(date=aware_date)))

    reviewer.incremental = IncrementalPR(True)
    reviewer.incremental.commits_range = [commit]
    reviewer.incremental.last_seen_commit = commit

    with patch("pr_agent.tools.pr_reviewer.get_settings") as mock_settings:
        mock_settings.return_value.git_provider = "github"
        mock_settings.return_value.pr_reviewer.minimal_commits_for_incremental_review = 0
        mock_settings.return_value.pr_reviewer.minimal_minutes_for_incremental_review = 0
        mock_settings.return_value.pr_reviewer.require_all_thresholds_for_incremental_review = False
        # Must not raise TypeError
        result = reviewer._can_run_incremental_review()

    assert result in (True, False)  # either outcome is valid; crash is the bug


def _frozen_now(fixed_aware_utc):
    class _FrozenDatetime:
        timezone = datetime.timezone
        timedelta = datetime.timedelta

        class datetime:
            @staticmethod
            def now(*args, **kwargs):
                return fixed_aware_utc.astimezone(datetime.timezone.utc)

    return patch("pr_agent.tools.pr_reviewer.datetime", new=_FrozenDatetime)


def test_incremental_review_runs_when_commit_is_older_than_utc_threshold():
    """The threshold is naive UTC, so a host in a non-UTC zone must not shift
    the minimum-age decision."""
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.is_auto = False
    reviewer.git_provider = MagicMock(spec=["get_incremental_commits"])
    reviewer.pr_url = "https://github.com/test/repo/pull/1"

    commit = SimpleNamespace(
        commit=SimpleNamespace(author=SimpleNamespace(date=datetime.datetime(2026, 9, 10, 12, 0, tzinfo=datetime.timezone.utc)))
    )
    reviewer.incremental = IncrementalPR(True)
    reviewer.incremental.commits_range = [commit]
    reviewer.incremental.last_seen_commit = commit

    with (
        _frozen_now(datetime.datetime(2026, 9, 10, 13, 0, tzinfo=datetime.timezone.utc)),
        patch("pr_agent.tools.pr_reviewer.get_settings") as mock_settings,
    ):
        mock_settings.return_value.git_provider = "github"
        mock_settings.return_value.pr_reviewer.minimal_commits_for_incremental_review = 0
        mock_settings.return_value.pr_reviewer.minimal_minutes_for_incremental_review = 30
        mock_settings.return_value.pr_reviewer.require_all_thresholds_for_incremental_review = False

        assert reviewer._can_run_incremental_review() is True


def test_incremental_review_skipped_when_commit_is_newer_than_utc_threshold():
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.is_auto = False
    reviewer.git_provider = MagicMock(spec=["get_incremental_commits"])
    reviewer.pr_url = "https://github.com/test/repo/pull/1"

    commit = SimpleNamespace(
        commit=SimpleNamespace(author=SimpleNamespace(date=datetime.datetime(2026, 9, 10, 13, 15, tzinfo=datetime.timezone.utc)))
    )
    reviewer.incremental = IncrementalPR(True)
    reviewer.incremental.commits_range = [commit]
    reviewer.incremental.last_seen_commit = commit

    with (
        _frozen_now(datetime.datetime(2026, 9, 10, 13, 0, tzinfo=datetime.timezone.utc)),
        patch("pr_agent.tools.pr_reviewer.get_settings") as mock_settings,
    ):
        mock_settings.return_value.git_provider = "github"
        mock_settings.return_value.pr_reviewer.minimal_commits_for_incremental_review = 0
        mock_settings.return_value.pr_reviewer.minimal_minutes_for_incremental_review = 30
        mock_settings.return_value.pr_reviewer.require_all_thresholds_for_incremental_review = True

        assert reviewer._can_run_incremental_review() is False
