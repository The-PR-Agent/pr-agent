from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.git_providers.github_provider import GithubProvider


def _provider_with_pull_requests(pull_requests):
    provider = GithubProvider.__new__(GithubProvider)
    provider.github_client = MagicMock()
    provider.github_client.get_repo.return_value.get_pulls.return_value = pull_requests
    return provider


def _workflow_run():
    return {
        "repository": {"full_name": "org/repo"},
        "head_repository": {"full_name": "contributor/repo"},
        "head_branch": "feature/fork-review",
        "head_sha": "abc123",
    }


def test_resolve_workflow_run_pull_request_url_matches_fork_and_sha():
    pull_request = SimpleNamespace(
        url="https://api.github.com/repos/org/repo/pulls/42",
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="contributor/repo"),
            sha="abc123",
        ),
    )
    provider = _provider_with_pull_requests([pull_request])

    assert provider.resolve_workflow_run_pull_request_url(_workflow_run()) == pull_request.url
    provider.github_client.get_repo.return_value.get_pulls.assert_called_once_with(
        state="open", head="contributor:feature/fork-review"
    )


def test_resolve_workflow_run_pull_request_url_fails_closed_for_mismatched_sha():
    pull_request = SimpleNamespace(
        url="https://api.github.com/repos/org/repo/pulls/42",
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="contributor/repo"),
            sha="different-sha",
        ),
    )
    provider = _provider_with_pull_requests([pull_request])

    assert provider.resolve_workflow_run_pull_request_url(_workflow_run()) == ""


def test_resolve_workflow_run_pull_request_url_fails_closed_for_ambiguous_matches():
    pull_request = SimpleNamespace(
        url="https://api.github.com/repos/org/repo/pulls/42",
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="contributor/repo"),
            sha="abc123",
        ),
    )
    provider = _provider_with_pull_requests([pull_request, pull_request])

    assert provider.resolve_workflow_run_pull_request_url(_workflow_run()) == ""
