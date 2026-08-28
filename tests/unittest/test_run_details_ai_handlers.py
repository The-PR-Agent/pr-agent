import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

import pr_agent.algo.ai_handlers.openai_ai_handler as openai_handler
from pr_agent.algo.run_details import get_run_details, init_run_details


class _FakeSettings:
    def __init__(self, api_type=""):
        self.openai = SimpleNamespace(
            key="test-key",
            org="test-org",
            api_type=api_type,
            api_version="2024-01-01",
            api_base="https://example.invalid",
        )

    def get(self, key, default=None):
        values = {
            "OPENAI.ORG": None,
            "OPENAI.API_TYPE": self.openai.api_type,
            "OPENAI.API_VERSION": self.openai.api_version,
            "OPENAI.API_BASE": self.openai.api_base,
            "OPENAI.DEPLOYMENT_ID": None,
        }
        return values.get(key, default)


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


@pytest.mark.asyncio
async def test_openai_handler_records_successful_call_with_usage(monkeypatch):
    monkeypatch.setattr(openai_handler, "get_settings", _FakeSettings)

    class _FakeCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="hello"), finish_reason="stop")],
                usage=_Usage(10, 2, 12),
            )

    class _FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(openai_handler, "AsyncOpenAI", _FakeClient)

    init_run_details()
    handler = openai_handler.OpenAIHandler()

    response = await handler.chat_completion(model="gpt-test", system="sys", user="usr")

    details = get_run_details()
    assert response == ("hello", "stop")
    assert details.num_ai_calls == 1
    assert details.prompt_tokens == 10
    assert details.completion_tokens == 2
    assert details.total_tokens == 12


@pytest.mark.asyncio
async def test_langchain_handler_records_successful_call_without_usage(monkeypatch):
    class _FakeRunnable:
        async def ainvoke(self, input):
            return SimpleNamespace(content="hello")

    langchain_core_messages = ModuleType("langchain_core.messages")
    langchain_core_messages.HumanMessage = lambda content: SimpleNamespace(content=content)
    langchain_core_messages.SystemMessage = lambda content: SimpleNamespace(content=content)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", langchain_core_messages)

    langchain_core_runnables = ModuleType("langchain_core.runnables")
    langchain_core_runnables.Runnable = _FakeRunnable
    monkeypatch.setitem(sys.modules, "langchain_core.runnables", langchain_core_runnables)

    langchain_openai = ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = type("ChatOpenAI", (), {})
    langchain_openai.AzureChatOpenAI = type("AzureChatOpenAI", (), {})
    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)

    # delitem (not pop) so teardown evicts the copy that was built against the fake langchain modules
    monkeypatch.delitem(sys.modules, "pr_agent.algo.ai_handlers.langchain_ai_handler", raising=False)
    langchain_handler = importlib.import_module("pr_agent.algo.ai_handlers.langchain_ai_handler")
    monkeypatch.setattr(langchain_handler, "get_settings", _FakeSettings)

    async def _fake_create_chat_async(self, deployment_id=None):
        return _FakeRunnable()

    monkeypatch.setattr(langchain_handler.LangChainOpenAIHandler, "_create_chat_async", _fake_create_chat_async)

    init_run_details()
    handler = langchain_handler.LangChainOpenAIHandler()

    response = await handler.chat_completion(model="gpt-test", system="sys", user="usr")

    details = get_run_details()
    assert response == ("hello", "completed")
    assert details.num_ai_calls == 1
    assert details.has_token_usage is False
