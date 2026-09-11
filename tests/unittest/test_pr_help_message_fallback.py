"""Question-mode help delegates failed attempts to the shared fallback loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.algo import run_details
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_help_message
from pr_agent.tools.pr_help_message import PRHelpMessage
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

PRIMARY = "gpt-4o"
BACKUP = "gpt-4o-mini"
ANSWER = (
    "response: Enable automatic review in the repository settings.\n"
    "relevant_sections:\n"
    "  - file_name: /tools/review.md\n"
    "    relevant_section_header_string: Automatic review\n"
)


@pytest.fixture
def help_tool(tmp_path, monkeypatch):
    overrides = {
        "config.model": PRIMARY,
        "config.fallback_models": [BACKUP],
        "config.publish_output": True,
        "model_routing.enable": False,
        # Keep this regression independent of the separate OpenAI-key gate fix.
        "openai.key": "test-only-key",
        "openai.deployment_id": "primary-deployment",
        "openai.fallback_deployments": ["backup-deployment"],
    }
    snapshot = snapshot_settings([*overrides, "pr_help_prompts.user"])
    for key, value in overrides.items():
        get_settings().set(key, value)

    doc = tmp_path / "docs" / "docs" / "tools" / "review.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Automatic review\n\nEnable automatic review in the repository settings.", encoding="utf-8")
    monkeypatch.setattr(pr_help_message, "__file__", str(tmp_path / "pr_agent" / "tools" / "pr_help_message.py"))

    details = run_details.RunDetails()
    monkeypatch.setattr(run_details, "get_run_details", lambda: details)
    logger = Mock()
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(pr_url="https://example.com/org/repo/pull/1", publish_comment=Mock())
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How do I configure automatic reviews?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}
    tool.token_handler = SimpleNamespace(count_tokens=len)
    try:
        yield tool, details, logger
    finally:
        restore_settings(snapshot)


def attempted_models(tool):
    return [call.kwargs["model"] for call in tool.ai_handler.chat_completion.await_args_list]


async def test_model_prompt_preserves_question_and_documentation_markup(help_tool):
    tool, _, _ = help_tool
    question = "What does `<review enabled=\"true\"> & 'notes'` mean?"
    snippets = "Use `a < b && c > d` and `<review enabled=\"true\">`."
    tool.vars.update(question=question, snippets=snippets)
    tool.ai_handler.chat_completion.return_value = ANSWER, "stop"

    assert await tool._prepare_prediction(PRIMARY) == ANSWER

    tool.ai_handler.chat_completion.assert_awaited_once()
    user_prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert question in user_prompt
    assert snippets in user_prompt


async def test_primary_failure_uses_backup_answer(help_tool):
    tool, details, _ = help_tool
    tool.ai_handler.chat_completion.side_effect = [RuntimeError("primary unavailable"), (ANSWER, "stop")]

    await tool.run()

    assert attempted_models(tool) == [PRIMARY, BACKUP]
    backup_prompt = tool.ai_handler.chat_completion.await_args_list[1].kwargs["user"]
    assert tool.question_str in backup_prompt
    assert "Enable automatic review in the repository settings." in backup_prompt
    tool.git_provider.publish_comment.assert_called_once()
    published_comment = tool.git_provider.publish_comment.call_args.args[0]
    assert "### Answer:\nEnable automatic review" in published_comment
    assert "> - https://docs.pr-agent.ai/tools/review/#automatic-review" in published_comment
    assert details.model_used == BACKUP
    assert details.fallback_used is True
    assert get_settings().get("openai.deployment_id") == "primary-deployment"


async def test_all_models_fail_without_publishing_no_information(help_tool):
    tool, details, logger = help_tool
    tool.ai_handler.chat_completion.side_effect = RuntimeError("provider unavailable")

    assert await tool.run() == ""

    assert attempted_models(tool) == [PRIMARY, BACKUP]
    tool.git_provider.publish_comment.assert_not_called()
    logger.exception.assert_called_once()
    assert "Failed to generate prediction with any model" in logger.exception.call_args.args[0]
    assert details.model_used is None
    assert get_settings().get("openai.deployment_id") == "primary-deployment"


@pytest.mark.parametrize(
    "response, expected",
    [(ANSWER, "### Answer:\nEnable automatic review"),
     ("response: No matching information\nrelevant_sections: []", "Could not find relevant information")],
)
async def test_successful_primary_response_does_not_use_backup(help_tool, response, expected):
    tool, details, _ = help_tool
    tool.ai_handler.chat_completion.return_value = response, "stop"

    await tool.run()

    assert attempted_models(tool) == [PRIMARY]
    tool.git_provider.publish_comment.assert_called_once()
    assert expected in tool.git_provider.publish_comment.call_args.args[0]
    assert details.model_used == PRIMARY
    assert details.fallback_used is False


async def test_cancellation_propagates_without_backup(help_tool):
    tool, details, logger = help_tool
    tool.ai_handler.chat_completion.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await tool.run()

    assert attempted_models(tool) == [PRIMARY]
    tool.git_provider.publish_comment.assert_not_called()
    logger.exception.assert_not_called()
    assert details.model_used is None
    assert get_settings().get("openai.deployment_id") == "primary-deployment"


async def test_invalid_prompt_reaches_final_failure_boundary(help_tool):
    tool, details, logger = help_tool
    get_settings().set("pr_help_prompts.user", "{{ missing_variable }}")
    tool._prepare_prediction = AsyncMock(wraps=tool._prepare_prediction)

    assert await tool.run() == ""

    assert [call.args[0] for call in tool._prepare_prediction.await_args_list] == [PRIMARY, BACKUP]
    tool.ai_handler.chat_completion.assert_not_called()
    tool.git_provider.publish_comment.assert_not_called()
    logger.exception.assert_called_once()
    assert "Failed to generate prediction with any model" in logger.exception.call_args.args[0]
    assert details.model_used is None
    assert get_settings().get("openai.deployment_id") == "primary-deployment"
