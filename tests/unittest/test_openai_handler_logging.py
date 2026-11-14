"""Verify request-scoped log protection in the alternative OpenAI handler."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pr_agent.algo.ai_handlers.openai_ai_handler as openai_handler_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import withhold_prompt_logging

SECRET = "Sensitive repository instructions echoed by a model"


def _make_handler(monkeypatch, *, error=None):
    logger = MagicMock()
    monkeypatch.setattr(openai_handler_module, "get_logger", lambda: logger)
    monkeypatch.setattr(openai_handler_module, "record_ai_call", lambda usage: None)
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=SECRET), finish_reason="stop")],
        usage=None,
    )
    create = AsyncMock(side_effect=error) if error else AsyncMock(return_value=completion)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_handler_module, "AsyncOpenAI", lambda: client)
    handler = openai_handler_module.OpenAIHandler.__new__(openai_handler_module.OpenAIHandler)
    monkeypatch.setattr(
        handler, "build_request_messages",
        lambda model, system, user: [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return handler, create, logger


@pytest.mark.asyncio
async def test_openai_handler_withheld_request_does_not_log_prompts_or_response(monkeypatch):
    handler, create, logger = _make_handler(monkeypatch)
    with withhold_prompt_logging(True):
        response, reason = await handler.chat_completion(
            "gpt-4o", system=SECRET, user=SECRET, img_path=SECRET
        )

    assert response == SECRET
    assert reason == "stop"
    assert SECRET in str(create.call_args.kwargs["messages"])
    assert SECRET not in str(logger.mock_calls)

    logger.reset_mock()
    await handler.chat_completion("gpt-4o", system=SECRET, user=SECRET)
    assert SECRET in str(logger.mock_calls)


@pytest.mark.asyncio
async def test_openai_handler_withheld_error_omits_provider_message(monkeypatch):
    handler, _, logger = _make_handler(monkeypatch, error=RuntimeError(SECRET))
    with withhold_prompt_logging(True):
        with pytest.raises(RuntimeError, match=SECRET):
            await handler.chat_completion("gpt-4o", system=SECRET, user=SECRET)

    assert "RuntimeError" in str(logger.warning.call_args)
    assert SECRET not in str(logger.mock_calls)
