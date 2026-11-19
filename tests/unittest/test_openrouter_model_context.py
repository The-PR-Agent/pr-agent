"""Prepare arbitrary OpenRouter model budgets without adding model-specific rows."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import litellm
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as handlers
import pr_agent.algo.token_budget as budgets
import pr_agent.algo.token_handler as tokens
import pr_agent.commands.pr_help_message as help_message

MODEL = "openrouter/example/new-model"
BASE = "https://openrouter.ai/api/v1"


@pytest.fixture
def configured(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "OR_API_KEY", "OPENROUTER_API_BASE", "OPENAI_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    values = {"OPENROUTER.KEY": "synthetic-key"}

    def get(key, default=None):
        if key.lower() == "openrouter":
            return {"key": values.get("OPENROUTER.KEY"), "api_base": values.get("OPENROUTER.API_BASE")}
        return values.get(key.upper(), default)

    settings = SimpleNamespace(
        config=SimpleNamespace(model=MODEL, custom_model_max_tokens=0, max_model_tokens=32000,
                               ai_timeout=30, custom_reasoning_model=False, reasoning_effort=None,
                               verbosity_level=0, seed=-1, get=lambda key, default=None: default),
        litellm=SimpleNamespace(custom_llm_provider="", get=lambda key, default=None: default),
        get=get,
    )
    for module in (handlers, budgets, tokens, help_message):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(tokens.TokenEncoder, "get_token_encoder", lambda model=None: SimpleNamespace(
        encode=lambda text, **kwargs: list(text),
    ))
    lookup = create_autospec(litellm.get_model_info, side_effect=ValueError("synthetic metadata miss"))
    monkeypatch.setattr(litellm, "get_model_info", lookup)
    fetch = AsyncMock(return_value=100000)
    monkeypatch.setattr(handlers, "fetch_model_context", fetch)
    return settings, values, lookup, fetch


@pytest.mark.parametrize("provider,model", [("", MODEL), ("openrouter", "example/new-model")])
async def test_prepare_deduplicates_and_sync_budget_never_requeries_sdk(configured, provider, model):
    settings, _, lookup, fetch = configured
    settings.litellm.custom_llm_provider = provider
    handler = handlers.LiteLLMAIHandler()
    await asyncio.gather(*(budgets.prepare_model_token_limits(handler, model) for _ in range(3)))
    lookup.assert_called_once_with(model, custom_llm_provider="openrouter", api_base=BASE, api_key="synthetic-key")
    fetch.assert_awaited_once_with("example/new-model")
    lookup.reset_mock()
    assert handler.get_model_token_limits(model) == (32000, None)
    assert handler.get_model_token_limits(model, ignore_max_model_tokens=True) == (100000, None)
    budget = budgets.AttemptTokenBudget.for_prompt_attempt(model, None, {}, "system", "user", ai_handler=handler)
    assert budget.input_token_limit(2000) == 30000
    lookup.assert_not_called()


async def test_new_model_and_new_handler_have_separate_metadata(configured):
    _, _, lookup, fetch = configured
    other_model = "openrouter/example/next-release"
    fetch.side_effect = [100000, 20000, 50000]
    first = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(first, MODEL)
    await budgets.prepare_model_token_limits(first, other_model)
    second = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(second, MODEL)
    assert first.get_model_token_limits(other_model, ignore_max_model_tokens=True) == (20000, None)
    assert first.get_model_token_limits(MODEL, ignore_max_model_tokens=True) == (100000, None)
    assert second.get_model_token_limits(MODEL, ignore_max_model_tokens=True) == (50000, None)
    assert lookup.call_count == fetch.await_count == 3


async def test_sdk_success_keeps_its_limit_without_public_lookup(configured):
    _, _, lookup, fetch = configured
    caller_thread = threading.get_ident()

    def sdk_info(*args, **kwargs):
        assert threading.get_ident() != caller_thread
        return {"max_input_tokens": 16000}

    lookup.side_effect = sdk_info
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler.get_model_token_limits(MODEL) == (16000, None)
    fetch.assert_not_awaited()


async def test_sdk_alias_lookup_sequence_precedes_public_discovery(configured):
    settings, _, lookup, fetch = configured
    settings.litellm.custom_llm_provider = "openrouter"
    model = "openai/gpt-5.123_thinking"

    def sdk_info(lookup_model, **kwargs):
        if lookup_model == "gpt-5.123":
            return {"max_input_tokens": 16000}
        raise ValueError("synthetic metadata miss")

    lookup.side_effect = sdk_info
    assert budgets.get_max_tokens(model) == 16000
    fetch.return_value = None
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, model.removesuffix("_thinking"))
    fetch.assert_awaited_once_with("openai/gpt-5.123")
    lookup.reset_mock()
    fetch.reset_mock()
    await budgets.prepare_model_token_limits(handler, model)
    assert handler.get_model_token_limits(model) == (16000, None)
    with pytest.raises(Exception, match="Unable to resolve a positive input-token limit"):
        handler.get_model_token_limits(model.removesuffix("_thinking"))
    assert [call.args[0] for call in lookup.call_args_list] == ["openai/gpt-5.123", "gpt-5.123"]
    for call in lookup.call_args_list:
        assert call.kwargs == {"custom_llm_provider": "openrouter", "api_base": BASE, "api_key": "synthetic-key"}
    fetch.assert_not_awaited()


async def test_help_budget_consumes_discovered_context_without_requery(configured):
    _, _, lookup, _ = configured
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, MODEL)
    command = help_message.PRHelpMessage.__new__(help_message.PRHelpMessage)
    command.ai_handler = handler
    handler.get_output_token_reserve = lambda model, default: 2000
    assert command._get_prompt_budget(MODEL) == 98000
    assert lookup.call_count == 1


async def test_preparation_deadline_remains_fallback_eligible(configured, monkeypatch):
    _, _, _, fetch = configured

    async def stalled_sdk(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(handlers.asyncio, "to_thread", stalled_sdk)
    monkeypatch.setattr(handlers, "MODEL_CONTEXT_TIMEOUT", 0.01)
    handler = handlers.LiteLLMAIHandler()
    with pytest.raises(TimeoutError):
        await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler._model_context_cache == {}
    fetch.assert_not_awaited()


@pytest.mark.parametrize("model,custom", [(MODEL, 12000), ("openrouter/openai/gpt-6.1-sol", 0)])
async def test_existing_limit_priorities_need_no_metadata(configured, model, custom):
    settings, _, lookup, fetch = configured
    settings.config.custom_model_max_tokens = custom
    expected = budgets.get_max_tokens(model)
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, model)
    assert handler.get_model_token_limits(model)[0] == expected
    lookup.assert_not_called()
    fetch.assert_not_awaited()


@pytest.mark.parametrize("key_source", ["settings", "environment"])
async def test_captured_route_survives_late_settings_changes(configured, monkeypatch, key_source):
    _, values, lookup, fetch = configured
    if key_source == "environment":
        values.pop("OPENROUTER.KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    handler = handlers.LiteLLMAIHandler()
    values.update({"OPENROUTER.KEY": "later-key", "OPENROUTER.API_BASE": "https://gateway.example/v1"})
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://gateway.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "later-key")
    await budgets.prepare_model_token_limits(handler, MODEL)
    lookup.assert_called_once_with(MODEL, custom_llm_provider="openrouter", api_base=BASE, api_key="synthetic-key")
    fetch.assert_awaited_once_with("example/new-model")


@pytest.mark.parametrize("route_settings,provider", [
    pytest.param({"OPENROUTER.KEY": "synthetic-key", "OPENROUTER.API_BASE": "https://gateway.example/v1"}, "",
                 id="custom-gateway"),
    pytest.param({"OPENROUTER.KEY": "synthetic-key", "OPENROUTER.API_BASE": BASE}, "openai",
                 id="other-provider"),
    pytest.param({"OPENAI.KEY": "synthetic-key", "OPENAI.API_BASE": BASE}, "", id="uncaptured-openai-route"),
])
async def test_other_routes_keep_legacy_lookup(configured, route_settings, provider):
    settings, values, lookup, fetch = configured
    settings.litellm.custom_llm_provider = provider
    values.clear()
    values.update(route_settings)
    lookup.side_effect = None
    lookup.return_value = {"max_input_tokens": 16000}
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler.get_model_token_limits(MODEL) == (16000, None)
    lookup.assert_called_once_with(MODEL)
    fetch.assert_not_awaited()


async def test_failed_lookup_is_memoized_not_replaced_by_global_cap(configured):
    settings, _, lookup, fetch = configured
    fetch.return_value = None
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, MODEL)
    await budgets.prepare_model_token_limits(handler, MODEL)
    with pytest.raises(Exception, match="Unable to resolve a positive input-token limit"):
        handler.get_model_token_limits(MODEL)
    settings.config.custom_model_max_tokens = 12000
    assert handler.get_model_token_limits(MODEL) == (12000, None)
    assert lookup.call_count == fetch.await_count == 1


async def test_transient_failure_is_not_memoized_and_can_recover(configured):
    _, _, lookup, fetch = configured
    fetch.side_effect = [budgets.FallbackEligibleError("synthetic transport failure"), 100000]
    handler = handlers.LiteLLMAIHandler()
    with pytest.raises(budgets.FallbackEligibleError):
        await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler._model_context_cache == {}
    await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler.get_model_token_limits(MODEL) == (32000, None)
    assert lookup.call_count == fetch.await_count == 2


async def _wait_for_fetch(started, *tasks):
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        pytest.fail("The public metadata lookup did not start")


async def test_concurrent_waiters_share_timeout_but_a_later_call_can_retry(configured, monkeypatch):
    _, _, lookup, fetch = configured
    started = asyncio.Event()

    async def sdk(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def stalled_metadata(_):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(handlers.asyncio, "to_thread", sdk)
    monkeypatch.setattr(handlers, "MODEL_CONTEXT_TIMEOUT", 0.01)
    fetch.side_effect = stalled_metadata
    handler = handlers.LiteLLMAIHandler()
    waiters = [asyncio.create_task(budgets.prepare_model_token_limits(handler, MODEL)) for _ in range(3)]
    await _wait_for_fetch(started, *waiters)
    outcomes = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(error, TimeoutError) for error in outcomes)
    assert lookup.call_count == fetch.await_count == 1
    assert handler._model_context_cache == {}
    fetch.side_effect = None
    await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler.get_model_token_limits(MODEL) == (32000, None)
    assert lookup.call_count == fetch.await_count == 2


async def test_custom_limit_can_override_prepared_metadata(configured):
    settings, _, _, _ = configured
    handler = handlers.LiteLLMAIHandler()
    await budgets.prepare_model_token_limits(handler, MODEL)
    settings.config.custom_model_max_tokens = 12000
    assert handler.get_model_token_limits(MODEL) == (12000, None)


def test_unprepared_callers_retain_the_existing_sdk_path(configured):
    _, _, lookup, fetch = configured
    lookup.side_effect = None
    lookup.return_value = {"max_input_tokens": 16000}
    assert handlers.LiteLLMAIHandler().get_model_token_limits(MODEL) == (16000, None)
    lookup.assert_called_once_with(MODEL)
    fetch.assert_not_awaited()


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_cancellation_does_not_poison_other_waiters_or_the_cache(configured, cancel_owner):
    _, _, _, fetch = configured
    started = asyncio.Event()
    release = asyncio.Event()

    async def metadata(_):
        started.set()
        await release.wait()
        return 100000

    fetch.side_effect = metadata
    handler = handlers.LiteLLMAIHandler()
    owner = asyncio.create_task(budgets.prepare_model_token_limits(handler, MODEL))
    await _wait_for_fetch(started, owner)
    waiter = asyncio.create_task(budgets.prepare_model_token_limits(handler, MODEL))
    await asyncio.sleep(0)
    cancelled, survivor = (owner, waiter) if cancel_owner else (waiter, owner)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled, timeout=5)
    release.set()
    await asyncio.wait_for(survivor, timeout=5)
    await budgets.prepare_model_token_limits(handler, MODEL)
    assert handler.get_model_token_limits(MODEL, ignore_max_model_tokens=True) == (100000, None)
    assert fetch.await_count == (2 if cancel_owner else 1)
