import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from azure.devops.exceptions import AzureDevOpsServiceError

from pr_agent.algo.inline_comment_dedup import code_fingerprint
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import PRCodeSuggestionsIdentity
from pr_agent.git_providers.azuredevops_provider import (
    AzureDevopsProvider,
    Comment,
    CommentThread,
)
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.log import get_logger


def test_get_pr_branch_preserves_slashes_in_source_branch():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "org"
    provider.pr_num = 1
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.get_pull_request_by_id.return_value = SimpleNamespace(
        source_ref_name="refs/heads/feature/release/v2"
    )

    assert provider.get_pr_branch() == "feature/release/v2"


def test_publish_description_propagates_update_failure():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "my-project"
    provider.repo_slug = "my-repo"
    provider.pr_num = 1
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.update_pull_request.side_effect = RuntimeError("permission denied")

    with pytest.raises(RuntimeError, match="permission denied"):
        provider.publish_description("AI title", "Updated description")


def test_azure_supports_thread_resolution():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)

    assert provider.supports_thread_resolution() is True


def test_azure_resolve_comment_thread_closes_thread():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.set_thread_status = MagicMock(return_value=True)

    assert provider.resolve_comment_thread(42) is True
    provider.set_thread_status.assert_called_once_with(42, "closed")


def _language_provider(items):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "proj"
    provider.repo_slug = "repo"
    provider.pr = SimpleNamespace(
        last_merge_target_commit=SimpleNamespace(commit_id="base-sha"),
        last_merge_commit=SimpleNamespace(commit_id="head-sha"),
    )
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.get_items.return_value = items
    return provider


def test_get_languages_returns_language_names():
    # get_languages() must key on language NAMES (e.g. "Python"), not raw
    # extensions ("py"): sort_files_by_main_languages() maps names back to
    # extensions, so extension keys would drop every file into "Other".
    provider = _language_provider([
        SimpleNamespace(git_object_type="blob", path="a.py"),
        SimpleNamespace(git_object_type="blob", path="b.py"),
        SimpleNamespace(git_object_type="blob", path="c.py"),
        SimpleNamespace(git_object_type="blob", path="d.js"),
        SimpleNamespace(git_object_type="blob", path="weird.zzz"),
    ])

    languages = provider.get_languages()

    # 3 Python + 1 JavaScript known; .zzz is unknown and excluded from the total.
    assert languages == {"Python": 75.0, "JavaScript": 25.0}


def test_get_languages_queries_target_commit_inventory():
    provider = _language_provider(
        [SimpleNamespace(git_object_type="blob", path="a.py")]
    )

    provider.get_languages()

    call_kwargs = provider.azure_devops_client.get_items.call_args.kwargs
    assert call_kwargs["version_descriptor"].version == "base-sha"
    assert call_kwargs["version_descriptor"].version_type == "commit"


def test_get_languages_returns_empty_map_when_target_commit_missing():
    provider = _language_provider(
        [SimpleNamespace(git_object_type="blob", path="a.py")]
    )
    provider.pr = SimpleNamespace(last_merge_target_commit=None)

    assert provider.get_languages() == {}
    provider.azure_devops_client.get_items.assert_not_called()


def test_get_languages_returns_empty_map_when_target_commit_id_missing():
    provider = _language_provider(
        [SimpleNamespace(git_object_type="blob", path="a.py")]
    )
    provider.pr = SimpleNamespace(last_merge_target_commit=SimpleNamespace(commit_id=None))

    assert provider.get_languages() == {}
    provider.azure_devops_client.get_items.assert_not_called()


def test_get_languages_returns_empty_map_when_nothing_matches():
    provider = _language_provider([
        SimpleNamespace(git_object_type="blob", path="weird.zzz"),
        SimpleNamespace(git_object_type="blob", path=""),
    ])

    assert provider.get_languages() == {}


def test_get_languages_maps_full_paths_and_multipart_extensions():
    # The matcher must classify by the basename and honor multipart extensions.
    provider = _language_provider([
        SimpleNamespace(git_object_type="blob", path="src/foo.py"),
        SimpleNamespace(git_object_type="blob", path="lib/bar.py"),
        SimpleNamespace(git_object_type="blob", path="doc/README.md"),
        SimpleNamespace(git_object_type="blob", path="notes.txt"),
        SimpleNamespace(git_object_type="blob", path="tpl/file.test.ts"),
    ])

    languages = provider.get_languages()

    assert languages == {"Python": 40.0, "Markdown": 20.0, "Text": 20.0, "TypeScript": 20.0}


def test_get_languages_ignores_non_blob_items_and_other_languages():
    # Percentages come from the repository blob inventory, not the PR change set:
    # non-blob entries (e.g. folders) must be skipped, and off-language files
    # must not skew the ranking.
    provider = _language_provider([
        SimpleNamespace(git_object_type="Folder", path="src"),
        SimpleNamespace(git_object_type="blob", path="src/app.py"),
        SimpleNamespace(git_object_type="blob", path="src/main.py"),
        SimpleNamespace(git_object_type="blob", path="render.bin"),
    ])

    languages = provider.get_languages()

    assert languages == {"Python": 100.0}


class TestAzureDevopsProviderRepoContext:
    def test_get_repo_file_content_reads_from_target_commit(self):
        # Repo-context files must be read from the PR target (base) commit, matching
        # the other providers.
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr = MagicMock()
        provider.pr.last_merge_target_commit.commit_id = "base-sha"
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_item.return_value = MagicMock(content="repo context")

        content = provider.get_repo_file_content("AGENTS.md")

        assert content == "repo context"
        _, kwargs = provider.azure_devops_client.get_item.call_args
        assert kwargs["path"] == "AGENTS.md"
        assert kwargs["repository_id"] == "my-repo"
        assert kwargs["project"] == "my-project"
        assert kwargs["version_descriptor"].version == "base-sha"
        assert kwargs["version_descriptor"].version_type == "commit"

    def test_get_repo_file_content_from_default_branch_omits_version(self):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr = MagicMock()
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_item.return_value = MagicMock(content="repo context")

        content = provider.get_repo_file_content("AGENTS.md", from_default_branch=True)

        assert content == "repo context"
        _, kwargs = provider.azure_devops_client.get_item.call_args
        assert kwargs["version_descriptor"] is None  # no version -> default branch

    def test_get_repo_file_content_treats_missing_file_as_empty(self):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr = MagicMock()
        provider.pr.last_merge_target_commit.commit_id = "base-sha"
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_item.side_effect = Exception("Operation returned a 404 status code.")

        assert provider.get_repo_file_content("MISSING.md") == ""

    def test_get_repo_file_content_propagates_non_404_errors(self):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr = MagicMock()
        provider.pr.last_merge_target_commit.commit_id = "base-sha"
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_item.side_effect = Exception("Operation returned a 500 status code.")

        with pytest.raises(Exception, match="500 status code"):
            provider.get_repo_file_content("AGENTS.md")


class TestAzureDevopsProviderFiles:
    @staticmethod
    def _change(path="/src/app.py", change_type="edit", git_object_type="blob", **metadata):
        return SimpleNamespace(
            additional_properties={
                "item": {"path": path, "gitObjectType": git_object_type},
                "changeType": change_type,
                **metadata,
            }
        )

    @classmethod
    def _provider(cls, pages=None):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr_num = 1
        provider.pr = SimpleNamespace(
            last_merge_target_commit=SimpleNamespace(commit_id="base-sha"),
            last_merge_commit=SimpleNamespace(commit_id="head-sha"),
        )
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_pull_request_iterations.return_value = [SimpleNamespace(id=7)]
        if pages is not None:
            provider.azure_devops_client.get_pull_request_iteration_changes.side_effect = pages
        provider.azure_devops_client.get_item.side_effect = lambda **kwargs: SimpleNamespace(
            content=f"content for {kwargs['path']}\n"
        )
        provider.diff_files = None
        provider._diff_path_map = None
        provider._pr_iteration_changes_cache = None
        provider.incremental = None
        provider.unreviewed_files_map = {}
        return provider

    def test_complete_iteration_collection_is_shared_by_file_consumers(self):
        entries = [self._change(f"/src/file_{index:03}.py") for index in range(101)]
        provider = self._provider([
            SimpleNamespace(change_entries=entries[:100], next_skip=100, next_top=1),
            SimpleNamespace(change_entries=entries[100:], next_skip=0, next_top=0),
        ])

        assert provider.get_files() == [entry.additional_properties["item"]["path"] for entry in entries]
        assert [file.filename for file in provider.get_diff_files()] == provider.get_files()
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_count == 2
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_args_list[0].kwargs == {
            "repository_id": "my-repo",
            "pull_request_id": 1,
            "iteration_id": 7,
            "project": "my-project",
            "top": 2000,
            "skip": 0,
            "compare_to": 0,
        }
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_args_list[1].kwargs["top"] == 1
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_args_list[1].kwargs["skip"] == 100

    def test_collection_follows_continuation_beyond_max_page_size(self):
        entries = [self._change(f"/src/file_{index:04}.py") for index in range(2007)]
        provider = self._provider([
            SimpleNamespace(change_entries=entries[:2000], next_skip=2000, next_top=7),
            SimpleNamespace(change_entries=entries[2000:], next_skip=0, next_top=0),
        ])

        assert provider._get_pr_iteration_changes() == entries
        calls = provider.azure_devops_client.get_pull_request_iteration_changes.call_args_list
        assert [(call.kwargs["skip"], call.kwargs["top"]) for call in calls] == [(0, 2000), (2000, 7)]

    def test_collection_handles_terminal_empty_page_and_no_iterations(self):
        provider = self._provider([
            SimpleNamespace(change_entries=[], next_skip=0, next_top=0),
        ])

        assert provider._get_pr_iteration_changes() == []
        assert provider.get_files() == []
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_count == 1

        provider = self._provider()
        provider.azure_devops_client.get_pull_request_iterations.return_value = []

        assert provider._get_pr_iteration_changes() == []
        provider.azure_devops_client.get_pull_request_iteration_changes.assert_not_called()

    def test_collection_rejects_non_advancing_continuation(self):
        provider = self._provider([
            SimpleNamespace(change_entries=[self._change("/first.py")], next_skip=100, next_top=100),
            SimpleNamespace(change_entries=[self._change("/second.py")], next_skip=100, next_top=100),
        ])

        with pytest.raises(RuntimeError, match="continuation"):
            provider._get_pr_iteration_changes()

        assert provider._pr_iteration_changes_cache is None

    @pytest.mark.parametrize("next_skip,next_top", [(0, 1), (1, 0), ("1", 1), (True, 1)])
    def test_collection_rejects_malformed_continuation(self, next_skip, next_top):
        provider = self._provider([
            SimpleNamespace(
                change_entries=[self._change("/first.py")],
                next_skip=next_skip,
                next_top=next_top,
            ),
        ])

        with pytest.raises(RuntimeError, match="continuation"):
            provider._get_pr_iteration_changes()

        assert provider._pr_iteration_changes_cache is None

    def test_later_page_failure_does_not_cache_partial_collection(self):
        entries = [self._change(f"/src/file_{index:03}.py") for index in range(101)]
        provider = self._provider()
        provider.azure_devops_client.get_pull_request_iteration_changes.side_effect = [
            SimpleNamespace(change_entries=entries[:100], next_skip=100, next_top=1),
            RuntimeError("page failed"),
            SimpleNamespace(change_entries=entries[:100], next_skip=100, next_top=1),
            SimpleNamespace(change_entries=entries[100:], next_skip=0, next_top=0),
        ]

        with pytest.raises(RuntimeError, match="page failed"):
            provider._get_pr_iteration_changes()
        assert provider._pr_iteration_changes_cache is None

        assert provider._get_pr_iteration_changes() == entries
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_count == 4

    def test_get_files_uses_current_iteration_and_skips_non_file_entries(self):
        provider = self._provider([
            SimpleNamespace(
                change_entries=[
                    self._change("/src", git_object_type="tree"),
                    self._change(""),
                    self._change("/src/current.py"),
                ],
                next_skip=0,
                next_top=0,
            ),
        ])
        provider.azure_devops_client.get_pull_request_commits.return_value = [SimpleNamespace(commit_id="old")]
        provider.azure_devops_client.get_changes.return_value = SimpleNamespace(
            changes=[
                {"item": {"path": "/src/reverted.py"}},
                {"item": {"path": "/src/current.py"}},
            ]
        )

        assert provider.get_files() == ["/src/current.py"]
        provider.azure_devops_client.get_pull_request_commits.assert_not_called()
        provider.azure_devops_client.get_changes.assert_not_called()

    @classmethod
    def _provider_with_change(cls, change, *get_item_results, incremental=None):
        provider = cls._provider([
            SimpleNamespace(change_entries=[change], next_skip=0, next_top=0),
        ])
        provider.azure_devops_client.get_item.side_effect = get_item_results
        provider.incremental = incremental
        return provider

    @staticmethod
    def _provider_with_pull_request_diff(*get_item_results):
        change = TestAzureDevopsProviderFiles._change()
        return TestAzureDevopsProviderFiles._provider_with_change(change, *get_item_results)

    @staticmethod
    def _status_error(status_code):
        error = Exception(f"Operation returned a {status_code} status code.")
        error.status_code = status_code
        return error

    @staticmethod
    def _azure_item_not_found_error():
        wrapped_error = SimpleNamespace(
            inner_exception=None,
            message="The specified item does not exist at the specified version.",
            exception_id=None,
            type_name="Microsoft.TeamFoundation.Git.Server.GitItemNotFoundException",
            type_key="GitItemNotFoundException",
            error_code=0,
            event_id=0,
            custom_properties=None,
        )
        return AzureDevOpsServiceError(wrapped_error)

    @classmethod
    def _provider_with_incremental_rename(cls, *get_item_results):
        incremental = IncrementalPR(True)
        incremental.last_seen_commit = SimpleNamespace(sha="last-seen-sha")
        provider = cls._provider_with_change(
            cls._change(
                path="/new/name.py",
                change_type="rename",
                originalPath="/old/name.py",
            ),
            *get_item_results,
            incremental=incremental,
        )
        provider.unreviewed_files_map = {"/new/name.py": "/new/name.py"}
        return provider

    def test_get_diff_files_keeps_file_when_new_content_fetch_fails(self):
        provider = self._provider_with_pull_request_diff(
            Exception("head fetch failed"),
            SimpleNamespace(content="old content\n"),
        )

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_files = provider.get_diff_files()
        finally:
            get_logger().remove(sink_id)

        assert len(diff_files) == 1
        assert diff_files[0].filename == "/src/app.py"
        assert diff_files[0].head_file == ""
        assert diff_files[0].base_file == "old content\n"
        assert any("/src/app.py" in message and "head-sha" in message for message in captured)

    def test_get_diff_files_keeps_file_when_original_content_fetch_fails(self):
        provider = self._provider_with_pull_request_diff(
            SimpleNamespace(content="new content\n"),
            Exception("base fetch failed"),
        )

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_files = provider.get_diff_files()
        finally:
            get_logger().remove(sink_id)

        assert len(diff_files) == 1
        assert diff_files[0].filename == "/src/app.py"
        assert diff_files[0].head_file == "new content\n"
        assert diff_files[0].base_file == ""
        assert any("/src/app.py" in message and "base-sha" in message for message in captured)

    def test_pure_rename_uses_old_path_and_preserves_identity(self):
        change = self._change(
            path="/new/name.py",
            change_type="rename",
            originalPath="/old/name.py",
        )
        provider = self._provider_with_change(
            change,
            SimpleNamespace(content="same content\n"),
            SimpleNamespace(content="same content\n"),
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.filename == "/new/name.py"
        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.edit_type == EDIT_TYPE.RENAMED
        assert diff_file.base_file == diff_file.head_file == "same content\n"
        assert diff_file.patch == ""
        assert diff_file.num_plus_lines == 0
        assert diff_file.num_minus_lines == 0
        calls = provider.azure_devops_client.get_item.call_args_list
        assert calls[0].kwargs["path"] == "/new/name.py"
        assert calls[0].kwargs["version_descriptor"].version == "head-sha"
        assert calls[1].kwargs["path"] == "/old/name.py"
        assert calls[1].kwargs["version_descriptor"].version == "base-sha"

    def test_rename_on_later_page_preserves_old_path_and_base_content(self):
        provider = self._provider([
            SimpleNamespace(
                change_entries=[self._change("/src/first.py")],
                next_skip=1,
                next_top=1,
            ),
            SimpleNamespace(
                change_entries=[
                    self._change(
                        path="/new/name.py",
                        change_type="rename",
                        originalPath="/old/name.py",
                    )
                ],
                next_skip=0,
                next_top=0,
            ),
        ])

        diff_files = provider.get_diff_files()

        assert [diff_file.filename for diff_file in diff_files] == ["/src/first.py", "/new/name.py"]
        assert diff_files[1].old_filename == "/old/name.py"
        assert diff_files[1].head_file == "content for /new/name.py\n"
        assert diff_files[1].base_file == "content for /old/name.py\n"
        assert provider.azure_devops_client.get_pull_request_iteration_changes.call_count == 2

    def test_rename_with_edit_only_reports_the_actual_change_and_keeps_head_anchor(self):
        change = self._change(
            path="/new/name.py",
            change_type="edit, rename",
            originalPath="/old/name.py",
        )
        provider = self._provider_with_change(
            change,
            SimpleNamespace(content="value = 2\n"),
            SimpleNamespace(content="value = 1\n"),
        )

        diff_file = provider.get_diff_files()[0]
        inline_comment = provider.create_inline_comment("body", "/new/name.py", "value = 2")

        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.num_plus_lines == 1
        assert diff_file.num_minus_lines == 1
        assert "+value = 2" in diff_file.patch
        assert "-value = 1" in diff_file.patch
        assert inline_comment["path"] == "/new/name.py"
        assert inline_comment["subject_type"] == "LINE"

    @pytest.mark.parametrize(
        ("change", "expected_old_path"),
        [
            (
                SimpleNamespace(
                    original_path="/attribute/original.py",
                    source_server_item="/attribute/source.py",
                    additional_properties={
                        "item": {"path": "/new/name.py"},
                        "changeType": "rename",
                        "originalPath": "/mapping/original.py",
                        "sourceServerItem": "/mapping/source.py",
                    },
                ),
                "/attribute/original.py",
            ),
            (
                SimpleNamespace(
                    original_path="",
                    source_server_item="/attribute/source.py",
                    additional_properties={
                        "item": {"path": "/new/name.py"},
                        "changeType": "rename",
                        "originalPath": "/mapping/original.py",
                    },
                ),
                "/mapping/original.py",
            ),
            (
                SimpleNamespace(
                    additional_properties={
                        "item": {"path": "/new/name.py"},
                        "changeType": "rename",
                        "sourceServerItem": "/mapping/source.py",
                    },
                ),
                "/mapping/source.py",
            ),
            (
                SimpleNamespace(
                    item=SimpleNamespace(path="/new/name.py"),
                    change_type="rename",
                    source_server_item="/attribute/source.py",
                    additional_properties={},
                ),
                "/attribute/source.py",
            ),
        ],
    )
    def test_rename_old_path_metadata_precedence(self, change, expected_old_path):
        provider = self._provider_with_change(
            change,
            SimpleNamespace(content="new\n"),
            SimpleNamespace(content="old\n"),
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.old_filename == expected_old_path
        assert provider.azure_devops_client.get_item.call_args_list[1].kwargs["path"] == expected_old_path

    @pytest.mark.parametrize(
        "metadata",
        [
            {},
            {"originalPath": {"path": "/old/name.py"}, "sourceServerItem": 17},
            {"originalPath": "   ", "sourceServerItem": []},
        ],
    )
    def test_rename_with_missing_or_malformed_old_path_keeps_empty_base_fallback(self, metadata):
        change = self._change(path="/new/name.py", change_type="rename", **metadata)
        provider = self._provider_with_change(change, SimpleNamespace(content="new\n"))

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_file = provider.get_diff_files()[0]
        finally:
            get_logger().remove(sink_id)

        assert diff_file.old_filename is None
        assert diff_file.base_file == ""
        assert provider.azure_devops_client.get_item.call_count == 1
        assert any("/new/name.py" in message and "no usable old path" in message for message in captured)

    def test_incremental_rename_reads_old_path_at_last_seen_commit(self):
        provider = self._provider_with_incremental_rename(
            SimpleNamespace(content="new\n"),
            SimpleNamespace(content="old\n"),
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.old_filename == "/old/name.py"
        old_content_call = provider.azure_devops_client.get_item.call_args_list[1]
        assert old_content_call.kwargs["path"] == "/old/name.py"
        assert old_content_call.kwargs["version_descriptor"].version == "last-seen-sha"
        assert provider.unreviewed_files_map["/new/name.py"] == diff_file.patch

    def test_incremental_rename_retries_current_path_when_old_path_is_missing_at_checkpoint(self):
        provider = self._provider_with_incremental_rename(
            SimpleNamespace(content="value = 3\n"),
            self._azure_item_not_found_error(),
            SimpleNamespace(content="value = 2\n"),
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.edit_type == EDIT_TYPE.RENAMED
        assert diff_file.base_file == "value = 2\n"
        assert diff_file.head_file == "value = 3\n"
        assert "-value = 2" in diff_file.patch
        assert "+value = 3" in diff_file.patch
        assert provider.unreviewed_files_map["/new/name.py"] == diff_file.patch
        calls = provider.azure_devops_client.get_item.call_args_list
        assert [call.kwargs["path"] for call in calls] == [
            "/new/name.py",
            "/old/name.py",
            "/new/name.py",
        ]
        assert [call.kwargs["version_descriptor"].version for call in calls] == [
            "head-sha",
            "last-seen-sha",
            "last-seen-sha",
        ]

    @pytest.mark.parametrize("retry_status_code", [404, 500])
    def test_incremental_rename_uses_empty_base_when_both_checkpoint_paths_fail(self, retry_status_code):
        provider = self._provider_with_incremental_rename(
            SimpleNamespace(content="new\n"),
            self._status_error(404),
            self._status_error(retry_status_code),
        )

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_file = provider.get_diff_files()[0]
        finally:
            get_logger().remove(sink_id)

        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.edit_type == EDIT_TYPE.RENAMED
        assert diff_file.base_file == ""
        assert [call.kwargs["path"] for call in provider.azure_devops_client.get_item.call_args_list] == [
            "/new/name.py",
            "/old/name.py",
            "/new/name.py",
        ]
        assert any(
            "/new/name.py" in message and "last-seen-sha" in message and "retry" in message
            for message in captured
        )

    def test_incremental_rename_does_not_retry_after_successful_empty_old_path_read(self):
        provider = self._provider_with_incremental_rename(
            SimpleNamespace(content="new\n"),
            SimpleNamespace(content=""),
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.base_file == ""
        assert [call.kwargs["path"] for call in provider.azure_devops_client.get_item.call_args_list] == [
            "/new/name.py",
            "/old/name.py",
        ]

    def test_incremental_rename_does_not_retry_current_path_after_non_404_failure(self):
        provider = self._provider_with_incremental_rename(
            SimpleNamespace(content="new\n"),
            self._status_error(500),
        )

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_file = provider.get_diff_files()[0]
        finally:
            get_logger().remove(sink_id)

        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.edit_type == EDIT_TYPE.RENAMED
        assert diff_file.base_file == ""
        assert [call.kwargs["path"] for call in provider.azure_devops_client.get_item.call_args_list] == [
            "/new/name.py",
            "/old/name.py",
        ]
        assert any(
            "/old/name.py" in message and "last-seen-sha" in message and "500" in message
            for message in captured
        )

    def test_failed_rename_old_content_read_keeps_identity_and_logs_the_failure(self):
        change = self._change(
            path="/new/name.py",
            change_type="rename",
            originalPath="/old/name.py",
        )
        provider = self._provider_with_change(
            change,
            SimpleNamespace(content="new\n"),
            Exception("base fetch failed"),
        )

        captured = []
        sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
        try:
            diff_file = provider.get_diff_files()[0]
        finally:
            get_logger().remove(sink_id)

        assert diff_file.old_filename == "/old/name.py"
        assert diff_file.base_file == ""
        assert any("/old/name.py" in message and "base-sha" in message for message in captured)

    @pytest.mark.parametrize(
        ("change_type", "get_item_results", "expected_type", "expected_base", "expected_paths"),
        [
            (
                "add",
                (SimpleNamespace(content="new\n"),),
                EDIT_TYPE.ADDED,
                "",
                ["/src/app.py"],
            ),
            (
                "edit",
                (SimpleNamespace(content="new\n"), SimpleNamespace(content="old\n")),
                EDIT_TYPE.MODIFIED,
                "old\n",
                ["/src/app.py", "/src/app.py"],
            ),
            (
                "delete",
                (Exception("head missing"), SimpleNamespace(content="old\n")),
                EDIT_TYPE.DELETED,
                "old\n",
                ["/src/app.py", "/src/app.py"],
            ),
        ],
    )
    def test_non_rename_change_handling_is_unchanged(
        self, change_type, get_item_results, expected_type, expected_base, expected_paths
    ):
        provider = self._provider_with_change(
            self._change(change_type=change_type),
            *get_item_results,
        )

        diff_file = provider.get_diff_files()[0]

        assert diff_file.edit_type == expected_type
        assert diff_file.old_filename is None
        assert diff_file.base_file == expected_base
        assert [call.kwargs["path"] for call in provider.azure_devops_client.get_item.call_args_list] == expected_paths


def _provider_with_diff(*filenames):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.repo_slug = "my-repo"
    provider.workspace_slug = "my-project"
    provider.pr_num = 1
    provider.temp_comments = []
    provider.azure_devops_client = MagicMock()
    provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="\n".join(f"line {line}" for line in range(1, 13)),
            patch="",
            filename=filename,
        )
        for filename in filenames
    ]
    return provider


def _created_threads(provider):
    return [kwargs["comment_thread"] for _, kwargs in provider.azure_devops_client.create_thread.call_args_list]


def _suggestion(relevant_file):
    return {
        "body": "```suggestion\nfixed\n```",
        "relevant_file": relevant_file,
        "relevant_lines_start": 10,
        "relevant_lines_end": 12,
    }


class TestAzureDevopsProviderPersistentComments:
    def test_failed_persistent_edit_publishes_the_new_result(self):
        provider = _provider_with_diff("/src/app.py")
        existing = MagicMock(body="## PR Code Suggestions ✨\n\nold suggestions")
        fallback = MagicMock()
        provider.get_issue_comments = MagicMock(return_value=[existing])
        provider.get_latest_commit_url = MagicMock(return_value="https://example.test/commit/deadbee")
        provider.get_comment_url = MagicMock(return_value="https://example.test/comment/1")
        provider.publish_comment = MagicMock(return_value=fallback)
        provider.azure_devops_client.update_comment.side_effect = RuntimeError("update failed")

        result = provider.publish_persistent_comment(
            "## PR Code Suggestions ✨\n\nnew suggestions",
            "## PR Code Suggestions ✨",
            final_update_message=False,
        )

        assert result is fallback
        provider.publish_comment.assert_called_once()


class TestAzureDevopsProviderSuggestionAnchoring:
    def test_suggestion_without_leading_slash_is_published_with_the_diff_path(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        assert provider.publish_code_suggestions([_suggestion("src/Api/Controllers/SomeController.cs")]) is True

        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context.file_path == "/src/Api/Controllers/SomeController.cs"
        assert threads[0].comments[0].content == _suggestion("/src/Api/Controllers/SomeController.cs")["body"]
        assert threads[0].thread_context.right_file_start.line == 10
        assert threads[0].thread_context.right_file_end.line == 12

    def test_suggestion_span_covers_the_complete_final_line(self):
        provider = _provider_with_diff("/src/app.py")
        provider.diff_files[0].head_file = "\n".join([
            *(f"line {line}" for line in range(1, 10)),
            "    if ready:",
            "        run()",
            "    }",
        ])

        provider.publish_code_suggestions([_suggestion("/src/app.py")])

        context = _created_threads(provider)[0].thread_context
        assert context.right_file_start.offset == 1
        assert context.right_file_end.offset == 6

    def test_suggestion_end_offset_uses_utf16_code_units(self):
        provider = _provider_with_diff("/src/app.py")
        provider.diff_files[0].head_file = "\n".join([
            *(f"line {line}" for line in range(1, 12)),
            "return '😀'",
        ])

        provider.publish_code_suggestions([_suggestion("/src/app.py")])

        context = _created_threads(provider)[0].thread_context
        assert context.right_file_end.offset == 12

    def test_suggestion_with_unavailable_final_line_becomes_a_pr_level_comment(self):
        provider = _provider_with_diff("/src/app.py")
        provider.diff_files[0].head_file = "line 1"

        provider.publish_code_suggestions([_suggestion("/src/app.py")])

        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context is None
        assert "could not resolve the complete line range" in threads[0].comments[0].content

    def test_unavailable_final_line_does_not_stop_the_batch(self):
        provider = _provider_with_diff("/src/short.py", "/src/complete.py")
        provider.diff_files[0].head_file = "line 1"

        provider.publish_code_suggestions([
            _suggestion("/src/short.py"),
            _suggestion("/src/complete.py"),
        ])

        threads = _created_threads(provider)
        anchored = [thread for thread in threads if thread.thread_context is not None]
        assert len(anchored) == 1
        assert anchored[0].thread_context.file_path == "/src/complete.py"

    def test_unavailable_final_line_respects_disabled_fallback(self):
        provider = _provider_with_diff("/src/app.py")
        provider.diff_files[0].head_file = "line 1"
        suggestion = _suggestion("/src/app.py")
        suggestion["fallback_to_pr_comment"] = False

        assert provider.publish_code_suggestions([suggestion]) is False
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_regular_inline_finding_keeps_its_existing_character_anchor(self):
        provider = _provider_with_diff("/src/app.py")
        finding = _suggestion("/src/app.py")
        finding["body"] = "Review finding"

        provider.publish_code_suggestions([finding])

        context = _created_threads(provider)[0].thread_context
        assert context.right_file_start.offset == 1
        assert context.right_file_end.offset == 1

    def test_persistent_inline_comments_skip_existing_code_suggestion(self):
        provider = _provider_with_diff("/src/app.py")
        body = "**Suggestion:** use the value\n```suggestion\nvalue = 1\n```"
        marker = code_fingerprint("src/app.py", "1-1", body)
        existing = MagicMock()
        existing.thread_context = MagicMock()
        existing.thread_context.file_path = "/src/app.py"
        existing.thread_context.right_file_start = SimpleNamespace(line=1)
        existing.thread_context.right_file_end = SimpleNamespace(line=1)
        existing.status = "fixed"
        existing.comments = [MagicMock(content=f"different wording\n\n<!-- pr-agent-dedup-code: {marker} -->")]
        provider.azure_devops_client.get_threads.return_value = [existing]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([{
                "body": body,
                "relevant_file": "/src/app.py",
                "relevant_lines_start": 1,
                "relevant_lines_end": 1,
            }])

        assert result is True
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_persistent_inline_comments_bootstrap_markerless_suggestion(self):
        provider = _provider_with_diff("/src/app.py")
        body = "**Suggestion:** use the value\n```suggestion\nvalue = 1\n```"
        existing = MagicMock()
        existing.thread_context = MagicMock()
        existing.thread_context.file_path = "/src/app.py"
        existing.thread_context.right_file_start = SimpleNamespace(line=5)
        existing.thread_context.right_file_end = SimpleNamespace(line=5)
        existing.comments = [MagicMock(content=body)]
        provider.azure_devops_client.get_threads.return_value = [existing]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([{
                "body": "**Suggestion:** different wording\n```suggestion\nvalue = 1\n```",
                "relevant_file": "/src/app.py",
                "relevant_lines_start": 5,
                "relevant_lines_end": 5,
            }])

        assert result is True
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_persistent_inline_comments_mark_new_suggestion_and_deduplicate_batch(self):
        provider = _provider_with_diff("/src/app.py")
        body = "**Suggestion:** use the value\n```suggestion\nvalue = 1\n```"
        suggestion = {
            "body": body,
            "relevant_file": "/src/app.py",
            "relevant_lines_start": 1,
            "relevant_lines_end": 1,
            "original_suggestion": {
                "existing_code": "value = 2",
                "improved_code": "value = 1",
            },
        }

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([suggestion, suggestion])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 1
        thread = _created_threads(provider)[0]
        assert "<!-- pr-agent-dedup:" in thread.comments[0].content
        assert "<!-- pr-agent-dedup-code:" in thread.comments[0].content

    def test_persistent_inline_comments_publish_distinct_issues_on_same_lines(self):
        provider = _provider_with_diff("/src/app.py")
        first = _suggestion("/src/app.py")
        second = _suggestion("/src/app.py")
        second["body"] = "**Suggestion:** use another rewrite\n```suggestion\nother\n```"

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([first, second])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 2

    def test_persistent_inline_comments_publish_same_issue_at_distinct_ranges(self):
        provider = _provider_with_diff("/src/app.py")
        first = _suggestion("/src/app.py")
        second = dict(first, relevant_lines_start=20, relevant_lines_end=22)

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([first, second])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 2

    def test_persistent_inline_comments_compare_the_complete_issue_body(self):
        provider = _provider_with_diff("/src/app.py")
        common_prefix = "**Suggestion:** " + "same context " * 10
        first = _suggestion("/src/app.py")
        first["body"] = f"{common_prefix}first issue\n```suggestion\nfirst\n```"
        second = _suggestion("/src/app.py")
        second["body"] = f"{common_prefix}second issue\n```suggestion\nsecond\n```"

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([first, second])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 2

    def test_persistent_inline_comments_bootstrap_distinct_range(self):
        provider = _provider_with_diff("/src/app.py")
        body = "**Suggestion:** use the value\n```suggestion\nvalue = 1\n```"
        existing = SimpleNamespace(
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=10),
                right_file_end=SimpleNamespace(line=12),
            ),
            comments=[SimpleNamespace(content=body)],
        )
        provider.azure_devops_client.get_threads.return_value = [existing]
        suggestion = dict(_suggestion("/src/app.py"), relevant_lines_start=20, relevant_lines_end=22)

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([suggestion])

        assert result is True
        provider.azure_devops_client.create_thread.assert_called_once()

    def test_persistent_inline_comments_ignore_suggestion_blocks_in_replies(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = dict(_suggestion("/src/app.py"), relevant_lines_start=1, relevant_lines_end=1)
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=1),
                right_file_end=SimpleNamespace(line=1),
            ),
            comments=[
                SimpleNamespace(content="Developer discussion"),
                SimpleNamespace(content=suggestion["body"]),
            ],
        )]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([suggestion])

        assert result is True
        provider.azure_devops_client.create_thread.assert_called_once()

    def test_persistent_inline_comments_mark_pr_level_fallback(self):
        provider = _provider_with_diff("/src/app.py")

        with (patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings,
              patch("pr_agent.algo.utils.get_settings") as heading_settings):
            settings.return_value.get.side_effect = lambda key, default=None: {
                "config.persistent_inline_comments": True,
            }.get(key, default)
            settings.return_value.azure_devops.get.return_value = "active"
            heading_settings.return_value.get.return_value = "Team Improvements"
            result = provider.publish_code_suggestions([_suggestion("/src/removed.py")])

        assert result is True
        body = _created_threads(provider)[0].comments[0].content
        assert body.startswith(
            "## Team Improvements ✨\n\n"
            f"{PRCodeSuggestionsIdentity.UNANCHORED.value}\n\n"
        )
        assert "<!-- pr-agent-dedup:" in body
        assert "<!-- pr-agent-dedup-code:" in body

    def test_persistent_inline_comments_deduplicate_unresolved_ranges(self):
        provider = _provider_with_diff("/src/app.py")
        provider.diff_files[0].head_file = "line 1"
        suggestion = _suggestion("/src/app.py")

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([suggestion, suggestion])

        assert result is True
        body = _created_threads(provider)[0].comments[0].content
        assert body.count("could not resolve the complete line range") == 1

    def test_persistent_inline_comments_remember_fallback_in_same_provider(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = _suggestion("/src/removed.py")

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            assert provider.publish_code_suggestions([suggestion]) is True
            assert provider.publish_code_suggestions([suggestion]) is True

        provider.azure_devops_client.create_thread.assert_called_once()

    def test_persistent_inline_comments_bootstrap_pr_level_fallback(self):
        provider = _provider_with_diff("/src/app.py")
        existing = MagicMock(thread_context=None)
        existing.comments = [MagicMock(
            content="`/src/removed.py` (lines 10-12) - could not be anchored\n\n"
                    "```suggestion\nfixed\n```"
        )]
        provider.azure_devops_client.get_threads.return_value = [existing]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([_suggestion("src/removed.py")])

        assert result is True
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_persistent_inline_comments_bootstrap_pr_level_fallback_without_a_code_block(self):
        provider = _provider_with_diff("/src/app.py")
        body = "**Suggestion:** rename the variable"
        existing = MagicMock(thread_context=None)
        existing.comments = [MagicMock(
            content="## PR Code Suggestions ✨\n\n"
                    f"{PRCodeSuggestionsIdentity.UNANCHORED.value}\n\n"
                    f"`/src/removed.py` (lines 10-12) - could not be anchored\n\n{body}"
        )]
        provider.azure_devops_client.get_threads.return_value = [existing]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([{
                "body": body,
                "relevant_file": "/src/removed.py",
                "relevant_lines_start": 10,
                "relevant_lines_end": 12,
            }])

        assert result is True
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_suggestion_with_matching_path_is_published_unchanged(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/SomeController.cs")])

        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context.file_path == "/src/Api/Controllers/SomeController.cs"

    def test_suggestion_with_extra_leading_slash_is_published_with_the_diff_path(self):
        provider = _provider_with_diff("src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/SomeController.cs")])

        assert _created_threads(provider)[0].thread_context.file_path == "src/Api/Controllers/SomeController.cs"

    def test_suggestion_with_padded_backticks_is_published_with_the_diff_path(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("` src/Api/Controllers/SomeController.cs `")])

        assert _created_threads(provider)[0].thread_context.file_path == "/src/Api/Controllers/SomeController.cs"

    def test_unmatched_suggestion_becomes_a_pr_level_comment_instead_of_an_orphaned_thread(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/Removed.cs")])

        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context is None
        body = threads[0].comments[0].content
        assert "/src/Api/Controllers/Removed.cs" in body
        assert "fixed" in body

    def test_unmatched_suggestions_are_published_in_one_comment(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/First.cs"),
            _suggestion("/src/Api/Controllers/Second.cs"),
        ])

        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context is None
        body = threads[0].comments[0].content
        assert "/src/Api/Controllers/First.cs" in body
        assert "/src/Api/Controllers/Second.cs" in body

    def test_diff_path_index_is_reused_for_a_batch(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.get_diff_files = MagicMock(return_value=provider.diff_files)

        provider.publish_code_suggestions([
            _suggestion("src/Api/Controllers/SomeController.cs"),
            _suggestion("/src/Api/Controllers/SomeController.cs"),
        ])

        provider.get_diff_files.assert_called_once_with()

    def test_transient_diff_failure_does_not_cache_an_empty_path_index(self):
        provider = _provider_with_diff()
        provider.diff_files = None
        diff_file = FilePatchInfo(
            base_file="",
            head_file="",
            patch="",
            filename="/src/Api/Controllers/SomeController.cs",
        )
        responses = iter([None, [diff_file]])

        def load_diff_files():
            provider.diff_files = next(responses)
            return provider.diff_files or []

        provider.get_diff_files = MagicMock(side_effect=load_diff_files)

        assert provider._resolve_diff_file_path("src/Api/Controllers/SomeController.cs") is None
        assert provider._resolve_diff_file_path("src/Api/Controllers/SomeController.cs") == diff_file.filename
        assert provider.get_diff_files.call_count == 2

    def test_incremental_mode_invalidates_the_diff_path_index(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider._diff_path_map = {"stale.cs": "/stale.cs"}
        provider._get_incremental_commits = MagicMock()
        incremental = MagicMock()
        incremental.is_incremental = True

        provider.get_incremental_commits(incremental)

        assert provider.diff_files is None
        assert provider._diff_path_map is None

    def test_full_mode_keeps_the_diff_path_index(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        diff_files = provider.diff_files
        path_map = {"somecontroller.cs": "/src/Api/Controllers/SomeController.cs"}
        provider._diff_path_map = path_map
        incremental = MagicMock()
        incremental.is_incremental = False

        provider.get_incremental_commits(incremental)

        assert provider.diff_files is diff_files
        assert provider._diff_path_map is path_map

    def test_set_pr_invalidates_the_diff_path_index(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider._diff_path_map = {"stale.cs": "/stale.cs"}
        provider.pr_commits = ["stale"]
        provider.previous_review = "stale"
        provider.unreviewed_files_map = {"stale.cs": "stale.cs"}
        provider._pr_iteration_changes_cache = ["stale"]
        provider.temp_comments = ["stale"]
        provider._parse_pr_url = MagicMock(return_value=("project", "repo", 2))
        provider._get_pr = MagicMock(return_value=MagicMock())

        provider.set_pr("https://dev.azure.com/example/project/_git/repo/pullrequest/2")

        assert provider.diff_files is None
        assert provider._diff_path_map is None
        assert provider.pr_commits is None
        assert provider.previous_review is None
        assert provider.unreviewed_files_map == {}
        assert provider._pr_iteration_changes_cache is None
        assert provider.temp_comments == []

    def test_unmatched_suggestion_path_does_not_break_markdown(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("` /src/Api/Controllers/Removed.cs `")])

        body = _created_threads(provider)[-1].comments[0].content
        assert body.startswith(
            "## PR Code Suggestions ✨\n\n"
            f"{PRCodeSuggestionsIdentity.UNANCHORED.value}\n\n"
        )
        assert "`/src/Api/Controllers/Removed.cs` (lines 10-12)" in body

    def test_aggregate_fallback_retries_suggestions_individually(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = [RuntimeError("request failed"),
                                                                  MagicMock(), MagicMock()]

        result = provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/First.cs"),
            _suggestion("/src/Api/Controllers/Second.cs"),
        ])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 3

    def test_unanchored_publish_failure_is_reported(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = RuntimeError("request failed")

        assert provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/Removed.cs")]) is False

    def test_anchored_publish_failure_is_reported(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = RuntimeError("request failed")

        assert provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/SomeController.cs")]) is False

    def test_braced_publish_error_does_not_stop_the_batch(self):
        provider = _provider_with_diff("/src/first.py", "/src/second.py")
        provider.azure_devops_client.create_thread.side_effect = [
            RuntimeError("request {'reason': 'failed'}"),
            MagicMock(),
            MagicMock(),
        ]

        result = provider.publish_code_suggestions([
            _suggestion("/src/first.py"),
            _suggestion("/src/second.py"),
        ])

        assert result is True
        assert provider.azure_devops_client.create_thread.call_count == 3

    def test_disabled_fallback_does_not_retry_a_failed_suggestion(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = RuntimeError("request failed")
        suggestion = _suggestion("/src/Api/Controllers/SomeController.cs")
        suggestion["fallback_to_pr_comment"] = False

        assert provider.publish_code_suggestions([suggestion]) is False
        assert provider.azure_devops_client.create_thread.call_count == 1

    def test_anchored_publish_failure_uses_the_publish_failure_reason(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = [RuntimeError("request failed"), MagicMock()]

        provider.publish_code_suggestions([_suggestion("/src/Api/Controllers/SomeController.cs")])

        fallback_body = _created_threads(provider)[-1].comments[0].content
        assert "could not be published as an inline comment" in fallback_body

    def test_malformed_suggestion_does_not_stop_the_batch(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([
            {"body": "missing location"},
            _suggestion("/src/Api/Controllers/SomeController.cs"),
        ])

        assert len(_created_threads(provider)) == 1

    @pytest.mark.parametrize("overrides", [
        {"relevant_file": 123},
        {"relevant_file": " "},
        {"relevant_file": "``"},
        {"body": None},
        {"relevant_lines_start": "10"},
        {"relevant_lines_start": True},
        {"relevant_lines_start": -2},
        {"relevant_lines_end": None},
    ])
    def test_invalid_values_do_not_stop_the_batch(self, overrides):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        malformed = _suggestion("/src/Api/Controllers/SomeController.cs")
        malformed.update(overrides)

        result = provider.publish_code_suggestions([
            malformed,
            _suggestion("/src/Api/Controllers/SomeController.cs"),
        ])

        assert result is True
        threads = _created_threads(provider)
        assert len(threads) == 1
        assert threads[0].thread_context.file_path == "/src/Api/Controllers/SomeController.cs"

    def test_diff_path_resolver_rejects_non_string_paths(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        assert provider._resolve_diff_file_path(123) is None

    def test_invalid_range_does_not_retry_successful_suggestions(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        invalid = _suggestion("/src/Api/Controllers/SomeController.cs")
        invalid["relevant_lines_start"] = -1

        result = provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/SomeController.cs"),
            invalid,
        ])

        assert result is True
        assert len(_created_threads(provider)) == 1

    def test_reversed_range_does_not_retry_successful_suggestions(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        invalid = _suggestion("/src/Api/Controllers/SomeController.cs")
        invalid["relevant_lines_start"] = 12
        invalid["relevant_lines_end"] = 10

        result = provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/SomeController.cs"),
            invalid,
        ])

        assert result is True
        assert len(_created_threads(provider)) == 1

    def test_partial_publish_failure_does_not_retry_successful_suggestions(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.azure_devops_client.create_thread.side_effect = [MagicMock(), RuntimeError("request failed"),
                                                                  RuntimeError("request failed"),
                                                                  RuntimeError("request failed")]

        result = provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/SomeController.cs"),
            _suggestion("src/Api/Controllers/SomeController.cs"),
        ])

        assert result is True

    def test_unmatched_suggestion_does_not_stop_the_remaining_suggestions(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([
            _suggestion("/src/Api/Controllers/Removed.cs"),
            _suggestion("src/Api/Controllers/SomeController.cs"),
        ])

        anchored = [t for t in _created_threads(provider) if t.thread_context is not None]
        assert len(anchored) == 1
        assert anchored[0].thread_context.file_path == "/src/Api/Controllers/SomeController.cs"


class TestAzureDevopsProviderCreateInlineComment:
    def test_resolved_line_comment_uses_the_diff_path(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider.diff_files[0].patch = "@@ -1,3 +1,4 @@\n context\n+    var x = 1;\n"
        provider.diff_files[0].head_file = " context\n    var x = 1;\n"

        comment = provider.create_inline_comment("body", "src/Api/Controllers/SomeController.cs", "    var x = 1;")

        assert comment["path"] == "/src/Api/Controllers/SomeController.cs"
        assert comment["subject_type"] == "LINE"

    def test_unresolved_line_returns_a_file_level_comment_instead_of_an_empty_dict(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        comment = provider.create_inline_comment("body", "src/Api/Controllers/SomeController.cs", "no such line")

        assert comment
        assert comment["subject_type"] == "FILE"
        assert comment["path"] == "/src/Api/Controllers/SomeController.cs"

    def test_file_level_comment_is_published_without_a_line_anchor(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_inline_comment("body", "src/Api/Controllers/SomeController.cs", "no such line")

        thread_context = _created_threads(provider)[0].thread_context
        assert thread_context == {"filePath": "/src/Api/Controllers/SomeController.cs"}

    def test_comment_on_a_file_outside_the_diff_becomes_a_pr_level_comment(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_inline_comment("body", "src/Api/Controllers/Removed.cs", "no such line")

        thread = _created_threads(provider)[0]
        assert thread.thread_context is None
        assert "src/Api/Controllers/Removed.cs" in thread.comments[0].content
        assert "body" in thread.comments[0].content

    def test_pr_level_fallback_removes_backticks_from_the_display_path(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_inline_comment("body", "src/Api/Controllers`Removed.cs", "no such line")

        body = _created_threads(provider)[0].comments[0].content
        assert body.startswith("`src/Api/ControllersRemoved.cs`")


class TestAzureDevopsProviderInlineComments:
    @staticmethod
    def _provider(threads):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr_num = 42
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_threads.return_value = threads
        provider.diff_files = [FilePatchInfo(base_file="", head_file="", patch="", filename="/app.py")]
        return provider

    def test_get_persistent_comment_bodies_returns_thread_roots(self):
        line_thread = SimpleNamespace(
            thread_context=SimpleNamespace(file_path="/app.py", right_file_start=SimpleNamespace(line=3)),
            comments=[SimpleNamespace(content="line finding"), SimpleNamespace(content="reply")],
        )
        file_thread = SimpleNamespace(
            thread_context=SimpleNamespace(file_path="/app.py", right_file_start=None),
            comments=[SimpleNamespace(content="file finding")],
        )
        pr_thread = SimpleNamespace(
            thread_context=None,
            comments=[SimpleNamespace(content="PR finding")],
        )
        provider = self._provider([line_thread, file_thread, pr_thread])

        assert provider.get_persistent_comment_bodies() == ["line finding", "file finding", "PR finding"]
        provider.azure_devops_client.get_threads.assert_called_once_with(
            repository_id="my-repo",
            pull_request_id=42,
            project="my-project",
        )

    def test_get_persistent_comment_bodies_includes_recent_successful_posts(self):
        provider = self._provider([])
        provider.publish_code_suggestions([{
            "body": "line finding",
            "relevant_file": "/app.py",
            "relevant_lines_start": 3,
            "relevant_lines_end": 3,
        }])

        assert provider.get_persistent_comment_bodies() == ["line finding"]

    def test_set_pr_clears_inline_comment_state(self):
        provider = self._provider([])
        provider._published_inline_comment_bodies = ["old finding"]
        provider._inline_comment_store = MagicMock()
        provider._parse_pr_url = MagicMock(return_value=("new-project", "new-repo", 43))
        provider._get_pr = MagicMock(return_value=MagicMock())

        provider.set_pr("https://dev.azure.com/example/new-project/_git/new-repo/pullrequest/43")

        assert provider._published_inline_comment_bodies == []
        assert provider._inline_comment_store is None

    def test_recent_inline_comment_bodies_returns_a_copy(self):
        provider = self._provider([])
        provider._published_inline_comment_bodies = ["line finding"]

        bodies = provider.get_recent_inline_comment_bodies()
        bodies.append("other finding")

        assert provider.get_recent_inline_comment_bodies() == ["line finding"]


class TestAzureDevopsProviderSuggestionReconciliation:
    @pytest.fixture(autouse=True)
    def _persistent_inline_comments(self):
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            yield settings

    @staticmethod
    def _thread(body, status="active"):
        return SimpleNamespace(
            id=17,
            status=status,
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=2),
            ),
            comments=[SimpleNamespace(content=body)],
        )

    @staticmethod
    def _marked_body(code="value = 1"):
        body = f"**Suggestion:** use the value\n```suggestion\n{code}\n```"
        marker = code_fingerprint("/src/app.py", None, body)
        return f"{body}\n\n<!-- pr-agent-dedup-code: {marker} -->"

    def _provider(self, thread, content="before\nvalue = 1\nafter"):
        provider = _provider_with_diff("/src/app.py")
        provider.pr = SimpleNamespace(last_merge_commit=SimpleNamespace(commit_id="head"))
        provider.azure_devops_client.get_threads.return_value = [thread]
        provider.azure_devops_client.get_item.return_value = SimpleNamespace(content=content)
        return provider

    def test_marks_exact_applied_suggestion_fixed(self):
        provider = self._provider(self._thread(self._marked_body()))

        assert provider.reconcile_code_suggestion_threads() == 1

        updated = provider.azure_devops_client.update_thread.call_args.args[0]
        assert updated.status == "fixed"

    def test_marks_closed_suggestion_fixed(self):
        provider = self._provider(self._thread(self._marked_body(), status="closed"))

        assert provider.reconcile_code_suggestion_threads() == 1

        updated = provider.azure_devops_client.update_thread.call_args.args[0]
        assert updated.status == "fixed"

    def test_leaves_unapplied_suggestion_active(self):
        provider = self._provider(self._thread(self._marked_body()), content="before\nvalue = 0\nafter")

        assert provider.reconcile_code_suggestion_threads() == 0
        provider.azure_devops_client.update_thread.assert_not_called()

    def test_leaves_empty_suggestion_active(self):
        provider = self._provider(self._thread(self._marked_body("")))

        assert provider.reconcile_code_suggestion_threads() == 0
        provider.azure_devops_client.update_thread.assert_not_called()

    def test_ignores_malformed_comment_content(self):
        thread = self._thread(self._marked_body())
        thread.comments.insert(0, SimpleNamespace(content=123))
        provider = self._provider(thread)

        assert provider.reconcile_code_suggestion_threads() == 1

    def test_respects_existing_terminal_status(self):
        provider = self._provider(self._thread(self._marked_body(), status="wontFix"))

        assert provider.reconcile_code_suggestion_threads() == 0
        provider.azure_devops_client.get_item.assert_not_called()
        provider.azure_devops_client.update_thread.assert_not_called()

    def test_reconciles_serialized_thread(self):
        thread = {
            "id": 18,
            "status": "active",
            "threadContext": {
                "filePath": "/src/app.py",
                "rightFileStart": {"line": 2},
            },
            "comments": [{"content": self._marked_body()}],
        }
        provider = self._provider(thread)

        assert provider.reconcile_code_suggestion_threads() == 1
        assert provider.azure_devops_client.update_thread.call_args.args[3] == 18

    def test_reconciliation_is_gated_by_persistent_inline_comments(self, _persistent_inline_comments):
        provider = self._provider(self._thread(self._marked_body()))
        _persistent_inline_comments.return_value.get.side_effect = (
            lambda key, default=None: default
        )

        assert provider.reconcile_code_suggestion_threads() == 0
        provider.azure_devops_client.get_threads.assert_not_called()
        provider.azure_devops_client.update_thread.assert_not_called()


class TestAzureDevopsProviderSuggestionDiscussions:
    def test_discovers_agent_mention_aliases_from_agent_comments(self):
        provider = _provider_with_diff("/src/app.py")
        agent = SimpleNamespace(
            id="agent-guid",
            display_name="Build Service (organization)",
            unique_name="agent@example.com",
        )
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(comments=[
            SimpleNamespace(content="## PR Code Suggestions ✨", author=agent),
            SimpleNamespace(content="A developer reply", author=SimpleNamespace(
                id="developer-guid",
                display_name="Developer",
                unique_name="developer@example.com",
            )),
        ])]

        assert provider.get_agent_mention_aliases() == {
            "agent-guid",
            "Build Service (organization)",
            "agent@example.com",
        }

    def test_discovers_agent_aliases_with_custom_headings(self):
        provider = _provider_with_diff("/src/app.py")
        agent = SimpleNamespace(id="agent-guid", display_name="Build Service", unique_name="agent@example.com")
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(comments=[
            SimpleNamespace(
                content=(
                    "## Team Guidelines ✨\n\n"
                    f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
                    "<table>suggestions</table>"
                ),
                author=agent,
            ),
        ])]

        assert provider.get_agent_mention_aliases() == {
            "agent-guid",
            "Build Service",
            "agent@example.com",
        }

    def test_uses_configured_agent_mention_aliases(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(comments=[
            SimpleNamespace(
                content="**Suggestion:** human comment",
                author=SimpleNamespace(
                    id="human-guid",
                    display_name="Developer",
                    unique_name="developer@example.com",
                ),
            ),
        ])]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.return_value = ["agent-guid", "Build Service (organization)"]
            aliases = provider.get_agent_mention_aliases()

        assert aliases == {"agent-guid", "Build Service (organization)"}
        provider.azure_devops_client.get_threads.assert_not_called()

    def test_does_not_discover_agent_aliases_from_developer_suggestions(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(comments=[
            SimpleNamespace(
                content="**Suggestion:** human comment",
                author=SimpleNamespace(
                    id="human-guid",
                    display_name="Developer",
                    unique_name="developer@example.com",
                ),
            ),
        ])]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.return_value = ""
            aliases = provider.get_agent_mention_aliases()

        assert aliases == set()

    def test_does_not_discover_agent_aliases_from_copied_dedup_markers(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [SimpleNamespace(comments=[
            SimpleNamespace(
                content="Copied suggestion\n\n<!-- pr-agent-dedup: 123456789abc -->",
                author=SimpleNamespace(id="human-guid", display_name="Developer"),
            ),
        ])]

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.return_value = ""
            aliases = provider.get_agent_mention_aliases()

        assert aliases == set()

    def test_formats_replies_from_suggestion_threads(self):
        provider = _provider_with_diff("/src/app.py")
        thread = SimpleNamespace(
            id=21,
            status="wontFix",
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=4),
                right_file_end=SimpleNamespace(line=6),
            ),
            comments=[
                SimpleNamespace(content="**Suggestion:** guard the value\n```suggestion\nsafe()\n```"),
                SimpleNamespace(
                    content="We will move this to the backlog.",
                    author=SimpleNamespace(display_name="Alex"),
                ),
            ],
        )
        provider.azure_devops_client.get_threads.return_value = [thread]

        discussions = json.loads(provider.get_code_suggestion_thread_context())

        assert discussions == [{
            "thread_id": 21,
            "status": "wontFix",
            "file": "/src/app.py",
            "start_line": 4,
            "end_line": 6,
            "suggestion": "**Suggestion:** guard the value\n```suggestion\nsafe()\n```",
            "replies": [{"author": "Alex", "message": "We will move this to the backlog."}],
        }]

    def test_includes_suggestion_threads_without_replies(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [
            SimpleNamespace(
                id=22,
                status="active",
                thread_context=SimpleNamespace(
                    file_path="/src/app.py",
                    right_file_start=SimpleNamespace(line=8),
                    right_file_end=SimpleNamespace(line=8),
                ),
                comments=[SimpleNamespace(content="**Suggestion:** use value\n```suggestion\nvalue\n```")],
            ),
            SimpleNamespace(comments=[
                SimpleNamespace(content="General discussion"),
                SimpleNamespace(content="A reply"),
            ]),
        ]

        assert json.loads(provider.get_code_suggestion_thread_context()) == [{
            "thread_id": 22,
            "status": "active",
            "file": "/src/app.py",
            "start_line": 8,
            "end_line": 8,
            "suggestion": "**Suggestion:** use value\n```suggestion\nvalue\n```",
            "replies": [],
        }]

    def test_includes_large_existing_suggestion_history(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [
            SimpleNamespace(
                id=thread_id,
                status="active",
                thread_context=SimpleNamespace(
                    file_path="/src/app.py",
                    right_file_start=SimpleNamespace(line=thread_id),
                    right_file_end=SimpleNamespace(line=thread_id),
                ),
                comments=[SimpleNamespace(
                    content=f"**Suggestion:** issue {thread_id}\n```suggestion\nvalue_{thread_id}\n```"
                )],
            )
            for thread_id in range(1, 31)
        ]

        discussions = json.loads(provider.get_code_suggestion_thread_context())

        assert len(discussions) == 30
        assert {discussion["thread_id"] for discussion in discussions} == set(range(1, 31))

    def test_adapts_azure_thread_comments_for_conversation_history(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_pull_request_thread.return_value = SimpleNamespace(comments=[
            SimpleNamespace(
                id=1,
                content="Original suggestion",
                author=SimpleNamespace(display_name="PR-Agent"),
            ),
            SimpleNamespace(
                id=2,
                content="Could this be nullable?\n\n<!-- pr-agent-response -->",
                author=SimpleNamespace(unique_name="developer@example.com"),
            ),
        ])

        comments = provider.get_review_thread_comments(21)

        assert [(comment.id, comment.body, comment.user.login) for comment in comments] == [
            (1, "Original suggestion", "PR-Agent"),
            (2, "Could this be nullable?", "developer@example.com"),
        ]

    def test_bounds_conversation_history(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_pull_request_thread.return_value = SimpleNamespace(comments=[
            SimpleNamespace(
                id=comment_id,
                content=f"{comment_id}:" + "x" * 800,
                author=SimpleNamespace(display_name="Developer"),
            )
            for comment_id in range(16)
        ])

        comments = provider.get_review_thread_comments(21)

        assert [comment.id for comment in comments] == [0, *range(6, 16)]
        assert all(len(comment.body) == 750 for comment in comments)

    def test_marks_thread_replies_as_agent_generated(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.create_comment.return_value = SimpleNamespace()

        provider.reply_to_thread(21, "Answer")

        comment = provider.azure_devops_client.create_comment.call_args.args[0]
        assert comment.content == "Answer\n\n<!-- pr-agent-response -->"

    def test_excludes_temporary_progress_reply_from_conversation_history(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.create_comment.return_value = SimpleNamespace()
        provider.reply_to_thread(21, "On it! ⏳", True)
        progress_content = provider.azure_devops_client.create_comment.call_args.args[0].content

        provider.azure_devops_client.get_pull_request_thread.return_value = SimpleNamespace(comments=[
            SimpleNamespace(
                id=1,
                content="Why is this nullable?",
                author=SimpleNamespace(unique_name="developer@example.com"),
            ),
            SimpleNamespace(
                id=2,
                content=progress_content,
                author=SimpleNamespace(display_name="PR-Agent"),
            ),
        ])

        comments = provider.get_review_thread_comments(21)

        assert [comment.id for comment in comments] == [1]

    def test_excludes_temporary_progress_reply_from_suggestion_discussions(self):
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.create_comment.return_value = SimpleNamespace()
        provider.reply_to_thread(21, "On it! ⏳", True)
        progress_content = provider.azure_devops_client.create_comment.call_args.args[0].content

        provider._threads_cache = [SimpleNamespace(
            id=21,
            status="active",
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=4),
                right_file_end=SimpleNamespace(line=4),
            ),
            comments=[
                SimpleNamespace(content="**Suggestion:** fix\n```suggestion\nvalue\n```"),
                SimpleNamespace(
                    content=progress_content,
                    author=SimpleNamespace(display_name="PR-Agent"),
                ),
                SimpleNamespace(
                    content="Rejected, keep as is.",
                    author=SimpleNamespace(unique_name="developer@example.com"),
                ),
            ],
        )]

        discussions = json.loads(provider.get_code_suggestion_thread_context())

        assert [reply["message"] for reply in discussions[0]["replies"]] == ["Rejected, keep as is."]


def test_azure_issue_comments_newest_first_does_not_reverse_twice():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    threads = [
        SimpleNamespace(
            id=1,
            comments=[SimpleNamespace(
                id=11,
                content="old comment",
                author=SimpleNamespace(unique_name="developer@example.com"),
            )],
        ),
        SimpleNamespace(
            id=2,
            comments=[SimpleNamespace(
                id=22,
                content="new comment",
                author=SimpleNamespace(unique_name="agent@example.com"),
            )],
        ),
    ]
    provider._get_threads = MagicMock(return_value=threads)

    comments = provider.get_issue_comments_newest_first()

    assert [comment.body for comment in comments] == ["new comment", "old comment"]


@patch("pr_agent.git_providers.azuredevops_provider.get_settings")
def test_azure_comment_authorship_uses_explicit_agent_identity(mock_get_settings):
    mock_get_settings.return_value.get.side_effect = lambda key, default=None: (
        "agent@example.com" if key == "azure_devops_server.agent_identity" else default
    )
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    agent_comment = SimpleNamespace(
        author=SimpleNamespace(unique_name="agent@example.com")
    )
    human_comment = SimpleNamespace(
        author=SimpleNamespace(unique_name="human@example.com")
    )

    assert provider.supports_review_finding_state() is True
    assert provider.is_comment_authored_by_pr_agent(agent_comment) is True
    assert provider.is_comment_authored_by_pr_agent(human_comment) is False


@patch("pr_agent.git_providers.azuredevops_provider.get_settings")
def test_azure_lifecycle_does_not_trust_display_name(mock_get_settings):
    mock_get_settings.return_value.get.side_effect = lambda key, default=None: (
        "PR Agent" if key == "azure_devops_server.agent_identity" else default
    )
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    comment = SimpleNamespace(
        author=SimpleNamespace(
            id="actual-agent-id",
            display_name="PR Agent",
            unique_name="different@example.com",
        )
    )

    assert provider.supports_review_finding_state() is False
    assert provider.is_comment_authored_by_pr_agent(comment) is False


@patch("pr_agent.git_providers.azuredevops_provider.get_settings")
def test_azure_lifecycle_matches_stable_identity_not_display_name(mock_get_settings):
    agent_id = "11111111-1111-1111-1111-111111111111"
    mock_get_settings.return_value.get.side_effect = lambda key, default=None: (
        agent_id if key == "azure_devops_server.agent_identity" else default
    )
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    matching_comment = SimpleNamespace(
        author=SimpleNamespace(
            id=agent_id,
            display_name="PR Agent",
            unique_name="other@example.com",
        )
    )
    same_name_different_id = SimpleNamespace(
        author=SimpleNamespace(
            id="22222222-2222-2222-2222-222222222222",
            display_name="PR Agent",
            unique_name="other@example.com",
        )
    )

    assert provider.supports_review_finding_state() is True
    assert provider.is_comment_authored_by_pr_agent(matching_comment) is True
    assert provider.is_comment_authored_by_pr_agent(same_name_different_id) is False


@patch("pr_agent.git_providers.azuredevops_provider.get_settings")
def test_azure_lifecycle_matches_descriptor_identity(mock_get_settings):
    descriptor = "aad.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mock_get_settings.return_value.get.side_effect = lambda key, default=None: (
        descriptor if key == "azure_devops_server.agent_identity" else default
    )
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    matching_comment = SimpleNamespace(
        author=SimpleNamespace(
            descriptor=descriptor,
            id=None,
            unique_name=None,
            display_name="PR Agent",
        )
    )
    same_name_different_descriptor = SimpleNamespace(
        author=SimpleNamespace(
            descriptor="aad.ffffffff-1111-2222-3333-444444444444",
            id=None,
            unique_name=None,
            display_name="PR Agent",
        )
    )
    missing_stable_identity = SimpleNamespace(
        author=SimpleNamespace(
            descriptor=None,
            id=None,
            unique_name=None,
            display_name="PR Agent",
        )
    )

    assert provider.supports_review_finding_state() is True
    assert provider.is_comment_authored_by_pr_agent(matching_comment) is True
    assert provider.is_comment_authored_by_pr_agent({
        "author": {"descriptor": descriptor, "displayName": "PR Agent"},
    }) is True
    assert provider.is_comment_authored_by_pr_agent(same_name_different_descriptor) is False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        provider.is_comment_authored_by_pr_agent(missing_stable_identity)


def test_azure_newest_comment_order_is_chronological_across_threads():
    from datetime import datetime, timezone

    def timestamp(second):
        return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)

    def comment(comment_id, body, second):
        return Comment(
            id=comment_id,
            content=body,
            published_date=timestamp(second),
            last_updated_date=timestamp(second),
        )

    old = comment(10, "old", 1)
    middle = comment(20, "middle", 2)
    tied_low = comment(30, "tied-low", 4)
    tied_high = comment(31, "tied-high", 4)
    newest = comment(5, "newest", 5)
    old_thread = CommentThread(id=100, comments=[old, middle])
    tied_thread = CommentThread(id=200, comments=[tied_low, tied_high])
    newest_thread = CommentThread(id=300, comments=[newest])

    raw_variants = [
        [old_thread, tied_thread, newest_thread],
        [newest_thread, tied_thread, old_thread],
        [tied_thread, old_thread, newest_thread],
    ]
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)

    for raw_threads in raw_variants:
        provider._get_threads = lambda raw_threads=raw_threads: raw_threads
        comments = provider.get_issue_comments_newest_first()
        assert [item.body for item in comments] == [
            "newest",
            "tied-high",
            "tied-low",
            "middle",
            "old",
        ]
        assert [item.id for item in comments] == [5, 31, 30, 20, 10]


def test_azure_raw_comment_order_uses_updates_and_thread_id_ties():
    def raw_comment(body, published, updated):
        return {
            "id": 1,
            "content": body,
            "publishedDate": published,
            "lastUpdatedDate": updated,
        }

    tied_time = "2026-01-01T00:00:04.000001Z"
    older_thread = {
        "id": 10,
        "comments": [raw_comment("tied-older-thread", tied_time, tied_time)],
    }
    newer_thread = {
        "id": 20,
        "comments": [raw_comment("tied-newer-thread", tied_time, tied_time)],
    }
    edited_thread = {
        "id": 5,
        "comments": [raw_comment(
            "edited-newest", "2026-01-01T00:00:01Z", "2026-01-01T00:00:05+00:00",
        )],
    }
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)

    for threads in (
        [older_thread, newer_thread, edited_thread],
        [edited_thread, newer_thread, older_thread],
        [newer_thread, edited_thread, older_thread],
    ):
        provider._get_threads = lambda threads=threads: threads
        comments = provider.get_issue_comments_newest_first()
        assert [comment["body"] for comment in comments] == [
            "edited-newest", "tied-newer-thread", "tied-older-thread",
        ]


class TestAzureDevopsGlobalSettings:
    @pytest.fixture(autouse=True)
    def _clear_global_settings_cache(self):
        # The org global-settings cache is process-level; clear it between tests.
        from pr_agent.git_providers import git_provider as _gp
        _gp._GLOBAL_SETTINGS_CACHE.clear()
        yield
        _gp._GLOBAL_SETTINGS_CACHE.clear()

    def _make_provider(self):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.workspace_slug = "my-project"
        provider.repo_slug = "my-repo"
        provider.azure_devops_client = MagicMock()
        return provider

    @staticmethod
    def _set_org_settings(ms, org):
        ms.return_value.config.use_global_settings_file = True
        ms.return_value.azure_devops.get.return_value = org

    def test_get_owning_namespace_returns_configured_org(self):
        provider = self._make_provider()
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as ms:
            self._set_org_settings(ms, "myorg")
            assert provider.get_owning_namespace() == "myorg"

    def test_get_owning_namespace_parses_org_from_collection_url(self):
        provider = self._make_provider()
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as ms:
            self._set_org_settings(ms, "https://dev.azure.com/myorg")
            assert provider.get_owning_namespace() == "myorg"

    def test_get_owning_namespace_none_when_org_unset(self):
        provider = self._make_provider()
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as ms:
            self._set_org_settings(ms, None)
            assert provider.get_owning_namespace() is None

    def test_fetch_global_repo_settings_reads_pr_agent_settings_from_pr_project(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_item_content.return_value = [b"[pr_reviewer]"]
        result = provider._fetch_global_repo_settings("myorg")

        assert result == b"[pr_reviewer]"
        provider.azure_devops_client.get_item_content.assert_called_once_with(
            repository_id="pr-agent-settings",
            project="my-project",
            download=False,
            include_content_metadata=False,
            include_content=True,
            path=".pr_agent.toml",
        )

    def test_fetch_global_repo_settings_404_returns_empty_and_propagates_other_errors(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_item_content.side_effect = Exception(
            "Operation returned a 404 status code."
        )
        assert provider._fetch_global_repo_settings("myorg") == ""

    def test_get_repo_settings_merges_global_then_local(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_item_content.side_effect = [
            [b"[pr_reviewer]\nextra_instructions = \"global\"\n"],
            [b"[pr_reviewer]\nextra_instructions = \"local\"\n"],
        ]
        with patch("pr_agent.git_providers.git_provider.get_settings") as ms, \
             patch("pr_agent.git_providers.azuredevops_provider.get_settings") as az:
            az.return_value.azure_devops.get.return_value = "myorg"
            ms.return_value.config.use_global_settings_file = True
            result = provider.get_repo_settings()

        assert result == [
            ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\n"),
            ("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n"),
        ]

    def test_get_repo_settings_only_local_when_global_disabled(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_item_content.side_effect = [
            [b"[pr_reviewer]\ntemperature = 0.2\n"],
        ]
        with patch("pr_agent.git_providers.git_provider.get_settings") as ms:
            ms.return_value.config.use_global_settings_file = False
            result = provider.get_repo_settings()

        assert result == [("local", b"[pr_reviewer]\ntemperature = 0.2\n")]

    def test_global_settings_result_is_cached(self):
        provider = self._make_provider()
        provider.azure_devops_client.get_item_content.return_value = [b"[pr_reviewer]\nnum_max_findings = 5\n"]
        with patch("pr_agent.git_providers.git_provider.get_settings") as ms, \
             patch("pr_agent.git_providers.azuredevops_provider.get_settings") as az:
            az.return_value.azure_devops.get.return_value = "myorg"
            ms.return_value.config.use_global_settings_file = True
            assert provider._get_global_repo_settings() == b"[pr_reviewer]\nnum_max_findings = 5\n"
            assert provider._get_global_repo_settings() == b"[pr_reviewer]\nnum_max_findings = 5\n"  # cached

        assert provider.azure_devops_client.get_item_content.call_count == 1


class TestAzureDevopsProviderSuggestionFence:
    """Regression tests for #2110: Azure DevOps has no committable suggestion blocks, so the
    ```suggestion fence /improve emits was published verbatim and rendered as a raw block.
    """

    @staticmethod
    def _committable_suggestion(relevant_file="/src/app.py"):
        suggestion = _suggestion(relevant_file)
        suggestion["body"] = "**Suggestion:** use a set [best practice]\n```suggestion\nvalues = set()\n```"
        suggestion["original_suggestion"] = {
            "existing_code": "values = []",
            "improved_code": "values = set()",
        }
        return suggestion

    def test_suggestion_fence_is_published_as_a_diff_block(self):
        provider = _provider_with_diff("/src/app.py")

        provider.publish_code_suggestions([self._committable_suggestion()])

        body = _created_threads(provider)[-1].comments[0].content
        assert "```suggestion" not in body
        assert "```diff" in body
        assert "-values = []" in body
        assert "+values = set()" in body
        assert body.startswith("**Suggestion:** use a set [best practice]")

    def test_body_is_left_alone_when_there_is_no_original_suggestion(self):
        provider = _provider_with_diff("/src/app.py")

        provider.publish_code_suggestions([_suggestion("/src/app.py")])

        assert _created_threads(provider)[-1].comments[0].content == "```suggestion\nfixed\n```"

    def test_pr_level_fallback_also_carries_the_diff_block(self):
        provider = _provider_with_diff("/src/app.py")
        provider.publish_comment = MagicMock()

        provider.publish_code_suggestions([self._committable_suggestion("/src/removed.py")])

        published = provider.publish_comment.call_args[0][0]
        assert "```diff" in published
        assert "```suggestion" not in published

    def test_unusable_original_suggestion_keeps_the_suggestion(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = self._committable_suggestion()
        del suggestion["original_suggestion"]["improved_code"]

        provider.publish_code_suggestions([suggestion])

        assert "```suggestion" in _created_threads(provider)[-1].comments[0].content

    def test_backslashes_in_proposed_code_are_preserved(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = self._committable_suggestion()
        suggestion["original_suggestion"] = {
            "existing_code": "print(1)",
            "improved_code": "print(\t\\1\\d)",
        }

        provider.publish_code_suggestions([suggestion])

        body = _created_threads(provider)[-1].comments[0].content
        assert "```diff" in body
        assert "+print(\t\\1\\d)" in body

    def test_invalid_range_still_falls_back_to_pr_comment(self):
        provider = _provider_with_diff("/src/app.py")
        provider.publish_comment = MagicMock()
        suggestion = self._committable_suggestion()
        suggestion["relevant_lines_start"] = 200
        suggestion["relevant_lines_end"] = 202

        provider.publish_code_suggestions([suggestion])

        published = provider.publish_comment.call_args[0][0]
        assert "```diff" in published
        assert "```suggestion" not in published

    def test_rendered_thread_still_feeds_the_next_run(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = self._committable_suggestion()
        suggestion["relevant_lines_start"] = suggestion["relevant_lines_end"] = 2
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            provider.publish_code_suggestions([suggestion])
            stored = SimpleNamespace(
                id=17,
                status="active",
                thread_context=SimpleNamespace(
                    file_path="/src/app.py",
                    right_file_start=SimpleNamespace(line=2),
                    right_file_end=SimpleNamespace(line=2),
                ),
                comments=[
                    _created_threads(provider)[0].comments[0],
                    SimpleNamespace(content="Not doing this.", author=SimpleNamespace(display_name="Alex")),
                ],
            )
            provider.azure_devops_client.get_threads.return_value = [stored]
            provider.pr = SimpleNamespace(last_merge_commit=SimpleNamespace(commit_id="head"))
            provider.azure_devops_client.get_item.return_value = SimpleNamespace(content="before\nvalues = set()\nafter")
            assert "Not doing this." in provider.get_code_suggestion_thread_context()
            assert provider.reconcile_code_suggestion_threads() == 1

    def test_rendered_thread_without_markers_is_not_reposted(self):
        """A thread published before persistent_inline_comments was enabled carries no
        dedup marker, but the fingerprint bootstrap must still recognize its rendered
        diff block so the suggestion is not re-posted once the feature is switched on.
        """
        suggestion = self._committable_suggestion()
        first_run = _provider_with_diff("/src/app.py")
        first_run.publish_code_suggestions([suggestion])
        stored = SimpleNamespace(
            thread_context=SimpleNamespace(
                file_path="/src/app.py",
                right_file_start=SimpleNamespace(line=10),
                right_file_end=SimpleNamespace(line=12),
            ),
            comments=[_created_threads(first_run)[0].comments[0]],
        )
        provider = _provider_with_diff("/src/app.py")
        provider.azure_devops_client.get_threads.return_value = [stored]
        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            provider.publish_code_suggestions([suggestion])
        provider.azure_devops_client.create_thread.assert_not_called()

    def test_no_op_suggestion_keeps_its_fence(self):
        provider = _provider_with_diff("/src/app.py")
        suggestion = self._committable_suggestion()
        suggestion["original_suggestion"]["improved_code"] = "values = []   "
        provider.publish_code_suggestions([suggestion])
        assert "```suggestion" in _created_threads(provider)[-1].comments[0].content
