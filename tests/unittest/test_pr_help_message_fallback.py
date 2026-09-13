"""Question-mode help delegates failed attempts to the shared fallback loop."""

import asyncio
from math import ceil
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
        "config.max_output_tokens": 0,
        "config.model_token_count_estimate_factor": 0,
        "model_routing.enable": False,
        # Keep this regression independent of the separate OpenAI-key gate fix.
        "openai.key": "test-only-key",
        "openai.deployment_id": "primary-deployment",
        "openai.fallback_deployments": ["backup-deployment"],
    }
    snapshot = snapshot_settings([*overrides, "pr_help_prompts.system", "pr_help_prompts.user"])
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
    tool._fixture_doc = doc
    try:
        yield tool, details, logger
    finally:
        restore_settings(snapshot)


def attempted_models(tool):
    return [call.kwargs["model"] for call in tool.ai_handler.chat_completion.await_args_list]


def count_message_characters(*, messages, **_kwargs):
    return sum(len(message["content"]) for message in messages)


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


async def test_each_attempt_fits_complete_prompt_for_its_model(help_tool, monkeypatch):
    tool, details, _ = help_tool
    tool._fixture_doc.write_text("# Automatic review\n\n" + "A" * 600, encoding="utf-8")
    get_settings().set("pr_help_prompts.system", "system")
    get_settings().set("pr_help_prompts.user", "question={{ question }}\nsnippets={{ snippets }}")
    get_settings().set("config.max_output_tokens", 20)
    limits = {PRIMARY: 2_000, BACKUP: 260}
    limit_calls = []
    counted_models = []

    def model_limit(model, ignore_max_model_tokens=False):
        limit_calls.append((model, ignore_max_model_tokens))
        return limits[model]

    def model_token_count(*, model, messages):
        counted_models.append(model)
        character_count = count_message_characters(messages=messages)
        return character_count if model == PRIMARY else character_count * 2

    monkeypatch.setattr(pr_help_message, "get_max_tokens", model_limit)
    monkeypatch.setattr(pr_help_message, "token_counter", model_token_count)
    tool.ai_handler.chat_completion.side_effect = [RuntimeError("primary unavailable"), (ANSWER, "stop")]

    await tool.run()

    assert attempted_models(tool) == [PRIMARY, BACKUP]
    primary_prompt = tool.ai_handler.chat_completion.await_args_list[0].kwargs["user"]
    backup_prompt = tool.ai_handler.chat_completion.await_args_list[1].kwargs["user"]
    assert "A" * 600 in primary_prompt
    assert "...(truncated)" not in primary_prompt
    assert "A" * 600 not in backup_prompt
    assert "...(truncated)" in backup_prompt
    assert "==file name==" in backup_prompt
    assert "/tools/review.md" in backup_prompt
    assert 2 * (len(backup_prompt) + len("system")) <= limits[BACKUP] - 20
    assert "A" * 600 in tool.vars["snippets"]
    assert limit_calls == [(PRIMARY, True), (BACKUP, True)]
    assert PRIMARY in counted_models
    assert BACKUP in counted_models
    assert details.model_used == BACKUP


@pytest.mark.parametrize(
    ("configured_output", "expected_budget"),
    [
        (400, 2_600),
        ("400", 2_600),
        (0, 1_000),
        (-1, 1_000),
        (None, 1_000),
        ("invalid", 1_000),
        (400.0, 2_600),
        (400.5, 2_600),
        (float("nan"), 1_000),
        (float("inf"), 1_000),
        (True, 2_999),
        (False, 1_000),
        ("1" * 5_000, 1_000),
    ],
)
def test_prompt_budget_honors_positive_output_limit_or_help_default(
    help_tool, monkeypatch, configured_output, expected_budget
):
    tool, _, _ = help_tool
    get_settings().set("config.max_output_tokens", configured_output)
    monkeypatch.setattr(pr_help_message, "get_max_tokens", lambda *_args, **_kwargs: 3_000)

    assert tool._get_prompt_budget(PRIMARY) == expected_budget


def test_prompt_budget_uses_handler_reported_output_limit(help_tool, monkeypatch):
    tool, _, _ = help_tool
    get_settings().set("config.max_output_tokens", 400)
    tool.ai_handler.get_output_token_limit = Mock(return_value=1_600)
    monkeypatch.setattr(pr_help_message, "get_max_tokens", lambda *_args, **_kwargs: 3_000)

    assert tool._get_prompt_budget(PRIMARY) == 1_400
    tool.ai_handler.get_output_token_limit.assert_called_once_with(PRIMARY)


def test_prompt_budget_uses_handler_reported_output_reserve(help_tool, monkeypatch):
    tool, _, _ = help_tool
    get_settings().set("config.max_output_tokens", 400)
    tool.ai_handler.get_output_token_reserve = Mock(return_value=1_600)
    monkeypatch.setattr(pr_help_message, "get_max_tokens", lambda *_args, **_kwargs: 3_000)

    assert tool._get_prompt_budget(PRIMARY) == 1_400
    tool.ai_handler.get_output_token_reserve.assert_called_once_with(PRIMARY, 2_000)


@pytest.mark.parametrize("reported_limit", [None, 0, -1, True, 1.5, "1600"])
def test_unusable_handler_output_limit_falls_back_to_config(
    help_tool, monkeypatch, reported_limit
):
    tool, _, _ = help_tool
    get_settings().set("config.max_output_tokens", 400)
    tool.ai_handler.get_output_token_limit = Mock(return_value=reported_limit)
    monkeypatch.setattr(pr_help_message, "get_max_tokens", lambda *_args, **_kwargs: 3_000)

    assert tool._get_prompt_budget(PRIMARY) == 2_600


def test_failing_handler_output_limit_falls_back_to_config(help_tool, monkeypatch):
    tool, _, logger = help_tool
    get_settings().set("config.max_output_tokens", 400)
    tool.ai_handler.get_output_token_limit = Mock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(pr_help_message, "get_max_tokens", lambda *_args, **_kwargs: 3_000)

    assert tool._get_prompt_budget(PRIMARY) == 2_600
    assert any("output token limit" in call.args[0] for call in logger.debug.call_args_list)


@pytest.mark.parametrize(
    ("estimate_factor", "expected"),
    [
        (-0.5, 53),
        (0, 53),
        (None, 53),
        ("invalid", 53),
        (float("nan"), 53),
        (float("inf"), 53),
        (True, 53),
        (0.3, ceil(53 * 1.3)),
    ],
)
def test_local_prompt_estimate_never_reduces_encoded_content(
    help_tool, monkeypatch, estimate_factor, expected
):
    tool, _, _ = help_tool

    class CharacterEncoder:
        @staticmethod
        def encode(text, disallowed_special=()):
            return list(text)

    get_settings().set("config.model_token_count_estimate_factor", estimate_factor)
    monkeypatch.setattr(pr_help_message, "token_counter", Mock(side_effect=RuntimeError("counter unavailable")),
                        raising=False)
    monkeypatch.setattr(pr_help_message.TokenEncoder, "get_token_encoder", lambda _model: CharacterEncoder(),
                        raising=False)

    assert tool._count_prompt_tokens(PRIMARY, "abc", "de") == expected


def test_local_prompt_estimate_uses_raw_estimate_on_overflow(help_tool, monkeypatch):
    tool, _, _ = help_tool

    class CharacterEncoder:
        @staticmethod
        def encode(text, disallowed_special=()):
            return list(text)

    get_settings().set("config.model_token_count_estimate_factor", 1e308)
    monkeypatch.setattr(pr_help_message, "token_counter", Mock(side_effect=RuntimeError("counter unavailable")))
    monkeypatch.setattr(pr_help_message.TokenEncoder, "get_token_encoder", lambda _model: CharacterEncoder())

    assert tool._count_prompt_tokens(PRIMARY, "abc", "de") == 53


@pytest.mark.parametrize("counter_mode", ["error", "zero", "boolean", "non_integer"])
async def test_unusable_model_count_uses_local_estimate_before_provider_call(
    help_tool, monkeypatch, counter_mode
):
    tool, _, _ = help_tool
    tool.vars = {"question": "q", "snippets": "A" * 100}
    get_settings().set("pr_help_prompts.system", "")
    get_settings().set("pr_help_prompts.user", "{{ snippets }}")
    monkeypatch.setattr(tool, "_get_prompt_budget", lambda _model: 70)

    class CharacterEncoder:
        @staticmethod
        def encode(text, disallowed_special=()):
            return list(text)

    def unusable_count(**_kwargs):
        if counter_mode == "error":
            raise RuntimeError("counter unavailable")
        return {"zero": 0, "boolean": True, "non_integer": 1.5}[counter_mode]

    monkeypatch.setattr(pr_help_message, "token_counter", unusable_count)
    monkeypatch.setattr(pr_help_message.TokenEncoder, "get_token_encoder", lambda _model: CharacterEncoder())
    tool.ai_handler.chat_completion.return_value = ANSWER, "stop"

    await tool._prepare_prediction(PRIMARY)

    tool.ai_handler.chat_completion.assert_awaited_once()
    user_prompt = tool.ai_handler.chat_completion.await_args.kwargs["user"]
    assert "...(truncated)" in user_prompt
    assert len(user_prompt) + 48 <= 70


async def test_local_estimate_overflow_still_tries_providers(help_tool, monkeypatch):
    tool, details, _ = help_tool

    class CharacterEncoder:
        @staticmethod
        def encode(text, disallowed_special=()):
            return list(text)

    get_settings().set("config.model_token_count_estimate_factor", 1e308)
    monkeypatch.setattr(pr_help_message, "token_counter", Mock(side_effect=RuntimeError("counter unavailable")))
    monkeypatch.setattr(pr_help_message.TokenEncoder, "get_token_encoder", lambda _model: CharacterEncoder())
    tool._prepare_prediction = AsyncMock(wraps=tool._prepare_prediction)
    tool.ai_handler.chat_completion.side_effect = [RuntimeError("primary unavailable"), (ANSWER, "stop")]

    await tool.run()

    assert [call.args[0] for call in tool._prepare_prediction.await_args_list] == [PRIMARY, BACKUP]
    assert attempted_models(tool) == [PRIMARY, BACKUP]
    tool.git_provider.publish_comment.assert_called_once()
    assert details.model_used == BACKUP


async def test_empty_claude_system_prompt_is_normalized_before_fitting(help_tool, monkeypatch):
    tool, _, _ = help_tool
    model = "anthropic/claude-sonnet-4-5"
    tool.vars = {"question": "q", "snippets": "A" * 100}
    get_settings().set("pr_help_prompts.system", "")
    get_settings().set("pr_help_prompts.user", "{{ snippets }}")
    monkeypatch.setattr(tool, "_get_prompt_budget", lambda _model: 80)
    tool.ai_handler.normalize_request_prompts = pr_help_message.LiteLLMAIHandler.normalize_request_prompts
    counted_system_prompts = []

    def count_normalized_prompts(*, messages, **_kwargs):
        counted_system_prompts.append(messages[0]["content"])
        return count_message_characters(messages=messages)

    monkeypatch.setattr(pr_help_message, "token_counter", count_normalized_prompts)
    tool.ai_handler.chat_completion.return_value = ANSWER, "stop"

    await tool._prepare_prediction(model)

    assert counted_system_prompts
    assert set(counted_system_prompts) == {"No system prompt provided"}
    call = tool.ai_handler.chat_completion.await_args
    assert call.kwargs["system"] == "No system prompt provided"
    assert "...(truncated)" in call.kwargs["user"]


def test_prompt_fitting_checks_smallest_nonempty_prefix(help_tool, monkeypatch):
    tool, _, _ = help_tool
    get_settings().set("pr_help_prompts.system", "")
    get_settings().set("pr_help_prompts.user", "{{ snippets }}")
    monkeypatch.setattr(tool, "_get_prompt_budget", lambda _model: 10)

    def discontinuous_count(*, messages, **_kwargs):
        snippets = messages[1]["content"]
        if snippets in ("", pr_help_message.TRUNCATION_MARKER):
            return 1
        if snippets == f"A{pr_help_message.TRUNCATION_MARKER}":
            return 10
        return 1_000

    monkeypatch.setattr(pr_help_message, "token_counter", discontinuous_count)

    _, user_prompt = tool._fit_prompts({"question": "q", "snippets": "ABC"}, PRIMARY)

    assert user_prompt == f"A{pr_help_message.TRUNCATION_MARKER}"


async def test_marker_only_prompt_is_sent_when_no_document_character_fits(help_tool, monkeypatch):
    tool, _, _ = help_tool
    tool.vars = {"question": "q", "snippets": "ABC"}
    get_settings().set("pr_help_prompts.system", "")
    get_settings().set("pr_help_prompts.user", "{{ snippets }}")
    monkeypatch.setattr(tool, "_get_prompt_budget", lambda _model: 10)

    def marker_only_count(*, messages, **_kwargs):
        snippets = messages[1]["content"]
        if snippets == "":
            return 1
        if snippets == pr_help_message.TRUNCATION_MARKER:
            return 5
        return 1_000

    monkeypatch.setattr(pr_help_message, "token_counter", marker_only_count)
    tool.ai_handler.chat_completion.return_value = ANSWER, "stop"

    await tool._prepare_prediction(PRIMARY)

    tool.ai_handler.chat_completion.assert_awaited_once()
    assert tool.ai_handler.chat_completion.await_args.kwargs["user"] == pr_help_message.TRUNCATION_MARKER


async def test_fixed_prompt_overhead_skips_model_call_and_tries_larger_fallback(help_tool, monkeypatch):
    tool, details, _ = help_tool
    get_settings().set("pr_help_prompts.system", "S" * 300)
    get_settings().set("pr_help_prompts.user", "question={{ question }}\nsnippets={{ snippets }}")
    get_settings().set("config.max_output_tokens", 20)
    limits = {PRIMARY: 250, BACKUP: 2_000}

    monkeypatch.setattr(pr_help_message, "get_max_tokens",
                        lambda model, ignore_max_model_tokens=False: limits[model])
    monkeypatch.setattr(pr_help_message, "token_counter", count_message_characters, raising=False)
    tool._prepare_prediction = AsyncMock(wraps=tool._prepare_prediction)
    tool.ai_handler.chat_completion.return_value = ANSWER, "stop"

    await tool.run()

    assert [call.args[0] for call in tool._prepare_prediction.await_args_list] == [PRIMARY, BACKUP]
    assert attempted_models(tool) == [BACKUP]
    assert details.model_used == BACKUP


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
