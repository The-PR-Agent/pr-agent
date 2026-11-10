"""/ask on a PR whose changed files are all filtered out of the diff."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.algo.run_details import command_failed, init_run_details
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_questions import NO_ANALYZABLE_FILES_NOTE, PRQuestions
from tests.unittest._run_details_command_helpers import (
    BACKUP,
    PRIMARY,
    TITLE,
    _provider,
    _published_bodies,
    command_run_settings,
)


@pytest.fixture(autouse=True)
def _run_settings():
    with command_run_settings() as settings:
        settings.set("pr_questions.enable_help_text", False)
        yield settings


def _make_ask(monkeypatch, remaining_files):
    """Run the real _prepare_prediction against a diff with no analyzable patch left."""
    tool = PRQuestions.__new__(PRQuestions)
    tool.pr_url = "https://example.com/org/repo/pull/1"
    tool.question_str = "Is this change safe?"
    tool.vars = {"conversation_history": "", "diff": ""}
    tool.prediction = None
    tool.patches_diff = None
    tool.no_analyzable_files = False
    tool.git_provider = _provider()
    tool.ai_handler = MagicMock(spec=[])
    tool._get_prediction = AsyncMock(return_value="unused")
    requested_models = []

    def fake_get_pr_diff(_provider, _token_handler, model, return_remaining_files=False, **_kwargs):
        requested_models.append(model)
        # The diff is empty: every changed file was dropped by file-type filtering (lockfile-only
        # PR), or the files are still there but did not fit the budget.
        return ("", list(remaining_files)) if return_remaining_files else ""

    monkeypatch.setattr("pr_agent.tools.pr_questions.get_pr_diff", fake_get_pr_diff)
    monkeypatch.setattr(
        "pr_agent.tools.pr_questions.AttemptTokenBudget.for_prompt_attempt",
        lambda *_args, **_kwargs: MagicMock(),
    )
    init_run_details()
    return tool, requested_models


async def test_ask_answers_with_a_note_when_every_changed_file_is_filtered(monkeypatch):
    tool, requested_models = _make_ask(monkeypatch, remaining_files=[])

    await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert requested_models == [PRIMARY]
    assert command_failed() is False
    [body] = _published_bodies(tool.git_provider)
    assert "Is this change safe?" in body
    assert f"### **Answer:**\n{NO_ANALYZABLE_FILES_NOTE}" in body
    # No model answered, so there are no run details to report.
    assert TITLE not in body
    tool.git_provider.publish_comment.assert_any_call("Preparing answer...", is_temporary=True)
    tool.git_provider.remove_initial_comment.assert_called_once()


async def test_ask_stores_the_note_when_output_is_not_published(monkeypatch):
    get_settings().set("config.publish_output", False)
    tool, _ = _make_ask(monkeypatch, remaining_files=[])

    await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert get_settings().get("data.answer") == NO_ANALYZABLE_FILES_NOTE
    assert _published_bodies(tool.git_provider) == []


async def test_ask_still_walks_the_fallback_chain_when_files_remain_but_do_not_fit(monkeypatch):
    tool, requested_models = _make_ask(monkeypatch, remaining_files=["app.py"])

    with pytest.raises(Exception, match="Failed to generate prediction"):
        await tool.run()

    tool._get_prediction.assert_not_awaited()
    assert requested_models == [PRIMARY, BACKUP]
    assert _published_bodies(tool.git_provider) == []
    tool.git_provider.remove_initial_comment.assert_called_once()
