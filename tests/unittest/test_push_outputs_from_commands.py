"""`/describe` and `/improve` reach the configured sinks, the same way `/review` already does.

`push_outputs` was wired into `PRReviewer` only, so an operator running the three default
automatic commands received one of the three results. The two questions worth pinning down are
whether each command emits at all, and what it sends: a sink is not a git provider, so the GFM
table `/improve` publishes to GitHub is not what should arrive in Slack.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.review_json_output import hosted_markdown_export, review_json_output_path
from pr_agent.algo.run_details import add_token_usage
from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions, render_suggestions_markdown
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_questions import NO_ANALYZABLE_FILES_NOTE, PRQuestions
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.commands.pr_update_changelog import PRUpdateChangelog
from pr_agent.config_loader import get_settings

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
# Which commands emit
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("export", [False, True])
async def test_review_sinks_are_disabled_for_hosted_markdown_export(monkeypatch, export):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer._pending_sink_output = ("review", {}, "# Review")
    sink = AsyncMock()
    monkeypatch.setattr("pr_agent.commands.pr_reviewer.push_outputs_async", sink)

    token = hosted_markdown_export.set(export)
    try:
        await reviewer._deliver_sink_output()
    finally:
        hosted_markdown_export.reset(token)

    if export:
        sink.assert_not_awaited()
    else:
        sink.assert_awaited_once_with(
            "review", payload={}, markdown="# Review", git_provider=reviewer.git_provider,
        )
    assert reviewer._pending_sink_output is None


def test_review_still_emits():
    """Control: the command that already emitted keeps doing so."""
    assert "push_outputs_async" in PRReviewer._deliver_sink_output.__code__.co_names


def test_describe_emits_from_its_publish_path():
    assert "push_outputs_async" in PRDescription.run.__code__.co_names


def test_improve_emits_from_its_publish_path():
    assert "push_outputs_async" in PRCodeSuggestions.run.__code__.co_names


@pytest.mark.parametrize("no_analyzable_files, show_help", [(False, False), (False, True), (True, False)])
async def test_ask_emits_before_publishing_the_answer(monkeypatch, no_analyzable_files, show_help):
    command = PRQuestions.__new__(PRQuestions)
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.question_str = "What changed?"
    command.prediction = None
    command.git_provider = MagicMock()
    command.git_provider.is_supported.return_value = show_help
    command.git_provider.supports_threaded_pr_questions.return_value = False
    events = []

    async def prepare(_model):
        command.no_analyzable_files = no_analyzable_files
        command.prediction = "The return value changed."

    async def record(message_type, **kwargs):
        events.append(("sink", message_type, kwargs))

    def publish(comment, **kwargs):
        if not kwargs.get("is_temporary"):
            events.append(("comment", comment))

    command._prepare_prediction = prepare
    command.git_provider.publish_comment.side_effect = publish
    monkeypatch.setattr("pr_agent.commands.pr_questions.retry_with_fallback_models",
                        lambda fn, model_type: fn("fake-model"))
    monkeypatch.setattr("pr_agent.commands.pr_questions.push_outputs_async", record)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setitem(get_settings().pr_questions, "enable_help_text", show_help)

    await command.run()

    assert [event[0] for event in events] == ["sink", "comment"]
    _, message_type, sink = events[0]
    assert message_type == "ask"
    assert sink["payload"] == {
        "question": "What changed?",
        "answer": NO_ANALYZABLE_FILES_NOTE if no_analyzable_files else "The return value changed.",
    }
    assert "### **Answer:**" in sink["markdown"]
    assert "What changed?" in sink["markdown"]
    assert "<details>" not in sink["markdown"]
    if show_help:
        assert "<details>" in events[1][1]
    assert sink["git_provider"] is command.git_provider


@pytest.mark.parametrize("no_analyzable_files", [False, True])
async def test_ask_writes_json_without_publishing_or_sinks(monkeypatch, tmp_path, no_analyzable_files):
    command = PRQuestions.__new__(PRQuestions)
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.question_str = "What changed?"
    command.prediction = None
    command.git_provider = MagicMock()
    command.git_provider.is_supported.return_value = False

    async def prepare(_model):
        command.no_analyzable_files = no_analyzable_files
        command.prediction = " The return value changed. "
        if not no_analyzable_files:
            add_token_usage({"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})

    command._prepare_prediction = prepare
    sinks = AsyncMock()
    monkeypatch.setattr("pr_agent.commands.pr_questions.retry_with_fallback_models",
                        lambda fn, model_type: fn("fake-model"))
    monkeypatch.setattr("pr_agent.commands.pr_questions.push_outputs_async", sinks)
    monkeypatch.setitem(get_settings().config, "publish_output", False)
    output = tmp_path / "answer.json"
    token = review_json_output_path.set(str(output))
    try:
        await command.run()
    finally:
        review_json_output_path.reset(token)

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "question": "What changed?",
        "answer": NO_ANALYZABLE_FILES_NOTE if no_analyzable_files else "The return value changed.",
        "usage": {} if no_analyzable_files else {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }
    command.git_provider.publish_comment.assert_not_called()
    sinks.assert_not_awaited()


@pytest.mark.parametrize("publish_output", [True, False])
async def test_changelog_emits_only_the_new_entry_when_publishing(monkeypatch, publish_output):
    command = PRUpdateChangelog.__new__(PRUpdateChangelog)
    command.changelog_path = "docs/HISTORY.md"
    command.changelog_file = "# Previous changelog\n\n- Earlier change"
    command.changelog_read_error = None
    command.commit_changelog = False
    command.push_skipped_reason = None
    command.prediction = None
    command.git_provider = MagicMock()
    events = []

    async def prepare(_callback, model_type):
        command.prediction = "```markdown\n### Added\n- New feature\n```"

    async def record(message_type, **kwargs):
        events.append(("sink", message_type, kwargs))

    def publish(body, **kwargs):
        if not kwargs.get("is_temporary"):
            events.append(("comment", body))

    command.git_provider.publish_comment.side_effect = publish
    monkeypatch.setattr("pr_agent.commands.pr_update_changelog.retry_with_fallback_models", prepare)
    monkeypatch.setattr("pr_agent.commands.pr_update_changelog.push_outputs_async", record)
    monkeypatch.setitem(get_settings().config, "publish_output", publish_output)

    await command.run()

    if not publish_output:
        assert events == []
        return

    assert [event[0] for event in events] == ["sink", "comment"]
    _, message_type, sink = events[0]
    assert message_type == "update_changelog"
    assert sink["payload"] == {"path": "docs/HISTORY.md", "entry": "### Added\n- New feature"}
    assert sink["markdown"] == "**Changelog draft:**\n\n### Added\n- New feature"
    assert sink["git_provider"] is command.git_provider
    assert "Previous changelog" not in sink["markdown"]
    assert "to commit the new content" not in sink["markdown"]


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

    async def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.push_outputs_async", record)
    monkeypatch.setattr("pr_agent.commands.pr_description.push_outputs_async", record)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    return calls


async def test_improve_sends_readable_markdown(emitted, monkeypatch):
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.git_provider.get_files.return_value = ["src/foo.py"]
    command.git_provider.is_supported.return_value = False
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.progress_response = None
    command._output_published = False
    command.is_extended = False
    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await command.run()

    assert emitted, "the improve publish path emitted nothing"
    _args, kwargs = emitted[0]
    assert kwargs["markdown"] is not None
    assert "src/foo.py:12-18" in kwargs["markdown"]
    assert "<table>" not in kwargs["markdown"]


def _awaitable(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro()


async def test_improve_still_publishes_to_the_provider(emitted, monkeypatch):
    """Control: emitting to a sink is additional, not instead of the pull request comment."""
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.git_provider.get_files.return_value = ["src/foo.py"]
    command.git_provider.is_supported.return_value = False
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.progress_response = None
    command._output_published = False
    command.is_extended = False
    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await command.run()

    command.git_provider.remove_initial_comment.assert_called_once()


@pytest.mark.asyncio
async def test_improve_external_output_reports_partial_suggestion_coverage(emitted, monkeypatch):
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.git_provider.get_files.return_value = ["src/foo.py"]
    command.git_provider.is_supported.return_value = False
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.progress_response = None
    command.remaining_files_list = ["omitted.py"]
    command.failed_chunk_count = 1
    command.total_chunk_count = 2
    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await command.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "src/foo.py:12-18" in markdown
    assert "1 of 2 analysis chunks failed" in markdown
    assert "omitted.py" in markdown


@pytest.mark.asyncio
async def test_improve_external_output_reports_no_suggestions_and_omitted_files(emitted, monkeypatch):
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.git_provider.get_files.return_value = ["src/foo.py"]
    command.git_provider.is_supported.return_value = False
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.progress_response = None
    command.remaining_files_list = ["omitted.py"]
    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": []})))

    await command.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "No code suggestions found in the successfully analyzed chunks." in markdown
    assert "omitted.py" in markdown


@pytest.mark.asyncio
async def test_improve_external_output_does_not_report_filtered_suggestions(emitted, monkeypatch):
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.git_provider.get_files.return_value = ["src/foo.py"]
    command.git_provider.is_supported.return_value = True
    command._is_suggestion_line_range_valid = MagicMock(return_value=False)
    command.pr_url = "https://github.com/org/repo/pull/1"
    command.progress = "Preparing suggestions..."
    command.progress_response = None
    command.remaining_files_list = ["omitted.py"]
    monkeypatch.setattr("pr_agent.commands.pr_code_suggestions.retry_with_fallback_models",
                        MagicMock(return_value=_awaitable({"code_suggestions": [SUGGESTION]})))

    await command.run()

    assert len(emitted) == 1
    markdown = emitted[0][1]["markdown"]
    assert "No code suggestions found in the successfully analyzed chunks." in markdown
    assert "src/foo.py:12-18" not in markdown
    assert "omitted.py" in markdown
