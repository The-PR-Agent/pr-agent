import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.tools.pr_code_suggestions as pr_code_suggestions_module
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import (PRCodeSuggestionsHeader,
                                 PRCodeSuggestionsIdentity)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import (restore_settings,
                                              snapshot_settings)


def _make_tool(git_provider=None):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = git_provider or MagicMock()
    tool.progress_response = None
    return tool


def _valid_suggestion(**overrides):
    suggestion = {
        "one_sentence_summary": "Avoid duplicated work",
        "label": "maintainability",
        "relevant_file": "app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "suggestion_content": "Use the shared helper.",
        "existing_code": "old()",
        "improved_code": "new()",
    }
    suggestion.update(overrides)
    return suggestion


def test_prepare_pr_code_suggestions_filters_duplicates_and_missing_required_fields():
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Avoid duplicated work
    label: maintainability
    relevant_file: app.py
    suggestion_content: Use the shared helper.
    existing_code: old()
    improved_code: new()
  - one_sentence_summary: Avoid duplicated work
    label: maintainability
    relevant_file: app.py
    suggestion_content: Duplicate summary.
    existing_code: old()
    improved_code: newer()
  - one_sentence_summary: Missing label
    relevant_file: app.py
    suggestion_content: Missing label should be skipped.
    existing_code: old()
    improved_code: new()
"""

    data = tool._prepare_pr_code_suggestions(prediction)

    assert len(data["code_suggestions"]) == 1
    assert data["code_suggestions"][0]["one_sentence_summary"] == "Avoid duplicated work"
    assert data["code_suggestions"][0]["improved_code"] == "new()"


def test_prepare_pr_code_suggestions_renames_critical_label_when_focusing_only_on_problems():
    settings = get_settings()
    original_focus = settings.get("pr_code_suggestions.focus_only_on_problems", False)
    settings.set("pr_code_suggestions.focus_only_on_problems", True)
    tool = _make_tool()
    prediction = """
code_suggestions:
  - one_sentence_summary: Fix unsafe behavior
    label: critical issue
    relevant_file: app.py
    suggestion_content: Guard this path.
    existing_code: old()
    improved_code: new()
"""

    try:
        data = tool._prepare_pr_code_suggestions(prediction)

        assert data["code_suggestions"][0]["label"] == "possible issue"
    finally:
        settings.set("pr_code_suggestions.focus_only_on_problems", original_focus)


@pytest.mark.asyncio
async def test_analyze_self_reflection_response_merges_scores_and_zeroes_invalid_ranges():
    git_provider = MagicMock()
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    settings.config.publish_output = False
    suggestion = _valid_suggestion()
    suggestion.pop("relevant_lines_start")
    suggestion.pop("relevant_lines_end")
    data = {"code_suggestions": [suggestion]}
    response_reflect = """
code_suggestions:
  - suggestion_score: 9
    why: Great suggestion, but line range is missing.
    relevant_lines_start: -1
    relevant_lines_end: -1
"""

    try:
        await tool.analyze_self_reflection_response(data, response_reflect)

        assert data["code_suggestions"][0]["score"] == 0
        assert data["code_suggestions"][0]["score_why"] == "Great suggestion, but line range is missing."
        assert data["code_suggestions"][0]["relevant_lines_start"] == -1
        assert data["code_suggestions"][0]["relevant_lines_end"] == -1
    finally:
        settings.config.publish_output = original_publish_output


@pytest.mark.asyncio
async def test_prepare_prediction_main_keeps_successful_chunks_when_one_parallel_chunk_fails():
    settings = get_settings()
    original_decouple_hunks = settings.pr_code_suggestions.decouple_hunks
    original_parallel_calls = settings.pr_code_suggestions.parallel_calls
    settings.pr_code_suggestions.decouple_hunks = True
    settings.pr_code_suggestions.parallel_calls = True
    tool = _make_tool()
    tool.token_handler = MagicMock()
    calls = []
    successful_chunk_started = asyncio.Event()
    release_successful_chunk = asyncio.Event()
    successful_chunk_finished = asyncio.Event()

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        calls.append(patches_diff)
        if patches_diff == "chunk-b":
            await successful_chunk_started.wait()
            release_successful_chunk.set()
            raise RuntimeError("chunk b failed")
        successful_chunk_started.set()
        await release_successful_chunk.wait()
        successful_chunk_finished.set()
        return {"code_suggestions": [_valid_suggestion(relevant_file="chunk-a.py")]}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", return_value=["chunk-a", "chunk-b"]):
            tool._get_prediction = fake_get_prediction

            data = await tool.prepare_prediction_main("primary-model")
    finally:
        settings.pr_code_suggestions.decouple_hunks = original_decouple_hunks
        settings.pr_code_suggestions.parallel_calls = original_parallel_calls

    assert calls == ["chunk-a", "chunk-b"]
    assert successful_chunk_finished.is_set()
    assert tool.failed_chunk_count == 1
    assert tool.total_chunk_count == 2
    assert data["code_suggestions"] == [_valid_suggestion(relevant_file="chunk-a.py")]


@pytest.mark.asyncio
async def test_prepare_prediction_main_propagates_chunk_cancellation_after_waiting_for_siblings():
    settings = get_settings()
    original_decouple_hunks = settings.pr_code_suggestions.decouple_hunks
    original_parallel_calls = settings.pr_code_suggestions.parallel_calls
    settings.pr_code_suggestions.decouple_hunks = True
    settings.pr_code_suggestions.parallel_calls = True
    tool = _make_tool()
    tool.token_handler = MagicMock()
    successful_chunk_finished = asyncio.Event()

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        if patches_diff == "chunk-b":
            raise asyncio.CancelledError
        await asyncio.sleep(0.01)
        successful_chunk_finished.set()
        return {"code_suggestions": []}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", return_value=["chunk-a", "chunk-b"]):
            tool._get_prediction = fake_get_prediction

            with pytest.raises(asyncio.CancelledError):
                await tool.prepare_prediction_main("primary-model")
    finally:
        settings.pr_code_suggestions.decouple_hunks = original_decouple_hunks
        settings.pr_code_suggestions.parallel_calls = original_parallel_calls

    assert successful_chunk_finished.is_set()


@pytest.mark.asyncio
async def test_prepare_prediction_main_keeps_processing_after_one_sequential_chunk_fails():
    settings = get_settings()
    original_decouple_hunks = settings.pr_code_suggestions.decouple_hunks
    original_parallel_calls = settings.pr_code_suggestions.parallel_calls
    settings.pr_code_suggestions.decouple_hunks = True
    settings.pr_code_suggestions.parallel_calls = False
    tool = _make_tool()
    tool.token_handler = MagicMock()
    calls = []

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        calls.append(patches_diff)
        if patches_diff == "chunk-b":
            raise RuntimeError("chunk b failed")
        return {"code_suggestions": [_valid_suggestion(relevant_file=f"{patches_diff}.py")]}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", return_value=[
            "chunk-a", "chunk-b", "chunk-c"
        ]):
            tool._get_prediction = fake_get_prediction

            data = await tool.prepare_prediction_main("primary-model")
    finally:
        settings.pr_code_suggestions.decouple_hunks = original_decouple_hunks
        settings.pr_code_suggestions.parallel_calls = original_parallel_calls

    assert calls == ["chunk-a", "chunk-b", "chunk-c"]
    assert tool.failed_chunk_count == 1
    assert tool.total_chunk_count == 3
    assert data["code_suggestions"] == [
        _valid_suggestion(relevant_file="chunk-a.py"),
        _valid_suggestion(relevant_file="chunk-c.py"),
    ]


@pytest.mark.asyncio
async def test_prepare_prediction_main_keeps_outer_fallback_when_all_chunks_fail():
    settings_snapshot = snapshot_settings(
        ("config.model", "config.fallback_models", "openai.deployment_id", "openai.fallback_deployments")
    )
    settings = get_settings()
    settings.set("config.model", "primary-model")
    settings.set("config.fallback_models", ["fallback-model"])
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    original_decouple_hunks = settings.pr_code_suggestions.decouple_hunks
    original_parallel_calls = settings.pr_code_suggestions.parallel_calls
    settings.pr_code_suggestions.decouple_hunks = True
    settings.pr_code_suggestions.parallel_calls = True
    tool = _make_tool()
    tool.token_handler = MagicMock()
    attempted = []

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        attempted.append((model, patches_diff))
        if model == "primary-model":
            raise RuntimeError(f"{patches_diff} failed")
        return {"code_suggestions": [_valid_suggestion(relevant_file=f"{patches_diff}.py")]}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", return_value=["chunk-a", "chunk-b"]):
            tool._get_prediction = fake_get_prediction

            data = await retry_with_fallback_models(tool.prepare_prediction_main)
    finally:
        settings.pr_code_suggestions.decouple_hunks = original_decouple_hunks
        settings.pr_code_suggestions.parallel_calls = original_parallel_calls
        restore_settings(settings_snapshot)

    assert attempted == [
        ("primary-model", "chunk-a"),
        ("primary-model", "chunk-b"),
        ("fallback-model", "chunk-a"),
        ("fallback-model", "chunk-b"),
    ]
    assert len(data["code_suggestions"]) == 2
    assert tool.failed_chunk_count == 0
    assert tool.total_chunk_count == 2


def test_suggestions_coverage_footer_reports_partial_runs_and_respects_flag():
    settings = get_settings()
    snapshot = snapshot_settings(["pr_code_suggestions.enable_suggestions_coverage_footer"])
    tool = _make_tool()
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 3

    try:
        settings.set("pr_code_suggestions.enable_suggestions_coverage_footer", True)
        footer = tool._get_suggestions_coverage_footer()
        assert "1 of 3 analysis chunks failed" in footer
        assert "successful chunks only" in footer

        empty_footer = tool._get_suggestions_coverage_footer(suggestions_present=False)
        assert "no suggestions were found in the successful chunks" in empty_footer
        assert "failed chunks could not be analyzed" in empty_footer

        settings.set("pr_code_suggestions.enable_suggestions_coverage_footer", False)
        assert tool._get_suggestions_coverage_footer() == ""
    finally:
        restore_settings(snapshot)


def test_suggestions_coverage_footer_is_safe_for_tools_built_without_init():
    tool = _make_tool()

    assert tool._get_suggestions_coverage_footer() == ""


@pytest.mark.asyncio
async def test_run_appends_partial_suggestions_coverage_to_the_summary():
    snapshot = snapshot_settings([
        "config.publish_output",
        "data",
        "pr_code_suggestions.enable_suggestions_coverage_footer",
    ])
    tool = _make_tool()
    tool.pr_url = "https://example.test/pull/1"
    tool.git_provider.get_files.return_value = ["app.py"]
    tool.generate_summarized_suggestions = MagicMock(return_value="Base suggestions body")
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2

    try:
        get_settings().set("config.publish_output", False)
        get_settings().set("pr_code_suggestions.enable_suggestions_coverage_footer", True)
        with (patch("pr_agent.tools.pr_code_suggestions.init_run_details"),
              patch("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                    AsyncMock(return_value={"code_suggestions": [_valid_suggestion()]}))):
            await tool.run()

        artifact = get_settings().data["artifact"]
        assert artifact.startswith("Base suggestions body")
        assert "1 of 2 analysis chunks failed" in artifact
    finally:
        restore_settings(snapshot)


def test_dedent_code_matches_target_file_indentation():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        )
    ]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 2, "return new()") == "    return new()"


def test_dedent_code_preserves_relative_indentation_across_anchor_and_snippet_styles():
    anchors = [
        ("", "spaces", 0),
        ("  ", "spaces", 2),
        ("    ", "spaces", 4),
        ("\t", "tabs", 1),
        ("\t\t", "tabs", 2),
    ]
    indentation_units = [1, 2, 4]
    initial_depths = [0, 1, 2]
    relative_depth_patterns = [(0, 1, 0), (0, 1, 2)]
    checked_cases = 0

    for anchor, anchor_style, anchor_depth in anchors:
        for indentation_unit in indentation_units:
            for initial_depth in initial_depths:
                for relative_depths in relative_depth_patterns:
                    snippet = "\n".join(
                        " " * ((initial_depth + relative_depth) * indentation_unit) + f"line_{index}"
                        for index, relative_depth in enumerate(relative_depths)
                    )
                    if anchor_style == "tabs":
                        expected = "\n".join(
                            "\t" * (anchor_depth + relative_depth) + f"line_{index}"
                            for index, relative_depth in enumerate(relative_depths)
                        )
                    else:
                        expected = "\n".join(
                            " " * (anchor_depth + relative_depth * indentation_unit) + f"line_{index}"
                            for index, relative_depth in enumerate(relative_depths)
                        )

                    git_provider = MagicMock()
                    git_provider.diff_files = [FilePatchInfo(
                        base_file="",
                        head_file=f"{anchor}anchor\n",
                        patch="",
                        filename="app.py",
                    )]
                    tool = _make_tool(git_provider)

                    actual = tool.dedent_code("app.py", 1, snippet)

                    assert actual == expected, (
                        anchor, indentation_unit, initial_depth, relative_depths, actual, expected
                    )
                    checked_cases += 1

    assert checked_cases == 90


def test_dedent_code_preserves_existing_tab_indentation_levels():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\t\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, "\touter\n\t\tinner\n\touter") == (
        "\t\touter\n\t\t\tinner\n\t\touter"
    )


@pytest.mark.parametrize("snippet", [
    " outer\n   inner\n outer",
    "   outer\n     inner\n   outer",
    "\t outer\n\t   inner\n\t outer",
])
def test_dedent_code_infers_tab_depth_from_relative_widths(snippet):
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, snippet) == "\touter\n\t\tinner\n\touter"


def test_dedent_code_preserves_alignment_spaces_after_the_tab_anchor():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\t    anchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, "outer\n    inner\nouter") == (
        "\t    outer\n\t\t    inner\n\t    outer"
    )


@pytest.mark.parametrize("alignment_spaces", [1, 2])
def test_dedent_code_preserves_continuation_alignment_spaces(alignment_spaces):
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "outer\n    inner\n        deeper\n" + " " * (8 + alignment_spaces) + "aligned"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\touter\n\t\tinner\n\t\t\tdeeper\n\t\t\t" + " " * alignment_spaces + "aligned"
    )


def test_dedent_code_preserves_alignment_without_adjacent_depths():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "outer\n    inner\n            deeper\n              aligned"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\touter\n\t\tinner\n\t\t\t\tdeeper\n\t\t\t\t  aligned"
    )


def test_dedent_code_excludes_closed_continuations_from_unit_inference():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "outer(\n  arg\n)\nif cond:\n    inner\n        deeper"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\touter(\n\t  arg\n\t)\n\tif cond:\n\t\tinner\n\t\t\tdeeper"
    )


def test_dedent_code_infers_structure_inside_a_closed_continuation():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "outer(\n  function() {\n      if (cond) {\n          work()\n      }\n  }\n)"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\touter(\n\t  function() {\n\t\t  if (cond) {\n"
        "\t\t\t  work()\n\t\t  }\n\t  }\n\t)"
    )


def test_dedent_code_preserves_spaces_in_a_pure_continuation():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "call(\n    arg\n)"
    assert tool.dedent_code("app.py", 1, snippet) == "\tcall(\n\t    arg\n\t)"


def test_dedent_code_keeps_two_space_structural_indentation():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "if outer:\n  if inner:\n    work()"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\tif outer:\n\t\tif inner:\n\t\t\twork()"
    )


def test_dedent_code_infers_structure_across_outdents_and_continuations():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\t\t\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    snippet = "        deep()\n    call(\n      arg\n    )\nouter()"
    assert tool.dedent_code("app.py", 1, snippet) == (
        "\t\t\tdeep()\n\t\tcall(\n\t\t  arg\n\t\t)\n\touter()"
    )


def test_dedent_code_removes_whitespace_from_blank_lines_when_shifting_left():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="    anchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, "        outer\n          \n            inner") == (
        "    outer\n\n        inner"
    )


def test_dedent_code_ignores_blank_line_width_when_inferring_tab_depth():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, " outer\n       \n   inner\n outer") == (
        "\touter\n\n\t\tinner\n\touter"
    )


def test_dedent_code_ignores_leading_blank_line_when_inferring_tab_depth():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="\tanchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, "\n  return new()") == "\n\treturn new()"


def test_dedent_code_ignores_leading_blank_line_when_shifting_spaces():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="    anchor\n",
        patch="",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 1, "\n        return new()") == "\n    return new()"


def test_dedent_code_uses_patch_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1,2 +1,2 @@\n def f():\n-    return older()\n+    return old()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 2, "return new()") == "    return new()"


def test_dedent_code_uses_patch_when_head_file_is_partial():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="def f():\n    return older()\n",
        head_file="def f():\n    return old()\n",
        patch="@@ -19,2 +19,2 @@\n def f():\n-\told()\n+\told()\n",
        filename="app.py",
        head_file_is_complete=False,
    )]
    tool = _make_tool(git_provider)

    assert tool.dedent_code("app.py", 20, "new()") == "\tnew()"


@pytest.mark.asyncio
async def test_push_inline_code_suggestions_falls_back_to_individual_publish_calls():
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="def f():\n    return old()\n",
            patch="",
            filename="app.py",
        ),
        FilePatchInfo(
            base_file="",
            head_file="def work():\n    return old_worker()\n",
            patch="",
            filename="worker.py",
        ),
    ]
    git_provider.publish_code_suggestions.side_effect = [False, True, True]
    tool = _make_tool(git_provider)
    data = {"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code="return new()",
            score=8,
        ),
        _valid_suggestion(
            relevant_file="worker.py",
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old_worker()",
            improved_code="return new_worker()",
            suggestion_content="Keep the worker result fresh.",
        ),
    ]}

    await tool.push_inline_code_suggestions(data)

    assert git_provider.publish_code_suggestions.call_count == 3
    batch_call = git_provider.publish_code_suggestions.call_args_list[0].args[0]
    first_retry = git_provider.publish_code_suggestions.call_args_list[1].args[0]
    second_retry = git_provider.publish_code_suggestions.call_args_list[2].args[0]
    assert len(batch_call) == 2
    assert first_retry == [batch_call[0]]
    assert second_retry == [batch_call[1]]
    assert first_retry[0]["relevant_file"] == "app.py"
    assert first_retry[0]["relevant_lines_start"] == 2
    assert first_retry[0]["relevant_lines_end"] == 2
    assert "```suggestion\n    return new()" in first_retry[0]["body"]
    assert second_retry[0]["relevant_file"] == "worker.py"
    assert second_retry[0]["relevant_lines_start"] == 2
    assert second_retry[0]["relevant_lines_end"] == 2
    assert "```suggestion\n    return new_worker()" in second_retry[0]["body"]


@pytest.fixture
def publish_output_no_suggestions():
    settings = get_settings()
    original = settings.get("pr_code_suggestions.publish_output_no_suggestions", True)

    def _set(value):
        settings.set("pr_code_suggestions.publish_output_no_suggestions", value)

    yield _set
    _set(original)


@pytest.mark.asyncio
async def test_publish_no_suggestions_removes_the_progress_comment_when_quiet(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.progress_response = MagicMock()

    await tool.publish_no_suggestions()

    git_provider.remove_comment.assert_called_once_with(tool.progress_response)
    git_provider.edit_comment.assert_not_called()
    git_provider.publish_comment.assert_not_called()


def _provider_with_file(head_file, filename="app.py"):
    git_provider = MagicMock()
    git_provider.diff_files = [
        FilePatchInfo(base_file="", head_file=head_file, patch="", filename=filename)
    ]
    git_provider.publish_code_suggestions.return_value = True
    return git_provider


def _published_suggestion(git_provider):
    published = git_provider.publish_code_suggestions.call_args_list[0].args[0]
    assert len(published) == 1
    return published[0]


def test_summarized_suggestions_use_the_target_file_indentation():
    git_provider = _provider_with_file("func f() {\n\told()\n}\n", filename="main.go")
    git_provider.get_line_link.return_value = "https://example.com/main.go#L2"
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        relevant_file="main.go",
        relevant_lines_start=2,
        relevant_lines_end=2,
        existing_code="old()",
        improved_code="replacement() {\n    nested()\n}",
        score=8,
    )

    summary = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})

    assert "+\treplacement() {" in summary
    assert "+\t\tnested()" in summary
    assert "+\t}" in summary


def test_summarized_suggestions_use_patch_anchor_for_partial_head_file():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="func f() {\n\tolder()\n",
        head_file="func f() {\n\told()\n",
        patch="@@ -19,2 +19,2 @@\n func f() {\n-\tolder()\n+\told()\n",
        filename="main.go",
        head_file_is_complete=False,
    )]
    git_provider.get_line_link.return_value = "https://example.com/main.go#L20"
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        relevant_file="main.go",
        relevant_lines_start=20,
        relevant_lines_end=20,
        existing_code="old()",
        improved_code="replacement() {\n    nested()\n}",
        score=8,
    )

    summary = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})

    assert "+\treplacement() {" in summary
    assert "+\t\tnested()" in summary
    assert "+\t}" in summary


def test_summarized_suggestions_normalize_both_sides_of_the_diff():
    git_provider = _provider_with_file(
        "func f() {\n\tif old() {\n\t\tkeep()\n\t}\n}\n",
        filename="main.go",
    )
    git_provider.get_line_link.return_value = "https://example.com/main.go#L2-L4"
    tool = _make_tool(git_provider)
    suggestion = _valid_suggestion(
        relevant_file="main.go",
        relevant_lines_start=2,
        relevant_lines_end=4,
        existing_code="if old() {\n    keep()\n}",
        improved_code="if new() {\n    keep()\n}",
        score=8,
    )

    summary = tool.generate_summarized_suggestions({"code_suggestions": [suggestion]})

    diff_block = summary.split("```diff\n", 1)[1].split("\n```", 1)[0]
    assert diff_block == "-\tif old() {\n+\tif new() {\n \t\tkeep()\n \t}"


@pytest.mark.asyncio
async def test_suggestion_covering_the_anchored_range_is_published_as_committable():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code="return new()",
            score=8,
        )
    ]})

    assert "```suggestion\n    return new()\n```" in _published_suggestion(git_provider)["body"]


@pytest.mark.asyncio
async def test_suggestion_rewriting_more_lines_than_it_replaces_is_published_as_a_plain_comment():
    git_provider = _provider_with_file("def f():\n    return old(\n        arg)\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old(\n    arg)",
            improved_code="return new(arg)",
            score=8,
        )
    ]})

    body = _published_suggestion(git_provider)["body"]
    assert "```suggestion" not in body
    assert "return new(arg)" in body


@pytest.mark.asyncio
async def test_suggestion_anchored_outside_the_file_is_published_as_a_plain_comment():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=40, relevant_lines_end=41, score=8)
    ]})

    git_provider.publish_code_suggestions.assert_not_called()
    body = git_provider.publish_comment.call_args.args[0]
    assert "```suggestion" not in body
    assert "`app.py:40-41`" in body
    assert "because the anchored range is outside the file" in body


@pytest.mark.asyncio
async def test_suggestion_with_reversed_range_is_published_as_a_pr_comment():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=2, relevant_lines_end=1, score=8)
    ]})

    git_provider.publish_code_suggestions.assert_not_called()
    body = git_provider.publish_comment.call_args.args[0]
    assert "`app.py:2-1`" in body
    assert "because the anchored range is outside the file" in body


@pytest.mark.asyncio
async def test_provider_diff_failure_is_not_treated_as_a_malformed_suggestion():
    git_provider = MagicMock()
    git_provider.diff_files = None
    git_provider.get_diff_files.side_effect = RuntimeError("provider failed")
    tool = _make_tool(git_provider)

    with pytest.raises(RuntimeError, match="provider failed"):
        await tool.push_inline_code_suggestions({"code_suggestions": [_valid_suggestion(score=8)]})

    git_provider.publish_code_suggestions.assert_not_called()
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_tracks_non_gfm_progress_comment_when_quiet(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    settings = get_settings()
    original_publish_output = settings.config.publish_output
    original_publish_output_progress = settings.config.publish_output_progress
    original_is_auto_command = settings.config.get("is_auto_command", False)
    settings.config.publish_output = True
    settings.config.publish_output_progress = True
    settings.config.is_auto_command = False
    git_provider = MagicMock()
    git_provider.get_files.return_value = ["app.py"]
    git_provider.is_supported.return_value = False
    progress_comment = MagicMock()
    git_provider.publish_comment.return_value = progress_comment
    tool = _make_tool(git_provider)
    tool.pr_url = "https://example.test/pull/1"
    tool.progress = "Preparing suggestions..."
    tool.prepare_prediction_main = AsyncMock()

    try:
        with (patch("pr_agent.tools.pr_code_suggestions.init_run_details"),
              patch("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                    AsyncMock(return_value={"code_suggestions": []}))):
            await tool.run()
    finally:
        settings.config.publish_output = original_publish_output
        settings.config.publish_output_progress = original_publish_output_progress
        settings.config.is_auto_command = original_is_auto_command

    git_provider.publish_comment.assert_called_once_with("Preparing suggestions...", is_temporary=True)
    git_provider.remove_comment.assert_called_once_with(progress_comment)


@pytest.mark.asyncio
async def test_publish_no_suggestions_does_not_remove_unrelated_temporary_comments(publish_output_no_suggestions):
    publish_output_no_suggestions(False)
    git_provider = MagicMock()
    tool = _make_tool(git_provider)

    await tool.publish_no_suggestions()

    git_provider.remove_initial_comment.assert_not_called()
    git_provider.remove_comment.assert_not_called()
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_publish_no_suggestions_still_overwrites_the_progress_comment_when_publishing(
        publish_output_no_suggestions):
    publish_output_no_suggestions(True)
    git_provider = MagicMock()
    git_provider.supports_code_suggestions_artifact.return_value = False
    tool = _make_tool(git_provider)
    tool.progress_response = MagicMock()

    await tool.publish_no_suggestions()

    _, kwargs = git_provider.edit_comment.call_args
    assert "No code suggestions found for the PR." in kwargs["body"]
    git_provider.remove_comment.assert_not_called()


@pytest.mark.asyncio
async def test_publish_no_suggestions_qualifies_partial_results(publish_output_no_suggestions):
    publish_output_no_suggestions(True)
    snapshot = snapshot_settings(["pr_code_suggestions.enable_suggestions_coverage_footer"])
    git_provider = MagicMock()
    git_provider.supports_code_suggestions_artifact.return_value = False
    tool = _make_tool(git_provider)
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2

    try:
        get_settings().set("pr_code_suggestions.enable_suggestions_coverage_footer", True)
        await tool.publish_no_suggestions()
    finally:
        restore_settings(snapshot)

    body = git_provider.publish_comment.call_args.args[0]
    assert "No code suggestions found in the successfully analyzed chunks." in body
    assert "1 of 2 analysis chunks failed" in body
    assert "failed chunks could not be analyzed" in body


@pytest.mark.asyncio
async def test_publish_no_suggestions_uses_provider_artifact_capability(publish_output_no_suggestions):
    publish_output_no_suggestions(True)
    git_provider = MagicMock()
    git_provider.supports_code_suggestions_artifact.return_value = True
    tool = _make_tool(git_provider)

    await tool.publish_no_suggestions()

    git_provider.publish_code_suggestions_artifact.assert_called_once_with(
        [], artifact_footer="", no_suggestions_message="No code suggestions found for the PR.")
    git_provider.publish_code_suggestions.assert_not_called()
    git_provider.publish_comment.assert_not_called()
    git_provider.edit_comment.assert_not_called()


@pytest.mark.asyncio
async def test_publish_no_suggestions_keeps_partial_notice_in_disabled_output_artifact(
        publish_output_no_suggestions):
    publish_output_no_suggestions(True)
    snapshot = snapshot_settings([
        "config.publish_output",
        "data",
        "pr_code_suggestions.enable_suggestions_coverage_footer",
    ])
    tool = _make_tool()
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2

    try:
        get_settings().set("config.publish_output", False)
        get_settings().set("pr_code_suggestions.enable_suggestions_coverage_footer", True)
        await tool.publish_no_suggestions()
        artifact = get_settings().data["artifact"]
    finally:
        restore_settings(snapshot)

    assert "No code suggestions found in the successfully analyzed chunks." in artifact
    assert "1 of 2 analysis chunks failed" in artifact


def test_setup_incremental_scope_calls_provider_when_supported():
    git_provider = MagicMock()
    git_provider.supports_incremental_kind.return_value = True
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(True)

    tool._setup_incremental_scope()

    git_provider.supports_incremental_kind.assert_called_once_with("suggestions")
    git_provider.get_incremental_commits.assert_called_once_with(tool.incremental, kind="suggestions")
    assert tool.incremental.is_incremental is True


def test_setup_incremental_scope_falls_back_when_unsupported():
    git_provider = MagicMock()
    git_provider.supports_incremental_kind.return_value = False
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(True)

    tool._setup_incremental_scope()

    git_provider.get_incremental_commits.assert_not_called()
    assert tool.incremental.is_incremental is False


def test_setup_incremental_scope_noop_without_incremental_flag():
    git_provider = MagicMock()
    tool = _make_tool(git_provider)
    tool.incremental = IncrementalPR(False)

    tool._setup_incremental_scope()

    git_provider.supports_incremental_kind.assert_not_called()
    git_provider.get_incremental_commits.assert_not_called()


def test_supports_incremental_kind_defaults_to_false_on_base_provider():
    # The base-class default must be "no support" so tools fall back to a full run
    # on providers that never implemented kind-aware incremental anchoring.
    assert GitProvider.supports_incremental_kind(MagicMock(), "suggestions") is False


def test_supports_code_suggestions_artifact_defaults_to_false_on_base_provider():
    assert GitProvider.supports_code_suggestions_artifact(MagicMock()) is False


@pytest.mark.asyncio
async def test_malformed_suggestion_does_not_stop_later_suggestions():
    git_provider = _provider_with_file("old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_file=123, score=8),
        _valid_suggestion(score=8),
    ]})

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert published[0]["relevant_file"] == "app.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_code", [123, ["old()"], {"code": "old()"}])
@pytest.mark.parametrize("improved_code", ["new()", ""])
async def test_non_string_existing_code_does_not_stop_later_suggestions(existing_code, improved_code):
    git_provider = _provider_with_file("old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(existing_code=existing_code, improved_code=improved_code, score=8),
        _valid_suggestion(score=8),
    ]})

    published = git_provider.publish_code_suggestions.call_args.args[0]
    assert len(published) == 1
    assert published[0]["original_suggestion"]["existing_code"] == "old()"


@pytest.mark.asyncio
async def test_advice_only_suggestion_is_published_instead_of_being_dropped():
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(relevant_lines_start=2, relevant_lines_end=2, improved_code="", score=8)
    ]})

    body = _published_suggestion(git_provider)["body"]
    assert "```suggestion" not in body
    assert "Use the shared helper." in body


@pytest.mark.asyncio
async def test_advice_only_suggestion_with_unverified_anchor_is_published_as_a_pr_comment():
    git_provider = MagicMock()
    git_provider.diff_files = None
    git_provider.get_diff_files.return_value = []
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(improved_code="", score=8)
    ]})

    git_provider.get_diff_files.assert_called_once_with()
    git_provider.publish_code_suggestions.assert_not_called()
    assert "Use the shared helper." in git_provider.publish_comment.call_args.args[0]


@pytest.mark.asyncio
async def test_dual_publishing_keeps_suggestions_without_replacement_code():
    settings = get_settings()
    original_threshold = settings.get("pr_code_suggestions.dual_publishing_score_threshold")
    settings.set("pr_code_suggestions.dual_publishing_score_threshold", 5)
    git_provider = _provider_with_file("def f():\n    return old()\n")
    tool = _make_tool(git_provider)

    try:
        await tool.dual_publishing({"code_suggestions": [
            _valid_suggestion(relevant_lines_start=2, relevant_lines_end=2, improved_code="", score=8)
        ]})

        assert "Use the shared helper." in _published_suggestion(git_provider)["body"]
        git_provider.publish_comment.assert_not_called()
    finally:
        settings.set("pr_code_suggestions.dual_publishing_score_threshold", original_threshold)


def test_is_applicable_suggestion_rejects_a_range_in_an_empty_file():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(base_file="", head_file="", patch="", filename="app.py")]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 1, "old()") is False


def test_is_applicable_suggestion_rejects_when_existing_code_does_not_cover_the_anchor():
    git_provider = _provider_with_file("def f():\n    first()\n    second()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 3, "first()") is False


def test_is_applicable_suggestion_rejects_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(base_file="", head_file=None, patch="", filename="app.py")]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 1, "old()") is False
    assert tool._suggestion_applyability("app.py", 1, 1, "old()")[1] == "the file content is unavailable"


def test_is_applicable_suggestion_uses_patch_when_file_content_is_unavailable():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1,2 +1,2 @@\n def f():\n-    return older()\n+    return old()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 2, "return old()") is True


def test_is_applicable_suggestion_rejects_a_range_missing_from_the_patch():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="",
        patch="@@ -1 +1 @@\n-old()\n+new()\n",
        filename="app.py",
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 2, 2, "other()") is False


def test_get_diff_file_does_not_refetch_an_empty_cache():
    git_provider = MagicMock()
    git_provider.diff_files = []
    tool = _make_tool(git_provider)

    assert tool._get_diff_file("app.py") is None
    git_provider.get_diff_files.assert_not_called()


def test_is_applicable_suggestion_preserves_blank_line_positions():
    git_provider = _provider_with_file("first()\n\nsecond()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 3, "first()\nsecond()") is False
    assert tool.is_applicable_suggestion("app.py", 1, 3, "first()\n\nsecond()") is True


def test_is_applicable_suggestion_preserves_relative_indentation():
    git_provider = _provider_with_file("if ready:\n    run()\n")
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 1, 2, "    if ready:\n        run()") is True
    assert tool.is_applicable_suggestion("app.py", 1, 2, "if ready:\nrun()") is False


def test_is_applicable_suggestion_uses_absolute_patch_lines_for_partial_head_content():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="first()\nlater()\n",
        head_file="first()\nlater()\n",
        patch=("@@ -10 +10 @@\n-old_first()\n+first()\n"
               "@@ -100 +100 @@\n-old_later()\n+later()\n"),
        filename="app.py",
        head_file_is_complete=False,
    )]
    tool = _make_tool(git_provider)

    assert tool.is_applicable_suggestion("app.py", 100, 100, "later()") is True


def test_persistent_update_survives_progress_cleanup_failure():
    """A failing progress-note cleanup must not abort the persistent update:
    if the cleanup error propagated, the caller would fall back to publishing
    a new suggestions thread, re-creating the duplicate-thread bug."""
    initial_header = "## PR Code Suggestions"
    existing = MagicMock()
    existing.body = f"{initial_header}\n<!-- aaa1111 -->\n<table>old suggestions</table>"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [existing]
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    # First edit updates the persistent comment and succeeds; the second edit
    # (re-labelling the progress note before deletion) fails.
    provider.edit_comment.side_effect = [None, RuntimeError("cleanup failed")]
    progress_note = MagicMock()

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider, f"{initial_header}\n<table>new suggestions</table>", initial_header,
        update_header=False, name="suggestions", final_update_message=False,
        progress_response=progress_note)

    assert result is existing
    assert provider.edit_comment.call_count == 2
    provider.remove_comment.assert_not_called()
    provider.publish_comment.assert_not_called()


def _persistent_provider(existing_comments):
    provider = MagicMock()
    provider.get_issue_comments.return_value = existing_comments
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    return provider


def test_custom_heading_migrates_legacy_persistent_suggestions_in_place():
    legacy = MagicMock()
    legacy.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n<table>old suggestions</table>"
    )
    provider = _persistent_provider([legacy])
    custom_header = "## Guideline Improvement Suggestions ✨"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is legacy
    updated = provider.edit_comment.call_args.args[1]
    assert updated.startswith(
        f"{custom_header}\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- deadbee -->"
    )
    assert "Suggestions up to commit aaa1111" in updated
    provider.publish_comment.assert_not_called()


def test_marked_persistent_suggestions_take_precedence_over_legacy_comment():
    legacy = MagicMock()
    legacy.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n<table>legacy</table>"
    )
    marked = MagicMock()
    marked.body = (
        "## Previous Custom Heading ✨\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- bbb2222 -->\n\n<table>marked</table>"
    )
    provider = _persistent_provider([legacy, marked])
    custom_header = "## Latest Custom Heading ✨"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is marked
    assert provider.edit_comment.call_args.args[0] is marked
    assert provider.edit_comment.call_args.args[1].startswith(
        f"{custom_header}\n\n{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
    )
    provider.publish_comment.assert_not_called()


def test_persistent_suggestions_do_not_adopt_quoted_or_late_identity():
    human = MagicMock()
    human.body = (
        "## Human discussion\n\n"
        f"> {PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        f"Quoted output:\n{PRCodeSuggestionsHeader.SUMMARY.value}\n"
    )
    provider = _persistent_provider([human])
    custom_header = "## Team Suggestions ✨"

    PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    provider.edit_comment.assert_not_called()
    published = provider.publish_comment.call_args.args[0]
    assert published.startswith(
        f"{custom_header}\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- deadbee -->\n\n"
    )


def test_legacy_heading_without_generated_shape_is_not_adopted():
    human = MagicMock()
    human.body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        "A human-authored comment using the same heading."
    )
    provider = _persistent_provider([human])
    custom_header = "## Team Suggestions ✨"

    PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    provider.edit_comment.assert_not_called()
    published = provider.publish_comment.call_args.args[0]
    assert PRCodeSuggestionsIdentity.SUMMARY.value in published


def test_custom_heading_is_kept_when_a_history_section_already_exists():
    existing = MagicMock()
    existing.body = (
        "## Previous Custom Heading ✨\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n"
        "Latest suggestions up to commit aaa1111\n\n"
        "<table>latest</table>\n\n___\n\n"
        "#### Previous suggestions\n"
        "<details><summary>Suggestions up to commit 0000000</summary>\n"
        "<br><table>older</table>\n\n</details>\n"
    )
    provider = _persistent_provider([existing])
    custom_header = "## Latest Custom Heading ✨"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{custom_header}\n\n<table>new suggestions</table>",
        initial_header=custom_header,
        name="suggestions",
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is existing
    updated = provider.edit_comment.call_args.args[1]
    assert updated.startswith(
        f"{custom_header}\n\n{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n<!-- deadbee -->"
    )
    assert "Suggestions up to commit aaa1111" in updated
    assert "Suggestions up to commit 0000000" in updated
    provider.publish_comment.assert_not_called()
