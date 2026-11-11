from unittest.mock import MagicMock, patch

import pytest
from github.Commit import Commit

from pr_agent.git_providers.github_provider import GithubProvider


def _commit(sha: str, parents: tuple[str, ...] | None = None) -> Commit:
    attributes = {
        "sha": sha,
        "url": f"https://api.github.com/repos/owner/repo/commits/{sha}",
        "html_url": f"https://github.com/owner/repo/commit/{sha}",
    }
    if parents is not None:
        # The PR commits endpoint lists each commit with its parents.
        attributes["parents"] = [
            {"sha": parent, "url": f"https://api.github.com/repos/owner/repo/commits/{parent}"} for parent in parents
        ]
    return Commit(requester=MagicMock(), headers={}, attributes=attributes, completed=True)


def _provider(pr, repo) -> GithubProvider:
    def set_pr(provider, _):
        provider.repo = "owner/repo"
        provider.pr_num = 1
        provider.pr = pr

    with (
        patch("pr_agent.git_providers.github_provider.get_settings") as get_settings,
        patch.object(GithubProvider, "_get_github_client", return_value=MagicMock()),
        patch.object(GithubProvider, "set_pr", autospec=True, side_effect=set_pr),
        patch.object(GithubProvider, "_get_repo", autospec=True, return_value=repo),
    ):
        get_settings.return_value.get.side_effect = lambda _key, default=None: default
        return GithubProvider("https://github.com/owner/repo/pull/1")


@pytest.mark.parametrize(
    ("listed_count", "head_number"),
    [(0, 1), (1, 1), (2, 3), (250, 250), (250, 251)],
    ids=["empty", "single", "mismatched-tail", "at-api-limit", "truncated-at-api-limit"],
)
def test_commit_selection_and_check_run_use_the_pull_request_head(listed_count, head_number):
    commits = [_commit(f"{number:040x}") for number in range(1, listed_count + 1)]
    head_sha = f"{head_number:040x}"
    head_commit = commits[-1] if listed_count == head_number else _commit(head_sha)
    pr = MagicMock()
    pr.get_commits.return_value = commits
    pr.head.sha = head_sha
    pr.html_url = "https://github.com/owner/repo/pull/1"
    pr._requester.requestJsonAndCheck.side_effect = lambda method, url, **kwargs: (
        ({}, {"check_runs": []}) if method == "GET" else ({}, {"id": 222})
    )
    repo = MagicMock()
    repo.get_commit.return_value = head_commit

    provider = _provider(pr, repo)

    assert provider.pr_commits == commits
    assert provider.last_commit_id is head_commit
    assert provider.get_latest_commit_url() == head_commit.html_url
    if listed_count == head_number:
        repo.get_commit.assert_not_called()
    else:
        repo.get_commit.assert_called_once_with(head_sha)

    assert provider._publish_check_run("review output", "review") is True
    lookup, create = pr._requester.requestJsonAndCheck.call_args_list
    assert lookup.args == ("GET", f"https://api.github.com/repos/owner/repo/commits/{head_sha}/check-runs")
    assert create.args == ("POST", "https://api.github.com/repos/owner/repo/check-runs")
    assert create.kwargs["input"]["head_sha"] == head_sha


OLD_BASE, BASE, FEATURE, FIX, OTHER, MERGE = (letter * 40 for letter in "dbfeca")


def _provider_with_commits(commits, head_commit, repo=None):
    pr = MagicMock()
    pr.get_commits.return_value = commits
    pr.head.sha = head_commit.sha
    pr.base.sha, pr.base.ref = BASE, "main"
    repo = repo or MagicMock()
    repo.get_commit.return_value = head_commit
    provider = _provider(pr, repo)
    provider._get_repo = lambda: repo
    repo.get_commit.reset_mock()
    return provider, repo


@pytest.mark.parametrize(
    ("parents", "compare_status", "expected"),
    [
        ((FEATURE, BASE), None, True),
        ((FEATURE, OLD_BASE), "ahead", True),
        ((FIX, BASE), None, False),
        ((FEATURE, OTHER), "diverged", False),
        ((FEATURE, OTHER), "behind", False),
        ((FEATURE,), None, False),
        ((FEATURE, BASE, OTHER), None, False),
    ],
    ids=[
        "update-branch",
        "merge-of-older-base-commit",
        "merge-after-new-commit",
        "merge-of-other-branch",
        "merge-of-branch-ahead-of-base",
        "ordinary-commit",
        "octopus-merge",
    ],
)
def test_is_base_branch_merge(parents, compare_status, expected):
    # The push moved the PR from FEATURE to MERGE. "Update branch" merges the base branch's head (the PR's
    # base commit, BASE) into FEATURE; a `git pull` after committing FIX on FEATURE has FIX as first parent.
    commits = [_commit(FEATURE, (OLD_BASE,))]
    if FIX in parents:
        commits.append(_commit(FIX, (FEATURE,)))
    commits.append(_commit(MERGE, parents))
    provider, repo = _provider_with_commits(commits, commits[-1])
    repo.compare.return_value.status = compare_status

    assert provider.is_base_branch_merge(MERGE, FEATURE) is expected
    repo.get_commit.assert_not_called()
    if compare_status is None:
        repo.compare.assert_not_called()
    else:
        repo.compare.assert_called_once_with(parents[1], "main")


def test_is_base_branch_merge_reads_head_beyond_commit_list():
    commits = [_commit(f"{number:040x}", (f"{number - 1:040x}",)) for number in range(1, 251)]
    head = _commit(MERGE, (commits[-1].sha, BASE))
    provider, repo = _provider_with_commits(commits, head)

    assert provider.last_commit_id is head
    assert provider.is_base_branch_merge(MERGE, commits[-1].sha) is True
    repo.get_commit.assert_not_called()
    repo.compare.assert_not_called()


def test_is_base_branch_merge_reads_unlisted_commit_from_repository():
    commits = [_commit(FEATURE, (OLD_BASE,))]
    provider, repo = _provider_with_commits(commits, commits[-1])
    repo.get_commit.return_value = _commit(MERGE, (FEATURE, BASE))

    assert provider.is_base_branch_merge(MERGE, FEATURE) is True
    repo.get_commit.assert_called_once_with(MERGE)
