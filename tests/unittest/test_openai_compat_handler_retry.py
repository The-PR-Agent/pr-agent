import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from tenacity import RetryError

import pr_agent.algo.ai_handlers.openai_ai_handler as openai_handler


@pytest.fixture(params=["openai", "langchain"])
def handler_and_completion(request, monkeypatch):
    completion = AsyncMock()
    settings = SimpleNamespace(openai=SimpleNamespace(key="test-key"), get=lambda key, default=None: default)
    if request.param == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(openai_handler, "get_settings", lambda: settings)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        monkeypatch.setattr(openai_handler, "AsyncOpenAI", lambda: client)
        return openai_handler.OpenAIHandler(), completion, openai_handler.OPENAI_RETRIES

    class FakeRunnable:
        async def ainvoke(self, **kwargs):
            return await completion(**kwargs)

    messages = ModuleType("langchain_core.messages")
    messages.HumanMessage = lambda content: SimpleNamespace(content=content)
    messages.SystemMessage = lambda content: SimpleNamespace(content=content)
    runnables = ModuleType("langchain_core.runnables")
    runnables.Runnable = FakeRunnable
    clients = ModuleType("langchain_openai")
    clients.ChatOpenAI = type("ChatOpenAI", (), {})
    clients.AzureChatOpenAI = type("AzureChatOpenAI", (), {})
    for module in (messages, runnables, clients):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    module_name = "pr_agent.algo.ai_handlers.langchain_ai_handler"
    # Restore the original module at teardown so optional-dependency tests remain isolated.
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    langchain_handler = importlib.import_module(module_name)
    monkeypatch.setattr(langchain_handler, "get_settings", lambda: settings)
    monkeypatch.setattr(
        langchain_handler.LangChainOpenAIHandler, "_create_chat_async", AsyncMock(return_value=FakeRunnable())
    )
    return langchain_handler.LangChainOpenAIHandler(), completion, langchain_handler.OPENAI_RETRIES


def _error(error_type):
    request = httpx.Request("POST", "http://model.invalid")
    if error_type is openai.APITimeoutError:
        return error_type(request=request)
    if error_type is openai.APIError:
        return error_type("temporary failure", request=request, body=None)
    status = {openai.BadRequestError: 400, openai.UnprocessableEntityError: 422, openai.RateLimitError: 429}[error_type]
    return error_type("rejected request", response=httpx.Response(status, request=request), body=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [openai.BadRequestError, openai.UnprocessableEntityError, openai.RateLimitError])
async def test_rejected_requests_surface_without_same_handler_replay(handler_and_completion, error_type):
    handler, completion, _ = handler_and_completion
    error = _error(error_type)
    completion.side_effect = error

    with pytest.raises(error_type) as raised:
        await handler.chat_completion(model="gpt-test", system="sys", user="usr")

    assert raised.value is error
    assert completion.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [openai.APITimeoutError, openai.APIError])
async def test_retryable_errors_keep_attempt_count_and_retryerror(handler_and_completion, error_type):
    handler, completion, attempts = handler_and_completion
    error = _error(error_type)
    completion.side_effect = error

    with pytest.raises(RetryError) as raised:
        await handler.chat_completion(model="gpt-test", system="sys", user="usr")

    assert raised.value.last_attempt.exception() is error
    assert completion.await_count == attempts
