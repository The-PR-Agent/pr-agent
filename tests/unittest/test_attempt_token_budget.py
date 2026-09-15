from dataclasses import replace
from types import SimpleNamespace

import pytest

import pr_agent.algo.token_budget as token_budget_module
import pr_agent.algo.token_handler as token_handler_module


class FakeTokenHandler:
    def __init__(self, prompt_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.counted = []

    def count_tokens(self, text):
        self.counted.append(text)
        return len(text)


@pytest.fixture
def token_settings(monkeypatch):
    settings = SimpleNamespace(
        config=SimpleNamespace(model="primary-model"),
        get=lambda key, default=None: {
            "config.model_token_count_estimate_factor": 0,
            "openai.key": "test-key",
            "anthropic.key": None,
        }.get(key, default),
    )
    monkeypatch.setattr(token_handler_module, "get_settings", lambda use_context=True: settings)
    monkeypatch.setattr(
        token_handler_module.TokenEncoder,
        "get_token_encoder",
        lambda model=None: SimpleNamespace(
            model=model,
            encode=lambda text, disallowed_special=(): list(text),
        ),
    )
    return settings


def test_for_attempt_uses_requested_window_and_preserves_fake_handler(monkeypatch):
    calls = []
    fake_handler = FakeTokenHandler(prompt_tokens=17)

    def get_window(model, ignore_max_model_tokens=False):
        calls.append((model, ignore_max_model_tokens))
        return 12_345

    monkeypatch.setattr(token_budget_module, "get_max_tokens", get_window)

    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "fallback-model",
        fake_handler,
        ignore_max_model_tokens=True,
    )

    assert budget.model == "fallback-model"
    assert budget.source_token_handler is fake_handler
    assert budget.token_handler is fake_handler
    assert budget.context_window == 12_345
    assert budget.prompt_tokens == 17
    assert calls == [("fallback-model", True)]


def test_for_attempt_binds_real_handler_without_mutating_source(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    variables = {"title": "PR"}
    source = token_handler_module.TokenHandler(object(), variables, "system {{ title }}", "user")

    budget = token_budget_module.AttemptTokenBudget.for_attempt("fallback-model", source)

    assert source.model == "primary-model"
    assert budget.source_token_handler is source
    assert budget.token_handler is not source
    assert budget.token_handler.model == "fallback-model"
    assert budget.token_handler.vars is variables
    assert budget.prompt_tokens == len("system PR") + len("user")


def test_reserves_are_resolved_independently_for_each_default(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    calls = []

    def reserve(model, default):
        calls.append((model, default))
        return default + 500

    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "openrouter/model",
        FakeTokenHandler(),
        output_token_reserve=reserve,
    )

    assert budget.resolve_output_reserve(1_500, preserve_minimum=True) == 2_000
    assert budget.resolve_output_reserve(1_000, preserve_minimum=True) == 1_500
    assert calls == [("openrouter/model", 1_500), ("openrouter/model", 1_000)]


@pytest.mark.parametrize(
    ("reported", "preserve_minimum", "expected"),
    [
        (100, False, 100),
        (100, True, 1_500),
        (1_200, True, 1_500),
        (1_500, True, 1_500),
        (5_000, True, 5_000),
    ],
)
def test_reserve_floor_is_an_explicit_consumer_policy(
    monkeypatch, reported, preserve_minimum, expected
):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        FakeTokenHandler(),
        output_token_reserve=lambda _model, _default: reported,
    )

    assert budget.resolve_output_reserve(
        1_500,
        preserve_minimum=preserve_minimum,
    ) == expected


def test_available_tokens_subtracts_prompt_once_and_clamps(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 1_000)
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        FakeTokenHandler(prompt_tokens=100),
        output_token_reserve=lambda _model, _default: 200,
    )

    assert budget.available_tokens(1_500) == 700
    assert budget.available_tokens(1_500, prompt_tokens=0) == 800
    assert budget.available_tokens(1_500, prompt_tokens=900) == 0


@pytest.mark.parametrize(
    ("window", "reserve", "prompt", "expected"),
    [(1_000, 1_200, 0, -200), (1_000, 900, 200, -100), (1_000, 900, 100, 0), (1_000, 900, 99, 1)],
)
def test_raw_capacity_distinguishes_exhausted_from_exact_boundary(window, reserve, prompt, expected):
    handler = FakeTokenHandler(prompt_tokens=prompt)
    budget = token_budget_module.AttemptTokenBudget(
        "model", handler, handler, window, output_token_reserve=lambda model, default: reserve,
    )

    assert budget.available_tokens(1_500, clamp=False) == expected
    assert budget.available_tokens(1_500) == max(expected, 0)


def test_frozen_budget_can_refresh_callback_without_rebinding(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        source,
        output_token_reserve=lambda _model, _default: 1_500,
    )

    refreshed = replace(
        budget,
        output_token_reserve=lambda _model, _default: 5_000,
    )

    assert refreshed is not budget
    assert refreshed.source_token_handler is source
    assert refreshed.token_handler is budget.token_handler
    assert refreshed.resolve_output_reserve(1_500) == 5_000


def test_count_tokens_delegates_without_breaking_simple_fakes(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget.for_attempt("model", source)

    assert budget.count_tokens("abcd") == 4
    assert source.counted == ["abcd"]


def test_matches_accepts_source_or_own_bound_handler_only(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = token_handler_module.TokenHandler(object(), {}, "system", "user")
    budget = token_budget_module.AttemptTokenBudget.for_attempt("fallback-model", source)
    equivalent_handler = source.for_model("fallback-model")

    assert budget.matches("fallback-model", source)
    assert budget.matches("fallback-model", budget.token_handler)
    assert not budget.matches("different-model", source)
    assert not budget.matches("fallback-model", equivalent_handler)
    assert not budget.matches("fallback-model", object())
