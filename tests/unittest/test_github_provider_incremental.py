from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, call
from urllib.parse import parse_qsl, urlsplit

from github.Commit import Commit

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


def test_incremental_commit_files_keep_300_item_pages():
    requester = Mock(per_page=100, is_not_lazy=False)
    commit_url = "https://api.github.com/repos/example/project/commits/pushed"
    all_files = [{"filename": f"file-{index}.py", "status": "modified"} for index in range(301)]

    def fetch_page(method, url, parameters=None, headers=None):
        assert method == "GET"
        query = dict(parse_qsl(urlsplit(url).query))
        query.update(parameters or {})
        assert int(query["per_page"]) == 300
        page = int(query.get("page", 1))
        offset = (page - 1) * 300
        links = {"link": f'<{commit_url}?per_page=300&page=2>; rel="next"'} if page == 1 else {}
        return links, {"files": all_files[offset:offset + 300]}

    requester.requestJsonAndCheck.side_effect = fetch_page
    commit = Commit(requester, {}, {
        "url": commit_url, "sha": "pushed", "commit": {"message": "Change files"},
    }, completed=False)
    provider = _make_provider([commit])
    provider.incremental.last_seen_commit = SimpleNamespace(sha="reviewed")
    provider.get_previous_review = lambda **kwargs: provider.previous_review
    provider.get_commit_range = lambda: [commit]
    provider._get_repo = lambda: SimpleNamespace(default_branch="main")
    provider.unreviewed_files_map = {}

    provider._get_incremental_commits()

    assert list(provider.unreviewed_files_map) == [file["filename"] for file in all_files]
    assert requester.requestJsonAndCheck.call_count == 2


def test_incremental_review_skips_merge_commits_regardless_of_message():
    merge_commit = _commit("merge", author_date=datetime(2026, 1, 4))
    merge_commit.parents = [SimpleNamespace(), SimpleNamespace()]
    merge_commit.commit.message = "Sync target branch"
    merge_commit.get_files = Mock(side_effect=AssertionError("merge files must not be reviewed"))
    provider = _make_provider([merge_commit])
    provider.incremental.last_seen_commit = SimpleNamespace(sha="reviewed")
    provider.get_previous_review = lambda **kwargs: provider.previous_review
    provider.get_commit_range = lambda: [merge_commit]
    provider.unreviewed_files_map = {}

    provider._get_incremental_commits()

    assert provider.unreviewed_files_map == {}


def test_previous_review_ignores_matching_comment_from_another_user():
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = Mock()
    foreign = SimpleNamespace(body="## PR Reviewer Guide 🔍\n\nforeign")
    provider.pr.get_issue_comments.return_value = [foreign]
    provider.is_comment_authored_by_pr_agent = Mock(return_value=False)

    assert provider.get_previous_review(full=True, incremental=False) is None


def test_previous_review_uses_latest_matching_comment_authored_by_pr_agent():
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = Mock()
    foreign = SimpleNamespace(body="## PR Reviewer Guide 🔍\n\nforeign")
    agent = SimpleNamespace(body="## PR Reviewer Guide 🔍\n\nagent")
    provider.pr.get_issue_comments.return_value = [agent, foreign]
    provider.is_comment_authored_by_pr_agent = Mock(side_effect=[False, True])

    assert provider.get_previous_review(full=True, incremental=False) is agent
    assert provider.is_comment_authored_by_pr_agent.call_args_list == [call(foreign), call(agent)]
