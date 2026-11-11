"""Keep LangChain token limits tied to its OpenAI client, not LiteLLM routing."""

from types import SimpleNamespace

import pytest

import pr_agent.algo.ai_handlers.langchain_ai_handler as langchain_module
import pr_agent.algo.token_budget as budget_module
import pr_agent.algo.token_handler as token_module
from pr_agent.commands.pr_help_message import PRHelpMessage


@pytest.fixture
def settings(monkeypatch):
    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "LANGSMITH_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    values = {"OPENAI.API_TYPE": "", "OPENAI.API_BASE": "https://api.openai.com/v1"}
    settings = SimpleNamespace(
        config=SimpleNamespace(model="gpt-6-sol", custom_model_max_tokens=0, max_model_tokens=0),
        litellm=SimpleNamespace(custom_llm_provider="ollama"),
        openai=SimpleNamespace(key="test-key", api_version="test-version", api_base="https://azure.example"),
        get=lambda key, default=None: values.get(key, default),
    )
    for module in (langchain_module, budget_module, token_module):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(langchain_module, "_LANGCHAIN_INSTALLED", True)
    monkeypatch.setattr(token_module.TokenEncoder, "get_token_encoder", lambda model=None: SimpleNamespace(
        encode=lambda text, **kwargs: list(text),
    ))
    return settings, values


def make_budget(handler):
    return budget_module.AttemptTokenBudget.for_prompt_attempt(
        "gpt-6-sol", None, {}, "system", "user", ai_handler=handler,
    )


@pytest.mark.parametrize("api_type", ["", "azure"])
async def test_native_client_keeps_input_ceiling_despite_litellm_provider(monkeypatch, settings, api_type):
    config, values = settings
    values["OPENAI.API_TYPE"] = api_type
    clients = []
    for name in ("ChatOpenAI", "AzureChatOpenAI"):
        monkeypatch.setattr(langchain_module, name, lambda _name=name, **kwargs: clients.append(_name), raising=False)
    handler = langchain_module.LangChainOpenAIHandler()
    await handler._create_chat_async()
    budget = make_budget(handler)

    assert clients == ["AzureChatOpenAI" if api_type else "ChatOpenAI"]
    assert budget.context_window == 1_050_000
    assert budget.input_token_limit(100_000) == 922_000
    assert config.litellm.custom_llm_provider == "ollama"


@pytest.mark.parametrize(("api_type", "api_base", "expected"), [
    ("", "https://gateway.example/v1", 2048),
    ("", None, 1_050_000),
    ("", "", 1_050_000),
    ("", "https://api.openai.com/v1", 1_050_000),
    ("", "https://api.openai.com:443/v1", 1_050_000),
    ("", "https://API.OPENAI.COM/v1", 1_050_000),
    ("azure", "https://azure.example", 1_050_000),
])
def test_custom_gateway_limit_and_native_precedence(settings, api_type, api_base, expected):
    config, values = settings
    config.config.custom_model_max_tokens = 2048
    values.update({"OPENAI.API_TYPE": api_type, "OPENAI.API_BASE": api_base})
    budget = make_budget(langchain_module.LangChainOpenAIHandler())

    assert budget.context_window == expected
    assert budget.max_input_tokens == 922_000


@pytest.mark.parametrize(("name", "value", "expected"), [
    ("OPENAI_API_BASE", "https://gateway.example/v1", 2048),
    ("OPENAI_API_BASE", "", 1_050_000),
    ("OPENAI_BASE_URL", "https://gateway.example/v1", 2048),
    ("LANGSMITH_GATEWAY", "TrUe", 2048),
    ("LANGSMITH_GATEWAY", "NO", 1_050_000),
])
async def test_environment_routes_keep_gateway_caps_without_overriding_client(
    monkeypatch, settings, name, value, expected,
):
    config, values = settings
    config.config.custom_model_max_tokens = 2048
    values["OPENAI.API_BASE"] = None
    monkeypatch.setenv(name, value)
    client_kwargs = []
    monkeypatch.setattr(langchain_module, "ChatOpenAI", lambda **kwargs: client_kwargs.append(kwargs), raising=False)
    handler = langchain_module.LangChainOpenAIHandler()
    await handler._create_chat_async()

    assert client_kwargs == [{"openai_api_key": "test-key"}]
    assert make_budget(handler).context_window == expected
    values["OPENAI.API_BASE"] = "https://api.openai.com/v1"
    assert make_budget(handler).context_window == 1_050_000


def test_global_cap_and_rebound_tokenizer_keep_handler_policy(settings):
    config, _ = settings
    config.config.max_model_tokens = 32_000
    budget = make_budget(langchain_module.LangChainOpenAIHandler())
    rebound = budget_module.AttemptTokenBudget.for_attempt("gpt-6-luna", budget.token_handler)

    assert rebound.context_window == 32_000
    assert rebound.max_input_tokens == 922_000
    assert rebound.input_token_limit(1000) == 31_000


def test_help_ignores_global_cap_but_keeps_native_input_ceiling(settings):
    config, _ = settings
    config.config.max_model_tokens = 32_000
    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.ai_handler = langchain_module.LangChainOpenAIHandler()
    tool.ai_handler.get_output_token_reserve = lambda model, default: 100_000

    assert tool._get_prompt_budget("gpt-6-sol") == 922_000
