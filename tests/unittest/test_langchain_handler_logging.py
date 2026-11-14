"""Check request-local logging protection for optional LangChain inference."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pr_agent.algo.ai_handlers.langchain_ai_handler as langchain_handler_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import withhold_prompt_logging

SECRET = "Repository guidance in rejected model request"


@pytest.mark.asyncio
async def test_langchain_handler_error_respects_request_log_guard(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(langchain_handler_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        langchain_handler_module, "get_settings",
        lambda: SimpleNamespace(get=lambda *_args: None),
    )
    # LangChain is optional: the error occurs before invoking any model-specific Runnable.
    monkeypatch.setattr(langchain_handler_module, "SystemMessage", lambda content: content, raising=False)
    monkeypatch.setattr(langchain_handler_module, "HumanMessage", lambda content: content, raising=False)
    handler = langchain_handler_module.LangChainOpenAIHandler.__new__(
        langchain_handler_module.LangChainOpenAIHandler
    )
    monkeypatch.setattr(
        handler, "build_request_messages",
        lambda model, system, user: [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    create = AsyncMock(side_effect=RuntimeError(SECRET))
    monkeypatch.setattr(handler, "_create_chat_async", create)

    with withhold_prompt_logging(True):
        with pytest.raises(RuntimeError, match=SECRET):
            await handler.chat_completion("gpt-4o", system=SECRET, user=SECRET, img_path=SECRET)

    assert SECRET not in str(logger.mock_calls)
    assert "RuntimeError" in str(logger.warning.call_args)

    logger.reset_mock()
    with pytest.raises(RuntimeError, match=SECRET):
        await handler.chat_completion("gpt-4o", system=SECRET, user=SECRET)
    assert SECRET in str(logger.warning.call_args)


@pytest.mark.asyncio
async def test_langchain_handler_withholds_langsmith_tracing_only_for_guarded_request(monkeypatch):
    events = []

    @contextmanager
    def fake_tracing_context(*, enabled):
        events.append(("trace_start", enabled))
        try:
            yield
        finally:
            events.append(("trace_end", enabled))

    class FakeModel:
        async def ainvoke(self, **kwargs):
            events.append(("invoke", kwargs["model"]))
            return SimpleNamespace(content="generated docs", response_metadata={})

    monkeypatch.setattr(langchain_handler_module, "Runnable", FakeModel, raising=False)
    monkeypatch.setattr(langchain_handler_module, "ChatOpenAI", FakeModel, raising=False)
    monkeypatch.setattr(langchain_handler_module, "AzureChatOpenAI", FakeModel, raising=False)
    monkeypatch.setattr(langchain_handler_module, "tracing_context", fake_tracing_context, raising=False)
    monkeypatch.setattr(
        langchain_handler_module,
        "get_settings",
        lambda: SimpleNamespace(get=lambda *_args: None),
    )
    monkeypatch.setattr(langchain_handler_module, "SystemMessage", lambda content: content, raising=False)
    monkeypatch.setattr(langchain_handler_module, "HumanMessage", lambda content: content, raising=False)
    handler = langchain_handler_module.LangChainOpenAIHandler.__new__(
        langchain_handler_module.LangChainOpenAIHandler
    )
    monkeypatch.setattr(
        handler,
        "build_request_messages",
        lambda model, system, user: [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    monkeypatch.setattr(handler, "_create_chat_async", AsyncMock(return_value=FakeModel()))

    with withhold_prompt_logging(True):
        assert (await handler.chat_completion("gpt-4o", system=SECRET, user=SECRET))[0] == "generated docs"
    assert events == [("trace_start", False), ("invoke", "gpt-4o"), ("trace_end", False)]

    events.clear()
    assert (await handler.chat_completion("gpt-4o", system="ordinary", user="ordinary"))[0] == "generated docs"
    assert events == [("invoke", "gpt-4o")]
