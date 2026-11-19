"""Commands other than `/review` reach the configured sinks too.

The tests pin down both whether each command emits and what it sends, since a sink is not a
git provider and needs plain markdown rather than provider-specific presentation.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.review_json_output import hosted_markdown_export, review_json_output_path
from pr_agent.algo.run_details import add_token_usage
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_questions import NO_ANALYZABLE_FILES_NOTE, PRQuestions
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.commands.pr_update_changelog import PRUpdateChangelog
from pr_agent.config_loader import get_settings

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
    assert "push_outputs_async" in PRDescription._run_describe.__code__.co_names


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
