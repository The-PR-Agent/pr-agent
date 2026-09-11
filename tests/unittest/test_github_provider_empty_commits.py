from unittest.mock import MagicMock, patch

from github.Commit import Commit

from pr_agent.git_providers.github_provider import GithubProvider


def _commit(sha: str) -> Commit:
    return Commit(
        requester=MagicMock(),
        headers={},
        attributes={
            "sha": sha,
            "url": f"https://api.github.com/repos/owner/repo/commits/{sha}",
            "html_url": f"https://github.com/owner/repo/commit/{sha}",
        },
        completed=True,
    )


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


def test_empty_commit_list_falls_back_to_pull_request_head():
    head_sha = "a" * 40
    fallback_commit = _commit(head_sha)
    pr = MagicMock()
    pr.get_commits.return_value = []
    pr.head.sha = head_sha
    pr.html_url = "https://github.com/owner/repo/pull/1"
    repo = MagicMock()
    repo.get_commit.return_value = fallback_commit

    provider = _provider(pr, repo)

    assert provider.pr_commits == []
    assert provider.last_commit_id is fallback_commit
    assert provider.get_latest_commit_url() == fallback_commit.html_url
    repo.get_commit.assert_called_once_with(head_sha)


def test_non_empty_commit_list_keeps_latest_commit_without_fallback():
    latest_commit = _commit("b" * 40)
    pr = MagicMock()
    pr.get_commits.return_value = [latest_commit]
    pr.html_url = "https://github.com/owner/repo/pull/1"
    repo = MagicMock()

    provider = _provider(pr, repo)

    assert provider.last_commit_id is latest_commit
    repo.get_commit.assert_not_called()
