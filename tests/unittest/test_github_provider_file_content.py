from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from pr_agent.git_providers.git_provider import ConcurrentFileUpdateError, FileContentSnapshot
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider_with_result(result=None, error=None):
    provider = GithubProvider.__new__(GithubProvider)
    repo = MagicMock()
    if error is not None:
        repo.get_contents.side_effect = error
    else:
        repo.get_contents.return_value = SimpleNamespace(decoded_content=result)
    provider._get_repo = MagicMock(return_value=repo)
    provider._pr_head_in_base_repo = MagicMock(return_value=True)
    return provider


def test_revision_content_is_reused_across_command_scope_resets():
    provider = _provider_with_result(result=b"file content")
    provider.repo = "owner/repo"
    file = SimpleNamespace(filename="src/file.py")

    for _ in range(3):
        assert provider._get_pr_file_content(file, "a" * 40) == "file content"
        provider.reset_diff_cache_for_command()

    provider._get_repo().get_contents.assert_called_once_with("src/file.py", ref="a" * 40)


def test_revision_content_cache_keys_include_repository_sha_and_old_path():
    provider = _provider_with_result(result=b"file content")
    provider.repo = "owner/repo"
    file = SimpleNamespace(filename="src/new.py")

    assert provider._get_pr_file_content(file, "a" * 40) == "file content"
    assert provider._get_pr_file_content(file, "b" * 40) == "file content"
    assert provider._get_pr_file_content(file, "a" * 40, path="src/old.py") == "file content"
    provider.repo = "other/repo"
    assert provider._get_pr_file_content(file, "a" * 40) == "file content"

    assert provider._get_repo().get_contents.call_count == 4


@pytest.mark.parametrize("required", [False, True])
def test_revision_content_does_not_cache_failed_reads(required):
    provider = _provider_with_result()
    provider.repo = "owner/repo"
    file = SimpleNamespace(filename="src/file.py")
    provider._get_repo().get_contents.side_effect = [
        GithubException(500, {"message": "temporary failure"}, {}),
        SimpleNamespace(decoded_content=b"recovered"),
    ]

    assert provider._get_pr_file_content(file, "a" * 40, required=required) == (None if required else "")
    assert provider._get_pr_file_content(file, "a" * 40, required=required) == "recovered"
    assert provider._get_pr_file_content(file, "a" * 40, required=required) == "recovered"
    assert provider._get_repo().get_contents.call_count == 2


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
    provider._pr_head_in_base_repo = MagicMock(return_value=True)
    written_commit = object()
    provider.repo_obj.update_file.return_value = {"content": object(), "commit": written_commit}

    result = provider.create_or_update_pr_file(
        file_path="CHANGELOG.md",
        branch="feature-branch",
        contents="new content",
        message="Update CHANGELOG.md",
        expected_snapshot=FileContentSnapshot("old", True, "file-sha"),
    )

    assert result is written_commit
    provider.repo_obj.get_contents.assert_not_called()
    provider.repo_obj.update_file.assert_called_once_with(
        path="CHANGELOG.md",
        message="Update CHANGELOG.md",
        content="new content",
        sha="file-sha",
        branch="feature-branch",
    )


def test_create_or_update_pr_file_creates_missing_file():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = MagicMock()
    provider.repo_obj.get_contents.side_effect = GithubException(404, {"message": "Not Found"}, {})
    provider._get_repo = MagicMock(return_value=provider.repo_obj)
    provider.pr = SimpleNamespace(
        head=SimpleNamespace(repo=SimpleNamespace(full_name="owner/repo")),
        base=SimpleNamespace(repo=SimpleNamespace(full_name="owner/repo")),
    )
    written_commit = object()
    provider.repo_obj.create_file.return_value = {"content": object(), "commit": written_commit}

    result = provider.create_or_update_pr_file(
        file_path="CHANGELOG.md",
        branch="feature-branch",
        contents="new content",
        message="Add CHANGELOG.md",
        expected_snapshot=FileContentSnapshot("", False, None),
    )

    assert result is written_commit
    provider.repo_obj.create_file.assert_called_once_with(
        path="CHANGELOG.md",
        message="Add CHANGELOG.md",
        content="new content",
        branch="feature-branch",
    )
    provider.repo_obj.update_file.assert_not_called()


@pytest.mark.parametrize("snapshot", [
    FileContentSnapshot("", False, None),
    FileContentSnapshot("old", True, "file-sha"),
])
def test_create_or_update_pr_file_does_not_write_for_fork_pr(snapshot):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = MagicMock()
    provider._get_repo = MagicMock(return_value=provider.repo_obj)
    # Fork pull request: the head branch lives in the fork, so the bare branch name
    # resolves against the base repository; the file must not be created there.
    provider.pr = SimpleNamespace(
        head=SimpleNamespace(repo=SimpleNamespace(full_name="fork-owner/repo")),
        base=SimpleNamespace(repo=SimpleNamespace(full_name="owner/repo")),
    )

    with pytest.raises(ValueError, match="fork pull request"):
        provider.create_or_update_pr_file(
            file_path="CHANGELOG.md",
            branch="main",
            contents="new content",
            message="Add CHANGELOG.md",
            expected_snapshot=snapshot,
        )

    provider.repo_obj.get_contents.assert_not_called()
    provider.repo_obj.create_file.assert_not_called()
    provider.repo_obj.update_file.assert_not_called()


def test_create_or_update_pr_file_does_not_create_for_deleted_fork():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = MagicMock()
    provider._get_repo = MagicMock(return_value=provider.repo_obj)
    # GitHub reports head.repo as null when the fork was deleted.
    provider.pr = SimpleNamespace(
        head=SimpleNamespace(repo=None),
        base=SimpleNamespace(repo=SimpleNamespace(full_name="owner/repo")),
    )

    with pytest.raises(ValueError, match="fork pull request"):
        provider.create_or_update_pr_file(
            file_path="CHANGELOG.md",
            branch="main",
            contents="new content",
            message="Add CHANGELOG.md",
            expected_snapshot=FileContentSnapshot("", False, None),
        )

    provider.repo_obj.get_contents.assert_not_called()
    provider.repo_obj.create_file.assert_not_called()
    provider.repo_obj.update_file.assert_not_called()


def test_create_or_update_pr_file_does_not_write_after_read_failure():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = MagicMock()
    read_error = GithubException(500, {"message": "upstream failure"}, {})
    provider.repo_obj.get_contents.side_effect = read_error
    provider._get_repo = MagicMock(return_value=provider.repo_obj)
    provider._pr_head_in_base_repo = MagicMock(return_value=True)

    with pytest.raises(GithubException) as exc_info:
        provider.create_or_update_pr_file(
            file_path="CHANGELOG.md",
            branch="feature-branch",
            contents="new content",
            message="Update CHANGELOG.md",
            expected_snapshot=FileContentSnapshot("", False, None),
        )

    assert exc_info.value is read_error
    provider.repo_obj.create_file.assert_not_called()
    provider.repo_obj.update_file.assert_not_called()


def test_gitlab_create_or_update_pr_file_does_not_write_for_fork_mr():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.mr = SimpleNamespace(source_project_id=2, target_project_id=1)
    provider.gl = MagicMock()
    provider.id_project = 1

    with pytest.raises(ValueError, match="fork merge request"):
        provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature", "new content", expected_snapshot=FileContentSnapshot("old", True, "sha")
        )

    provider.gl.projects.get.assert_not_called()


@pytest.mark.parametrize("content", [b"", b"existing content"])
def test_file_snapshot_keeps_content_and_sha_from_one_response(content):
    provider = _provider_with_result(result=content)
    provider._get_repo().get_contents.return_value.sha = "captured-blob"

    snapshot = provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature")

    assert snapshot == FileContentSnapshot(content.decode(), True, "captured-blob")
    provider._get_repo().get_contents.assert_called_once_with("CHANGELOG.md", ref="feature")


def test_file_snapshot_distinguishes_missing_from_empty():
    provider = _provider_with_result(error=GithubException(404, {}, {}))
    assert provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature") == FileContentSnapshot("", False, None)


@pytest.mark.parametrize("error", [GithubException(500, {}, {}), RuntimeError("transport failed")])
def test_file_snapshot_propagates_read_errors(error):
    provider = _provider_with_result(error=error)
    with pytest.raises(type(error)) as raised:
        provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature")
    assert raised.value is error


def test_file_snapshot_propagates_invalid_content():
    provider = _provider_with_result(result=b"\xff")
    with pytest.raises(UnicodeDecodeError):
        provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature")


def test_guarded_create_does_not_overwrite_a_file_that_appeared():
    provider = _provider_with_result(result=b"concurrent contents")
    with pytest.raises(ConcurrentFileUpdateError):
        provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature", "stale contents", expected_snapshot=FileContentSnapshot("", False, None)
        )
    provider._get_repo().create_file.assert_not_called()
    provider._get_repo().update_file.assert_not_called()


@pytest.mark.parametrize("status", [409, 422])
def test_guarded_create_propagates_final_race_without_retry(status):
    provider = _provider_with_result(error=GithubException(404, {}, {}))
    provider._pr_head_in_base_repo = MagicMock(return_value=True)
    failure = GithubException(status, {"message": "file created concurrently"}, {})
    provider._get_repo().create_file.side_effect = failure
    with pytest.raises(GithubException) as raised:
        provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature", "stale contents", expected_snapshot=FileContentSnapshot("", False, None)
        )
    assert raised.value is failure
    provider._get_repo().get_contents.assert_called_once_with("CHANGELOG.md", ref="feature")
    provider._get_repo().create_file.assert_called_once()
    provider._get_repo().update_file.assert_not_called()


@pytest.mark.parametrize("status", [404, 409, 422])
def test_guarded_update_keeps_captured_sha_and_never_switches_to_create(status):
    provider = _provider_with_result(result=b"newer contents")
    provider._get_repo().get_contents.return_value.sha = "newer-blob"
    failure = GithubException(status, {}, {})
    provider._get_repo().update_file.side_effect = failure
    with pytest.raises(GithubException) as raised:
        provider.create_or_update_pr_file(
            "CHANGELOG.md", "feature", "stale contents", expected_snapshot=FileContentSnapshot("old", True, "old-blob")
        )
    assert raised.value is failure
    provider._get_repo().get_contents.assert_not_called()
    provider._get_repo().update_file.assert_called_once_with(
        path="CHANGELOG.md", branch="feature", content="stale contents", message="", sha="old-blob"
    )
    provider._get_repo().create_file.assert_not_called()


def test_repo_lookup_reuses_case_insensitive_name_across_file_reads():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.github_client = MagicMock()
    repo = MagicMock()
    repo.full_name = "Owner/Repo"
    repo.get_contents.return_value.decoded_content = b"content"
    provider.github_client.get_repo.return_value = repo

    assert provider.get_pr_file_content("first.py", "main") == "content"
    assert provider.get_pr_file_content("second.py", "main") == "content"
    provider.github_client.get_repo.assert_called_once_with("owner/repo")
    assert repo.get_contents.call_count == 2


def test_repo_lookup_fetches_again_for_a_different_repository():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.github_client = MagicMock()
    first_repo = MagicMock()
    first_repo.full_name = "Owner/Repo"
    second_repo = MagicMock()
    second_repo.full_name = "Owner/Other"
    provider.github_client.get_repo.side_effect = [first_repo, second_repo]

    assert provider._get_repo() is first_repo
    provider.repo = "owner/other"
    assert provider._get_repo() is second_repo
    assert provider._get_repo() is second_repo
    assert provider.github_client.get_repo.call_count == 2


@pytest.mark.parametrize(
    ("cached_name", "requested_name"),
    [(None, "owner/repo"), ("Owner/Repo", None)],
)
def test_repo_lookup_refetches_when_cached_name_cannot_be_compared(cached_name, requested_name):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = requested_name
    provider.repo_obj = SimpleNamespace(full_name=cached_name)
    provider.github_client = MagicMock()
    replacement = MagicMock()
    provider.github_client.get_repo.return_value = replacement

    assert provider._get_repo() is replacement
    provider.github_client.get_repo.assert_called_once_with(requested_name)
