from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider


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
    def _provider():
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr_num = 1
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_pull_request_commits.return_value = [SimpleNamespace(commit_id="m1")]
        return provider

    def test_get_files_full_skips_commits_without_changes(self):
        provider = self._provider()
        provider.azure_devops_client.get_pull_request_commits.return_value = [
            SimpleNamespace(commit_id="m1"),
            SimpleNamespace(commit_id="m2"),
        ]
        provider.azure_devops_client.get_changes.side_effect = [
            SimpleNamespace(changes=None),
            SimpleNamespace(changes=[{"item": {"path": "/src/app.py"}}]),
        ]

        assert provider._get_files_full() == ["/src/app.py"]

    def test_get_files_full_skips_changes_without_paths(self):
        provider = self._provider()
        provider.azure_devops_client.get_changes.return_value = SimpleNamespace(changes=[
            {},
            {"item": None},
            {"item": {"path": ""}},
            {"item": {"path": "/src/app.py"}},
        ])

        assert provider._get_files_full() == ["/src/app.py"]

    def test_get_files_full_supports_sdk_change_objects(self):
        provider = self._provider()
        provider.azure_devops_client.get_changes.return_value = SimpleNamespace(changes=[
            SimpleNamespace(item=SimpleNamespace(path="/src/sdk.py")),
        ])

        assert provider._get_files_full() == ["/src/sdk.py"]

    def test_get_files_full_skips_tree_entries(self):
        provider = self._provider()
        provider.azure_devops_client.get_changes.return_value = SimpleNamespace(changes=[
            {"item": {"path": "/src", "gitObjectType": "tree"}},
            {"item": {"path": "/src/app.py", "gitObjectType": "blob"}},
        ])

        assert provider._get_files_full() == ["/src/app.py"]


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

    def test_set_pr_invalidates_the_diff_path_index(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")
        provider._diff_path_map = {"stale.cs": "/stale.cs"}
        provider._parse_pr_url = MagicMock(return_value=("project", "repo", 2))
        provider._get_pr = MagicMock(return_value=MagicMock())

        provider.set_pr("https://dev.azure.com/example/project/_git/repo/pullrequest/2")

        assert provider.diff_files is None
        assert provider._diff_path_map is None

    def test_unmatched_suggestion_path_does_not_break_markdown(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("` /src/Api/Controllers/Removed.cs `")])

        body = _created_threads(provider)[-1].comments[0].content
        assert body.startswith("`/src/Api/Controllers/Removed.cs` (lines 10-12)")

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

    def test_get_inline_comment_bodies_only_returns_line_threads(self):
        line_thread = SimpleNamespace(
            thread_context=SimpleNamespace(file_path="/app.py", right_file_start=SimpleNamespace(line=3)),
            comments=[SimpleNamespace(content="line finding")],
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

        assert provider.get_inline_comment_bodies() == ["line finding"]
        provider.azure_devops_client.get_threads.assert_called_once_with(
            repository_id="my-repo",
            pull_request_id=42,
            project="my-project",
        )

    def test_get_inline_comment_bodies_supports_serialized_context(self):
        thread = SimpleNamespace(
            thread_context={"filePath": "/app.py", "rightFileStart": {"line": 3, "offset": 1}},
            comments=[SimpleNamespace(content="line finding"), SimpleNamespace(content="")],
        )

        assert self._provider([thread]).get_inline_comment_bodies() == ["line finding"]

    def test_get_inline_comment_bodies_includes_recent_successful_posts(self):
        provider = self._provider([])
        provider.publish_code_suggestions([{
            "body": "line finding",
            "relevant_file": "/app.py",
            "relevant_lines_start": 3,
            "relevant_lines_end": 3,
        }])

        assert provider.get_inline_comment_bodies() == ["line finding"]

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
