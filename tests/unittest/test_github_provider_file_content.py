from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from pr_agent.git_providers.github_provider import GithubProvider


def _provider_with_result(result=None, error=None):
    provider = GithubProvider.__new__(GithubProvider)
    repo = MagicMock()
    if error is not None:
        repo.get_contents.side_effect = error
    else:
        repo.get_contents.return_value = SimpleNamespace(decoded_content=result)
    provider._get_repo = MagicMock(return_value=repo)
    return provider


def test_get_pr_file_content_keeps_default_leniency():
    provider_error = GithubException(500, {"message": "upstream failure"}, {})
    provider = _provider_with_result(error=provider_error)

    assert provider.get_pr_file_content("CHANGELOG.md", "main") == ""


def test_get_pr_file_content_strict_missing_file_is_empty():
    not_found = GithubException(404, {"message": "Not Found"}, {})
    provider = _provider_with_result(error=not_found)

    assert provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=True) == ""


@pytest.mark.parametrize(
    "error",
    [
        GithubException(500, {"message": "upstream failure"}, {}),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RuntimeError("transport failed"),
    ],
)
def test_get_pr_file_content_strict_reraises_original_error(error):
    provider = _provider_with_result(error=error)

    with pytest.raises(type(error)) as exc_info:
        provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=True)

    assert exc_info.value is error


def test_create_or_update_pr_file_returns_written_commit():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = MagicMock()
    provider.repo_obj.get_contents.return_value.sha = "file-sha"
    provider._get_repo = MagicMock(return_value=provider.repo_obj)
    written_commit = object()
    provider.repo_obj.update_file.return_value = {"content": object(), "commit": written_commit}

    result = provider.create_or_update_pr_file(
        file_path="CHANGELOG.md",
        branch="feature-branch",
        contents="new content",
        message="Update CHANGELOG.md",
    )

    assert result is written_commit
    provider.repo_obj.update_file.assert_called_once_with(
        path="CHANGELOG.md",
        message="Update CHANGELOG.md",
        content="new content",
        sha="file-sha",
        branch="feature-branch",
    )
