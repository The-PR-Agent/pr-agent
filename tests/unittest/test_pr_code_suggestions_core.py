import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.tools.pr_code_suggestions as pr_code_suggestions_module
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import PRCodeSuggestionsHeader, PRCodeSuggestionsIdentity
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


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


@pytest.mark.asyncio
async def test_prepare_prediction_main_caps_suggestions_per_file_after_chunk_merge():
    settings_snapshot = snapshot_settings((
        "pr_code_suggestions.decouple_hunks",
        "pr_code_suggestions.parallel_calls",
        "pr_code_suggestions.max_suggestions_per_file",
    ))
    settings = get_settings()
    settings.pr_code_suggestions.decouple_hunks = True
    settings.pr_code_suggestions.parallel_calls = False
    settings.set("pr_code_suggestions.max_suggestions_per_file", 1)
    tool = _make_tool()
    tool.token_handler = MagicMock()

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        return {"code_suggestions": [_valid_suggestion(
            one_sentence_summary=f"Finding from {patches_diff}",
            relevant_lines_start=1 if patches_diff == "chunk-a" else 2,
        )]}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", return_value=["chunk-a", "chunk-b"]):
            tool._get_prediction = fake_get_prediction

            data = await tool.prepare_prediction_main("primary-model")
    finally:
        restore_settings(settings_snapshot)

    assert data["code_suggestions"] == [_valid_suggestion(
        one_sentence_summary="Finding from chunk-a",
        relevant_lines_start=1,
    )]


def test_limit_suggestions_per_file_keeps_highest_scores_and_preserves_other_files():
    settings_snapshot = snapshot_settings(("pr_code_suggestions.max_suggestions_per_file",))
    settings = get_settings()
    settings.set("pr_code_suggestions.max_suggestions_per_file", 2)
    tool = _make_tool()
    suggestions = [
        _valid_suggestion(one_sentence_summary="Low", score=3),
        _valid_suggestion(one_sentence_summary="High", score=9),
        _valid_suggestion(one_sentence_summary="Medium", score=6),
        _valid_suggestion(one_sentence_summary="Other file", relevant_file="worker.py", score=1),
    ]

    try:
        limited = tool._limit_suggestions_per_file(suggestions)
    finally:
        restore_settings(settings_snapshot)

    assert limited == [suggestions[1], suggestions[2], suggestions[3]]


def test_limit_suggestions_per_file_is_inert_at_the_shipped_default():
    tool = _make_tool()
    suggestions = [
        _valid_suggestion(one_sentence_summary="First", score=3),
        _valid_suggestion(one_sentence_summary="Second", score=9),
    ]

    assert tool._limit_suggestions_per_file(suggestions) == suggestions


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


@pytest.mark.asyncio
async def test_prepare_prediction_main_rebuilds_unnumbered_chunks_after_conversion_fallback():
    settings = get_settings()
    original_decouple_hunks = settings.pr_code_suggestions.decouple_hunks
    original_parallel_calls = settings.pr_code_suggestions.parallel_calls
    settings.pr_code_suggestions.decouple_hunks = False
    settings.pr_code_suggestions.parallel_calls = False
    tool = _make_tool()
    tool.token_handler = MagicMock()
    tool.convert_to_decoupled_with_line_numbers = AsyncMock(return_value=[])
    chunk_pairs = []

    async def fake_get_prediction(model, patches_diff, patches_diff_no_line_numbers):
        chunk_pairs.append((patches_diff, patches_diff_no_line_numbers))
        return {"code_suggestions": [_valid_suggestion(relevant_file=f"chunk-{len(chunk_pairs)}.py")]}

    try:
        with patch.object(pr_code_suggestions_module, "get_pr_multi_diffs", side_effect=[
            ["stale unnumbered chunk"],
            ["1 fallback-a", "2 fallback-b"],
        ]):
            tool._get_prediction = fake_get_prediction

            data = await tool.prepare_prediction_main("primary-model")
    finally:
        settings.pr_code_suggestions.decouple_hunks = original_decouple_hunks
        settings.pr_code_suggestions.parallel_calls = original_parallel_calls

    assert chunk_pairs == [
        ("1 fallback-a", "fallback-a"),
        ("2 fallback-b", "fallback-b"),
    ]
    assert tool.total_chunk_count == 2
    assert len(data["code_suggestions"]) == 2


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
@pytest.mark.parametrize("improved_code", ["return new(", "return await new()"])
async def test_invalid_python_replacement_is_published_as_a_pr_comment(improved_code):
    git_provider = _provider_with_file("def fetch():\n    return old()\n")
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code=improved_code,
            score=8,
        )
    ]})

    git_provider.publish_code_suggestions.assert_not_called()
    body = git_provider.publish_comment.call_args.args[0]
    assert "```suggestion" not in body
    assert "because the proposed Python code has invalid syntax" in body
    assert "`app.py:2-2`" in body


@pytest.mark.asyncio
async def test_invalid_python_replacement_stays_in_a_noncommittable_artifact():
    git_provider = _provider_with_file("def fetch():\n    return old()\n")
    git_provider.supports_code_suggestions_artifact.return_value = True
    git_provider.publish_code_suggestions_artifact.return_value = True
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old()",
            improved_code="return new(",
            score=8,
        )
    ]})

    published = git_provider.publish_code_suggestions_artifact.call_args.args[0]
    assert len(published) == 1
    assert "```suggestion" not in published[0]["body"]
    assert "because the proposed Python code has invalid syntax" in published[0]["body"]
    git_provider.publish_code_suggestions.assert_not_called()
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_context_dependent_multiline_python_replacement_remains_committable():
    git_provider = _provider_with_file(
        "async def fetch():\n"
        "    return await old(\n"
        "        source,\n"
        "    )\n"
    )
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=2,
            relevant_lines_end=4,
            existing_code="return await old(\n    source,\n)",
            improved_code="return await new()",
            score=8,
        )
    ]})

    body = _published_suggestion(git_provider)["body"]
    assert "```suggestion\n    return await new()\n```" in body
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_non_python_replacement_remains_best_effort():
    git_provider = _provider_with_file(
        "function fetch() {\n  return old();\n}\n",
        filename="app.js",
    )
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_file="app.js",
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old();",
            improved_code="return (",
            score=8,
            language="python",
        )
    ]})

    assert "```suggestion\n  return (\n```" in _published_suggestion(git_provider)["body"]
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_python_replacement_with_incomplete_file_context_remains_best_effort():
    git_provider = MagicMock()
    git_provider.diff_files = [FilePatchInfo(
        base_file="",
        head_file="def fetch():\n    return old()\n",
        patch="@@ -19,2 +19,2 @@\n def fetch():\n-    return older()\n+    return old()\n",
        filename="app.py",
        head_file_is_complete=False,
    )]
    git_provider.publish_code_suggestions.return_value = True
    tool = _make_tool(git_provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [
        _valid_suggestion(
            relevant_lines_start=20,
            relevant_lines_end=20,
            existing_code="return old()",
            improved_code="return new(",
            score=8,
        )
    ]})

    assert "```suggestion\n    return new(\n```" in _published_suggestion(git_provider)["body"]
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
async def test_python_replacement_in_an_unparseable_file_remains_best_effort():
    git_provider = _provider_with_file("def broken(:\n    return old()\n")
    tool = _make_tool(git_provider)

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
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("compile_side_effect", [
    [RecursionError("baseline too deeply nested")],
    [None, RecursionError("replacement too deeply nested")],
])
async def test_python_validation_failure_remains_best_effort(compile_side_effect):
    git_provider = _provider_with_file("def fetch():\n    return old()\n")
    tool = _make_tool(git_provider)

    with patch(
        "pr_agent.tools.pr_code_suggestions.compile",
        side_effect=compile_side_effect,
        create=True,
    ):
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
    git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_length", "improved_code", "truncated_code"), [
    (6, "return new_value", "return"),
    (2, "    return new()", ""),
])
async def test_truncated_replacement_is_published_as_a_pr_comment(
        max_length, improved_code, truncated_code):
    settings = get_settings()
    snapshot = snapshot_settings((
        "pr_code_suggestions.max_code_suggestion_length",
        "pr_code_suggestions.suggestion_truncation_message",
    ))
    try:
        settings.set("pr_code_suggestions.max_code_suggestion_length", max_length)
        settings.set("pr_code_suggestions.suggestion_truncation_message", "")
        git_provider = _provider_with_file(
            "function fetch() {\n  return old();\n}\n",
            filename="app.js",
        )
        tool = _make_tool(git_provider)
        suggestion = PRCodeSuggestions._truncate_if_needed(_valid_suggestion(
            relevant_file="app.js",
            relevant_lines_start=2,
            relevant_lines_end=2,
            existing_code="return old();",
            improved_code=improved_code,
            score=8,
        ))

        assert suggestion["improved_code"].rstrip() == truncated_code
        await tool.push_inline_code_suggestions({"code_suggestions": [suggestion]})

        git_provider.publish_code_suggestions.assert_not_called()
        body = git_provider.publish_comment.call_args.args[0]
        assert "```suggestion" not in body
        assert "because the proposed code was truncated" in body
        assert "`app.js:2-2`" in body
    finally:
        restore_settings(snapshot)


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
    git_provider.supports_code_suggestion_state.return_value = False
    tool = _make_tool(git_provider)
    tool.progress_response = MagicMock()

    await tool.publish_no_suggestions()

    call = git_provider.edit_comment.call_args
    edited_body = call.kwargs.get("body", call.args[1])
    assert "No code suggestions found for the PR." in edited_body
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


def test_load_suggestion_discussion_context_delegates_to_provider():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.get_code_suggestion_thread_context.return_value = '[{"thread_id": 1}]'
    tool = _make_tool(provider)

    assert tool._load_suggestion_discussion_context() == '[{"thread_id": 1}]'


def test_load_suggestion_discussion_context_degrades_to_empty():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.get_code_suggestion_thread_context.side_effect = RuntimeError("unavailable")
    tool = _make_tool(provider)

    assert tool._load_suggestion_discussion_context() == ""


def test_load_suggestion_discussion_context_ignores_other_providers():
    provider = MagicMock()
    provider.supports_code_suggestion_state.return_value = False
    tool = _make_tool(provider)

    assert tool._load_suggestion_discussion_context() == ""
    provider.get_code_suggestion_thread_context.assert_not_called()


def _render_suggestions_user_prompt(prompt_key: str, discussion_context: str) -> str:
    from jinja2 import Environment, StrictUndefined

    variables = {
        "title": "Test PR",
        "date": "2024-01-01",
        "diff_no_line_numbers": "+value",
        "duplicate_prompt_examples": False,
        "suggestion_discussion_context": discussion_context,
    }
    environment = Environment(undefined=StrictUndefined, autoescape=True)
    return environment.from_string(get_settings().get(prompt_key)).render(variables)


@pytest.mark.parametrize("prompt_key", [
    "pr_code_suggestions_prompt.user",
    "pr_code_suggestions_prompt_not_decoupled.user",
])
def test_suggestion_prompt_includes_untrusted_discussion_context(prompt_key):
    prompt = _render_suggestions_user_prompt(
        prompt_key,
        '[{"thread_id": 7, "replies": [{"author": "alice", "message": "Keep this nullable"}]}]',
    )

    assert "Keep this nullable" in prompt
    assert "untrusted data" in prompt


@pytest.mark.parametrize("prompt_key", [
    "pr_code_suggestions_prompt.user",
    "pr_code_suggestions_prompt_not_decoupled.user",
])
def test_suggestion_prompt_omits_discussion_section_when_context_empty(prompt_key):
    prompt = _render_suggestions_user_prompt(prompt_key, "")

    assert "Prior code-suggestion discussions" not in prompt
    assert "untrusted data" not in prompt


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
def test_code_suggestion_state_is_provider_driven():
    provider = MagicMock()

    assert GitProvider.supports_code_suggestion_state(provider) is False
    assert AzureDevopsProvider.supports_code_suggestion_state(provider) is True


@pytest.mark.asyncio
async def test_empty_incremental_run_reconciles_existing_suggestions():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.reconcile_code_suggestion_threads.return_value = 1
    provider.get_code_suggestion_thread_context.return_value = '[{"status": "fixed"}]'
    tool = _make_tool(provider)
    tool._incremental_empty_scope = True
    tool.pr_url = "https://example.test/pr/1"
    tool.vars = {"suggestion_discussion_context": '[{"status": "active"}]'}

    assert await tool.run() is None
    provider.reconcile_code_suggestion_threads.assert_called_once_with()
    provider.get_files.assert_not_called()
    assert tool.vars["suggestion_discussion_context"] == '[{"status": "fixed"}]'


def test_azure_persistent_comment_updates_without_history():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.supports_code_suggestion_state.return_value = True
    provider.get_issue_comments_newest_first.return_value = []
    existing = MagicMock()
    provider.publish_comment.return_value = existing

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        "## PR Code Suggestions ✨\n\nnew suggestions",
        "## PR Code Suggestions ✨",
        update_header=True,
        name="suggestions",
        final_update_message=False,
        max_previous_comments=0,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is existing
    provider.publish_comment.assert_called_once()
    assert PRCodeSuggestionsIdentity.SUMMARY.value in (
        provider.publish_comment.call_args.args[0]
    )
    provider.publish_persistent_comment.assert_not_called()


def test_azure_persistent_comment_without_history_keeps_identity_marker():
    published = {}

    def _publish_comment(pr_comment, is_temporary=False, thread_context=None):
        published["body"] = pr_comment
        return SimpleNamespace(body=pr_comment)

    # built without __init__ rather than subclassed: the real constructor needs a live Azure
    # connection, and a subclass that skips it trips CodeQL's missing-super-init rule
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.get_issue_comments = lambda: []
    provider.publish_comment = _publish_comment

    PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        "## Team Suggestions ✨\n\nnew suggestions",
        "## Team Suggestions ✨",
        update_header=True,
        name="suggestions",
        final_update_message=False,
        max_previous_comments=0,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert PRCodeSuggestionsIdentity.SUMMARY.value in published["body"]
    assert AzureDevopsProvider._is_agent_comment(published["body"])


def test_failed_azure_persistent_update_keeps_progress_comment():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.supports_code_suggestion_state.return_value = True
    existing = MagicMock()
    existing.body = "## PR Code Suggestions ✨\n\nold suggestions"
    provider.get_issue_comments_newest_first.return_value = [existing]
    provider.edit_comment.return_value = False
    fallback = MagicMock()
    provider.publish_comment.return_value = fallback
    progress = MagicMock()

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        "## PR Code Suggestions ✨\n\nnew suggestions",
        "## PR Code Suggestions ✨",
        update_header=True,
        name="suggestions",
        final_update_message=False,
        max_previous_comments=0,
        progress_response=progress,
    )

    assert result is fallback
    assert provider.edit_comment.call_count == 2
    assert provider.edit_comment.call_args_list[0].args[0] is existing
    assert provider.edit_comment.call_args_list[1].args[0] is progress
    provider.publish_comment.assert_called_once()
    provider.remove_comment.assert_called_once_with(progress)


def test_failed_azure_history_update_publishes_current_suggestions():
    provider = MagicMock(spec=AzureDevopsProvider)
    existing = MagicMock()
    existing.body = (
        "## Team Suggestions ✨\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n<table>old suggestions</table>"
    )
    progress = MagicMock()
    fallback = MagicMock()
    provider.get_issue_comments.return_value = [existing]
    provider.get_issue_comments_newest_first.return_value = [
        existing
    ]
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    provider.edit_comment.side_effect = [False, False]
    provider.publish_comment.return_value = fallback

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        "## Team Suggestions ✨\n\n<table>new suggestions</table>",
        "## Team Suggestions ✨",
        name="suggestions",
        progress_response=progress,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is fallback
    assert provider.edit_comment.call_args_list[0].args[0] is existing
    assert provider.edit_comment.call_args_list[1].args[0] is progress
    provider.publish_comment.assert_called_once()
    provider.remove_comment.assert_called_once_with(progress)


@pytest.mark.asyncio
async def test_azure_no_suggestions_uses_current_result_identity():
    provider = MagicMock(spec=AzureDevopsProvider)
    provider.supports_code_suggestion_state.return_value = True
    tool = _make_tool(provider)
    settings = get_settings()
    original_persistent = settings.pr_code_suggestions.persistent_comment
    original_publish_empty = settings.pr_code_suggestions.publish_output_no_suggestions
    original_history = settings.pr_code_suggestions.max_history_len
    original_publish_output = settings.config.publish_output
    try:
        settings.pr_code_suggestions.persistent_comment = True
        settings.pr_code_suggestions.publish_output_no_suggestions = True
        settings.pr_code_suggestions.max_history_len = 0
        settings.config.publish_output = True

        await tool.publish_no_suggestions()

        published = provider.publish_comment.call_args.args[0]
        assert published.startswith(
            "## PR Code Suggestions ✨\n\n"
            f"{PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value}\n\n"
        )
        assert published.endswith("No code suggestions found for the PR.")
        provider.publish_persistent_comment.assert_not_called()
    finally:
        settings.pr_code_suggestions.persistent_comment = original_persistent
        settings.pr_code_suggestions.publish_output_no_suggestions = original_publish_empty
        settings.pr_code_suggestions.max_history_len = original_history
        settings.config.publish_output = original_publish_output


def test_persistent_update_removes_progress_after_status_edit_failure():
    initial_header = "## PR Code Suggestions"
    existing = MagicMock()
    existing.body = f"{initial_header}\n<!-- aaa1111 -->\n<table>old suggestions</table>"
    provider = MagicMock()
    provider.get_issue_comments.return_value = [existing]
    provider.get_issue_comments_newest_first.return_value = [existing]
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    provider.edit_comment.side_effect = [None, RuntimeError("cleanup failed")]
    progress_note = MagicMock()

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider, f"{initial_header}\n<table>new suggestions</table>", initial_header,
        update_header=False, name="suggestions", final_update_message=False,
        progress_response=progress_note)

    assert result is existing
    assert provider.edit_comment.call_count == 2
    provider.remove_comment.assert_called_once_with(progress_note)
    provider.publish_comment.assert_not_called()


def _persistent_provider(existing_comments):
    provider = MagicMock()
    provider.get_issue_comments.return_value = existing_comments
    provider.get_issue_comments_newest_first.return_value = list(
        reversed(existing_comments)
    )
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


@pytest.mark.parametrize("raises", [False, True], ids=["returns-false", "raises"])
def test_first_persistent_improve_edit_failure_publishes_visible_fallback(raises):
    provider = MagicMock()
    provider.get_issue_comments.return_value = []
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"
    progress = MagicMock()
    fallback = MagicMock()
    provider.publish_comment.return_value = fallback
    if raises:
        provider.edit_comment.side_effect = RuntimeError("edit failed")
    else:
        provider.edit_comment.return_value = False

    header = "## PR Code Suggestions " + chr(0x2728)
    new_comment = (
        header + "\n\n"
        + PRCodeSuggestionsIdentity.SUMMARY.value + "\n\n"
        + "<!-- deadbee -->\n\n<table>new suggestions</table>\n\n"
    )
    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        header + "\n\n<table>new suggestions</table>",
        header,
        name="suggestions",
        final_update_message=False,
        progress_response=progress,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is fallback
    provider.edit_comment.assert_called_once_with(progress, new_comment)
    provider.publish_comment.assert_called_once()
    provider.remove_comment.assert_called_once_with(progress)


class _LifecycleSuggestionProvider:
    def __init__(self, comments=(), edit_results=(), supports_state=False):
        self.comments = list(comments)
        self.edit_results = list(edit_results)
        self._supports_state = supports_state
        self.edits = []
        self.published = []
        self.removed = []

    def get_issue_comments(self):
        return list(self.comments)

    def get_issue_comments_newest_first(self):
        return list(reversed(self.get_issue_comments()))

    def get_latest_commit_url(self):
        return "https://example.test/commit/deadbee"

    def get_comment_url(self, comment):
        return f"https://example.test/comment/{comment.name}"

    def edit_comment(self, comment, body):
        self.edits.append((comment, body))
        if self.edit_results:
            result = self.edit_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def publish_comment(self, body, **kwargs):
        comment = SimpleNamespace(body=body, name=f"published-{len(self.published)}")
        self.published.append((body, kwargs, comment))
        return comment

    def remove_comment(self, comment):
        self.removed.append(comment)

    def supports_code_suggestion_state(self):
        return self._supports_state

    def should_publish_improve_as_thread(self):
        return False

    def supports_code_suggestions_artifact(self):
        return False

    def is_supported(self, capability):
        return False

    def publish_persistent_comment(
        self,
        pr_comment,
        initial_header,
        update_header=True,
        name="review",
        final_update_message=True,
        identity_marker=None,
        legacy_initial_header=None,
    ):
        if self.comments:
            result = self.edit_comment(self.comments[-1], pr_comment)
            if result is False:
                return self.publish_comment(
                    f"{pr_comment}\n\n{identity_marker or ''}".rstrip()
                )
            return self.comments[-1]
        return self.publish_comment(
            f"{pr_comment}\n\n{identity_marker or ''}".rstrip()
        )


def _lifecycle_suggestion_comment(name):
    body = (
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        f"<!-- {name} -->\n\n<table>{name}</table>"
    )
    return SimpleNamespace(body=body, name=name)


def test_persistent_improve_uses_newest_matching_comment():
    old = _lifecycle_suggestion_comment("old")
    newest = _lifecycle_suggestion_comment("newest")
    provider = _LifecycleSuggestionProvider([old, newest])

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n<table>new suggestions</table>",
        PRCodeSuggestionsHeader.SUMMARY.value,
        name="suggestions",
        final_update_message=False,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is newest
    assert provider.edits[0][0] is newest
    assert provider.published == []


def test_persistent_improve_edit_failure_does_not_publish_duplicate_summary():
    existing = _lifecycle_suggestion_comment("existing")
    progress = SimpleNamespace(body="Preparing suggestions...", name="progress")
    provider = _LifecycleSuggestionProvider([existing], edit_results=[False, False])

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n<table>new suggestions</table>",
        PRCodeSuggestionsHeader.SUMMARY.value,
        name="suggestions",
        progress_response=progress,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is provider.published[0][2]
    assert len(provider.published) == 1
    failure_body = provider.published[0][0]
    assert PRCodeSuggestionsIdentity.SUMMARY.value not in failure_body
    assert "previous suggestions remain unchanged" in failure_body
    assert provider.removed == [progress]


@pytest.mark.asyncio
async def test_no_suggestions_failure_removes_stale_progress_comment():
    settings_snapshot = snapshot_settings(
        (
            "config.publish_output",
            "config.publish_output_progress",
            "pr_code_suggestions.publish_output_no_suggestions",
        )
    )
    try:
        settings = get_settings()
        settings.config.publish_output = True
        settings.config.publish_output_progress = True
        settings.pr_code_suggestions.publish_output_no_suggestions = True

        provider = _LifecycleSuggestionProvider(
            edit_results=[False],
        )
        progress = SimpleNamespace(body="Preparing suggestions...", name="progress")
        tool = _make_tool(provider)
        tool.progress_response = progress

        await tool.publish_no_suggestions()

        assert len(provider.published) == 1
        assert "No code suggestions found" in provider.published[0][0]
        assert provider.removed == [progress]
        assert tool.progress_response is None
    finally:
        restore_settings(settings_snapshot)


def test_stateful_no_history_edit_failure_has_no_duplicate_authoritative_summary():
    existing = _lifecycle_suggestion_comment("existing")
    provider = _LifecycleSuggestionProvider(
        [existing],
        edit_results=[False],
        supports_state=True,
    )

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        f"{PRCodeSuggestionsHeader.SUMMARY.value}\n\n<table>new suggestions</table>",
        PRCodeSuggestionsHeader.SUMMARY.value,
        name="suggestions",
        final_update_message=False,
        max_previous_comments=0,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
    )

    assert result is provider.published[0][2]
    assert len(provider.published) == 1
    failure_body = provider.published[0][0]
    assert PRCodeSuggestionsIdentity.SUMMARY.value not in failure_body
    assert "previous suggestions remain unchanged" in failure_body
