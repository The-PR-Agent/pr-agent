"""`/describe` and `/improve` reach the configured sinks, the same way `/review` already does.

`push_outputs` was wired into `PRReviewer` only, so an operator running the three default
automatic commands received one of the three results. The two questions worth pinning down are
whether each tool emits at all, and what it sends: a sink is not a git provider, so the GFM
table `/improve` publishes to GitHub is not what should arrive in Slack.
"""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo import run_output
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions, render_suggestions_markdown
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer

SUGGESTION = {
    "relevant_file": "src/foo.py",
    "relevant_lines_start": 12,
    "relevant_lines_end": 18,
    "label": "possible issue",
    "score": 8,
    "one_sentence_summary": "Guard the index before dereferencing it",
    "suggestion_content": "The loop can run past the end of the list.",
}


# --------------------------------------------------------------------------------------
# Which tools emit
# --------------------------------------------------------------------------------------
def test_review_still_emits():
    """Control: the tool that already emitted keeps doing so."""
    assert "async_push_outputs" in PRReviewer._prepare_pr_review.__code__.co_names


@pytest.mark.asyncio
async def test_describe_waits_for_one_sink_emission_before_labels_and_description(monkeypatch):
    tool = PRDescription.__new__(PRDescription)
    tool.pr_id = "1"
    tool.git_provider = MagicMock()
    tool.git_provider.is_supported.side_effect = lambda feature: feature == "get_labels"
    tool.git_provider.get_pr_labels.return_value = []
    tool.vars = {}
    tool.prediction = "generated"
    tool.data = {"title": "AI title", "description": "Description"}
    tool._prepare_data = MagicMock()
    tool._prepare_labels = MagicMock(return_value=["enhancement"])
    tool._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", "Walkthrough"))
    monkeypatch.setattr("pr_agent.tools.pr_description.extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr("pr_agent.tools.pr_description.retry_with_fallback_models", AsyncMock())
    for key, value in {
        "publish_output": True, "is_auto_command": True,
        "output_relevant_configurations": False, "output_run_details": False,
    }.items():
        monkeypatch.setattr(get_settings().config, key, value)
    for key, value in {
        "enable_semantic_files_types": False, "publish_labels": True,
        "use_description_markers": False, "enable_help_text": False,
        "enable_help_comment": False, "publish_description_as_comment": False,
        "generate_ai_title": True, "final_update_message": False,
    }.items():
        monkeypatch.setattr(get_settings().pr_description, key, value)

    started = asyncio.Event()
    release = asyncio.Event()

    async def emit(*args, **kwargs):
        started.set()
        await release.wait()

    sink = AsyncMock(side_effect=emit)
    monkeypatch.setattr("pr_agent.tools.pr_description.async_push_outputs", sink)
    running = asyncio.create_task(tool.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not running.done()
        tool.git_provider.publish_labels.assert_not_called()
        tool.git_provider.publish_description.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=1)

    markdown = "Description\n\nWalkthrough___\n\n"
    sink.assert_awaited_once_with("describe", payload=tool.data, markdown=markdown)
    tool.git_provider.publish_labels.assert_called_once_with(["enhancement"])
    tool.git_provider.publish_description.assert_called_once_with(
        "AI title", "<!-- pr-agent-generated -->\n" + markdown,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True], ids=["published", "publication-failure"])
async def test_describe_repropagates_deferred_sink_cancellation_after_provider_output(
        monkeypatch, provider_fails
):
    tool = PRDescription.__new__(PRDescription)
    tool.pr_id = "1"
    tool.git_provider = MagicMock()
    tool.git_provider.is_supported.side_effect = lambda feature: feature == "get_labels"
    tool.git_provider.get_pr_labels.return_value = []
    tool.vars = {}
    tool.prediction = "generated"
    tool.data = {"title": "AI title", "description": "Description"}
    tool._prepare_data = MagicMock()
    tool._prepare_labels = MagicMock(return_value=["enhancement"])
    tool._prepare_pr_answer = MagicMock(return_value=("AI title", "Description", "Walkthrough"))
    monkeypatch.setattr("pr_agent.tools.pr_description.extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr("pr_agent.tools.pr_description.retry_with_fallback_models", AsyncMock())
    monkeypatch.setattr(
        "pr_agent.tools.pr_description.async_push_outputs", AsyncMock(return_value=True)
    )
    if provider_fails:
        tool.git_provider.publish_description.side_effect = RuntimeError("provider unavailable")
    for key, value in {
        "publish_output": True, "is_auto_command": True,
        "output_relevant_configurations": False, "output_run_details": False,
    }.items():
        monkeypatch.setattr(get_settings().config, key, value)
    for key, value in {
        "enable_semantic_files_types": False, "publish_labels": True,
        "use_description_markers": False, "enable_help_text": False,
        "enable_help_comment": False, "publish_description_as_comment": False,
        "generate_ai_title": True, "final_update_message": False,
    }.items():
        monkeypatch.setattr(get_settings().pr_description, key, value)

    with pytest.raises(asyncio.CancelledError):
        await tool.run()

    tool.git_provider.publish_labels.assert_called_once_with(["enhancement"])
    tool.git_provider.publish_description.assert_called_once()


@pytest.mark.asyncio
async def test_improve_repropagates_deferred_sink_cancellation_when_provider_finalization_fails(monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.git_provider.remove_initial_comment.side_effect = RuntimeError("provider unavailable")
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    monkeypatch.setattr(get_settings().config, "publish_output_progress", False)
    monkeypatch.setattr(
        "pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
        AsyncMock(return_value={"code_suggestions": [SUGGESTION]}),
    )
    monkeypatch.setattr(
        "pr_agent.tools.pr_code_suggestions.async_push_outputs", AsyncMock(return_value=True)
    )

    with pytest.raises(asyncio.CancelledError):
        await tool.run()

    tool.git_provider.remove_initial_comment.assert_called_once()


def test_improve_emits_from_its_publish_path():
    assert "async_push_outputs" in PRCodeSuggestions.run.__code__.co_names


# --------------------------------------------------------------------------------------
# What `/improve` sends
#
# The provider gets `generate_summarized_suggestions`, a GFM table wrapped in <table> and
# <details> HTML, and only when the provider supports gfm_markdown. Slack and Telegram render
# neither, so sending that - or, worse, no markdown at all, leaving the sink with raw JSON -
# makes the notification unreadable.
# --------------------------------------------------------------------------------------
def test_the_suggestion_markdown_names_the_location():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "**src/foo.py:12-18**" in rendered


def test_the_suggestion_markdown_carries_the_label_and_score():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "possible issue" in rendered
    assert "score 8" in rendered


def test_the_suggestion_markdown_carries_the_summary():
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "Guard the index before dereferencing it" in rendered


def test_the_suggestion_markdown_is_not_html():
    """The point of a separate renderer: no <table>/<details> that a chat client shows raw."""
    rendered = render_suggestions_markdown({"code_suggestions": [SUGGESTION]})

    assert "<table>" not in rendered
    assert "<details>" not in rendered


def test_a_single_line_suggestion_is_not_written_as_a_range():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{**SUGGESTION, "relevant_lines_start": 12, "relevant_lines_end": 12}]})

    assert "**src/foo.py:12**" in rendered


def test_every_suggestion_is_rendered():
    rendered = render_suggestions_markdown({"code_suggestions": [
        SUGGESTION,
        {**SUGGESTION, "relevant_file": "src/bar.py", "one_sentence_summary": "Close the file"},
    ]})

    assert "src/foo.py" in rendered
    assert "src/bar.py" in rendered
    assert "Close the file" in rendered


def test_the_content_is_used_when_there_is_no_one_sentence_summary():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{k: v for k, v in SUGGESTION.items() if k != "one_sentence_summary"}]})

    assert "The loop can run past the end of the list." in rendered


@pytest.mark.parametrize("data", [
    {},
    {"code_suggestions": []},
    {"code_suggestions": None},
    {"code_suggestions": ["not a dict"]},
])
def test_an_empty_or_malformed_answer_still_renders(data):
    """The renderer runs on the publish path, so it must not be able to fail the command."""
    assert render_suggestions_markdown(data) == "## PR Code Suggestions\n\nNo suggestions to report."


def test_a_suggestion_without_a_file_is_still_rendered():
    rendered = render_suggestions_markdown(
        {"code_suggestions": [{"one_sentence_summary": "Something", "relevant_file": ""}]})

    assert "(file not reported)" in rendered
    assert "Something" in rendered


# --------------------------------------------------------------------------------------
# End to end through the real publish paths
# --------------------------------------------------------------------------------------
@pytest.fixture
def emitted(monkeypatch):
    calls = []

    async def emit(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.async_push_outputs", emit)
    monkeypatch.setattr("pr_agent.tools.pr_description.async_push_outputs", emit)
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    return calls


async def test_improve_sends_readable_markdown(emitted, monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await tool.run()

    assert emitted, "the improve publish path emitted nothing"
    _args, kwargs = emitted[0]
    assert kwargs["markdown"] is not None
    assert "src/foo.py:12-18" in kwargs["markdown"]
    assert "<table>" not in kwargs["markdown"]


def _awaitable(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro()


@pytest.mark.parametrize("suggestions", [[SUGGESTION], []], ids=["suggestions", "no-suggestions"])
@pytest.mark.asyncio
async def test_improve_waits_for_sink_before_provider_output(monkeypatch, suggestions):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.git_provider.supports_code_suggestions_artifact.return_value = not bool(suggestions)
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        AsyncMock(return_value={"code_suggestions": suggestions}))
    started = asyncio.Event()
    release = asyncio.Event()

    async def emit(*args, **kwargs):
        started.set()
        await release.wait()

    sink = AsyncMock(side_effect=emit)
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.async_push_outputs", sink)
    running = asyncio.create_task(tool.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not running.done()
        tool.git_provider.remove_initial_comment.assert_not_called()
        tool.git_provider.publish_code_suggestions_artifact.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=2)

    sink.assert_awaited_once()
    assert sink.await_args.args == ("improve",)
    assert sink.await_args.kwargs["payload"] == {"code_suggestions": suggestions}
    if suggestions:
        tool.git_provider.remove_initial_comment.assert_called_once()
    else:
        tool.git_provider.publish_code_suggestions_artifact.assert_called_once()


@pytest.mark.asyncio
async def test_improve_cancellation_during_sink_still_finalizes_provider_output(monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        AsyncMock(return_value={"code_suggestions": [SUGGESTION]}))
    get_settings().set("PUSH_OUTPUTS.ENABLE", True)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_push(*_args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr(run_output, "push_outputs", blocking_push)
    running = asyncio.create_task(tool.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        running.cancel()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        tool.git_provider.remove_initial_comment.assert_not_called()
    finally:
        release.set()
        get_settings().set("PUSH_OUTPUTS.ENABLE", False)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=2)
    tool.git_provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
async def test_improve_timeout_waits_for_provider_finalization_then_expires(monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool._output_published = False
    tool.is_extended = False
    monkeypatch.setattr(get_settings().config, "publish_output", True)
    monkeypatch.setattr(
        "pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
        AsyncMock(return_value={"code_suggestions": [SUGGESTION]}),
    )
    get_settings().set("PUSH_OUTPUTS.ENABLE", True)
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_push(*_args):
        assert release.wait(timeout=5)

    monkeypatch.setattr(run_output, "push_outputs", blocking_push)
    loop.call_later(0.05, release.set)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await tool.run()
    finally:
        release.set()
        get_settings().set("PUSH_OUTPUTS.ENABLE", False)

    tool.git_provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
async def test_improve_external_output_reports_partial_suggestion_coverage(emitted, monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool.remaining_files_list = ["omitted.py"]
    tool.failed_chunk_count = 1
    tool.total_chunk_count = 2
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await tool.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "src/foo.py:12-18" in markdown
    assert "1 of 2 analysis chunks failed" in markdown
    assert "omitted.py" in markdown


@pytest.mark.asyncio
async def test_improve_external_output_reports_no_suggestions_and_omitted_files(emitted, monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = False
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress_response = None
    tool.remaining_files_list = ["omitted.py"]
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": []})))

    await tool.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "No code suggestions found in the successfully analyzed chunks." in markdown
    assert "omitted.py" in markdown


@pytest.mark.asyncio
async def test_improve_external_output_does_not_report_filtered_suggestions(emitted, monkeypatch):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.git_provider.get_files.return_value = ["src/foo.py"]
    tool.git_provider.is_supported.return_value = True
    tool._is_suggestion_line_range_valid = MagicMock(return_value=False)
    tool.pr_url = "https://github.com/org/repo/pull/1"
    tool.progress = "Preparing suggestions..."
    tool.progress_response = None
    tool.remaining_files_list = ["omitted.py"]
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await tool.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "No code suggestions found in the successfully analyzed chunks." in markdown
    assert "src/foo.py:12-18" not in markdown
    assert "omitted.py" in markdown
