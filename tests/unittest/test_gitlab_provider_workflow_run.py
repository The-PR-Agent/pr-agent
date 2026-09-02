from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider_with_merge_requests(merge_requests):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = MagicMock()
    base_project = SimpleNamespace(
        id=101,
        path_with_namespace="org/repo",
        mergerequests=MagicMock(),
    )
    source_project = SimpleNamespace(id=202, path_with_namespace="contributor/repo")
    base_project.mergerequests.list.return_value = merge_requests
    provider.gl.projects.get.side_effect = lambda project: {
        "org/repo": base_project,
        "contributor/repo": source_project,
    }[project]
    return provider, base_project


def _merge_request(source_project_id=202, sha="abc123"):
    return SimpleNamespace(
        source_project_id=source_project_id,
        source_branch="feature/fork-review",
        sha=sha,
        web_url="https://gitlab.com/org/repo/-/merge_requests/42",
    )


def test_find_open_pr_url_matches_source_project_branch_and_sha():
    provider, base_project = _provider_with_merge_requests([_merge_request()])

    assert provider.find_open_pr_url(
        "org/repo", "contributor/repo", "feature/fork-review", "abc123"
    ) == "https://gitlab.com/org/repo/-/merge_requests/42"
    base_project.mergerequests.list.assert_called_once_with(
        get_all=True, state="opened", source_branch="feature/fork-review"
    )


def test_find_open_pr_url_fails_closed_for_mismatched_source_metadata():
    provider, _ = _provider_with_merge_requests([
        _merge_request(source_project_id=999),
        _merge_request(sha="different-sha"),
    ])

    assert provider.find_open_pr_url(
        "org/repo", "contributor/repo", "feature/fork-review", "abc123"
    ) == ""


def test_find_open_pr_url_fails_closed_for_ambiguous_matches():
    provider, _ = _provider_with_merge_requests([_merge_request(), _merge_request()])

    assert provider.find_open_pr_url(
        "org/repo", "contributor/repo", "feature/fork-review", "abc123"
    ) == ""


def test_find_open_pr_url_fails_closed_when_gitlab_lookup_fails():
    provider, base_project = _provider_with_merge_requests([])
    base_project.mergerequests.list.side_effect = RuntimeError("GitLab unavailable")

    assert provider.find_open_pr_url(
        "org/repo", "contributor/repo", "feature/fork-review", "abc123"
    ) == ""
