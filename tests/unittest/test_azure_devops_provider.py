import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.inline_comment_dedup import code_fingerprint
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

    def test_get_repo_file_content_treats_failure_as_empty(self):
        provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
        provider.repo_slug = "my-repo"
        provider.workspace_slug = "my-project"
        provider.pr = MagicMock()
        provider.pr.last_merge_target_commit.commit_id = "base-sha"
        provider.azure_devops_client = MagicMock()
        provider.azure_devops_client.get_item.side_effect = Exception("not found")

        assert provider.get_repo_file_content("MISSING.md") == ""


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

        with patch("pr_agent.git_providers.azuredevops_provider.get_settings") as settings:
            settings.return_value.get.side_effect = lambda key, default=None: (
                True if key == "config.persistent_inline_comments" else default
            )
            settings.return_value.azure_devops.get.return_value = "active"
            result = provider.publish_code_suggestions([_suggestion("/src/removed.py")])

        assert result is True
        body = _created_threads(provider)[0].comments[0].content
        assert body.startswith("## PR Code Suggestions")
        assert "<!-- pr-agent-dedup:" in body
        assert "<!-- pr-agent-dedup-code:" in body

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
        provider.pr_commits = ["stale"]
        provider.previous_review = "stale"
        provider.unreviewed_files_map = {"stale.cs": "stale.cs"}
        provider.temp_comments = ["stale"]
        provider._parse_pr_url = MagicMock(return_value=("project", "repo", 2))
        provider._get_pr = MagicMock(return_value=MagicMock())

        provider.set_pr("https://dev.azure.com/example/project/_git/repo/pullrequest/2")

        assert provider.diff_files is None
        assert provider._diff_path_map is None
        assert provider.pr_commits is None
        assert provider.previous_review is None
        assert provider.unreviewed_files_map == {}
        assert provider.temp_comments == []

    def test_unmatched_suggestion_path_does_not_break_markdown(self):
        provider = _provider_with_diff("/src/Api/Controllers/SomeController.cs")

        provider.publish_code_suggestions([_suggestion("` /src/Api/Controllers/Removed.cs `")])

        body = _created_threads(provider)[-1].comments[0].content
        assert body.startswith("## PR Code Suggestions")
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


class TestAzureDevopsProviderSuggestionReconciliation:
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
