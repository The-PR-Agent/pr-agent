from datetime import datetime
from types import SimpleNamespace

from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.github_provider import GithubProvider

REVIEW_TIME = datetime(2026, 1, 3, 10, 0, 0)


def _commit(sha, author_date, committer_date=None):
    committer = SimpleNamespace(date=committer_date) if committer_date else None
    return SimpleNamespace(
        sha=sha,
        commit=SimpleNamespace(author=SimpleNamespace(date=author_date), committer=committer),
    )


def _make_provider(pr_commits):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr_commits = pr_commits
    provider.previous_review = SimpleNamespace(created_at=REVIEW_TIME)
    provider.incremental = IncrementalPR(True)
    return provider


def test_rebased_commit_is_detected_as_new():
    # A force-push rewrites the commit after the review, but git preserves the author date.
    commit = _commit("rebased", author_date=datetime(2026, 1, 2), committer_date=datetime(2026, 1, 5))
    provider = _make_provider([commit])

    assert provider.get_commit_range() == [commit]
    assert provider.incremental.first_new_commit is commit


def test_commit_older_than_review_is_not_new():
    commit = _commit("old", author_date=datetime(2026, 1, 1), committer_date=datetime(2026, 1, 1))
    provider = _make_provider([commit])

    assert provider.get_commit_range() == []
    assert provider.incremental.last_seen_commit is commit


def test_falls_back_to_author_date_when_committer_is_missing():
    commit = _commit("no-committer", author_date=datetime(2026, 1, 5))
    provider = _make_provider([commit])

    assert provider.get_commit_range() == [commit]


def test_fully_rebased_branch_falls_back_to_full_review():
    # Every commit post-dates the review, so there is no baseline commit to diff against.
    commits = [
        _commit("a", author_date=datetime(2026, 1, 1), committer_date=datetime(2026, 1, 5)),
        _commit("b", author_date=datetime(2026, 1, 2), committer_date=datetime(2026, 1, 5)),
    ]
    provider = _make_provider(commits)
    provider.pr = SimpleNamespace(get_commits=lambda: commits)
    provider.unreviewed_files_map = {}
    provider.get_previous_review = lambda **kwargs: provider.previous_review

    provider._get_incremental_commits()

    assert provider.incremental.is_incremental is False
    assert provider.incremental.last_seen_commit_sha is None


def test_only_commits_after_the_review_are_returned():
    reviewed = _commit("reviewed", author_date=datetime(2026, 1, 1), committer_date=datetime(2026, 1, 1))
    pushed = _commit("pushed", author_date=datetime(2026, 1, 4), committer_date=datetime(2026, 1, 4))
    provider = _make_provider([reviewed, pushed])

    assert provider.get_commit_range() == [pushed]
    assert provider.incremental.last_seen_commit is reviewed
