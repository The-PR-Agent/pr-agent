"""Unit tests for LiteLLMAIHandler._litellm_supports_temperature.

The gate that forwards config.temperature mirrors the reasoning_effort gate:
support is derived from litellm's parameter metadata, probed over every suffix
of the model id, with config.no_temperature_models as the operator override
(tested in test_litellm_chat_completion_core.py). These tests pin the probe
itself: which candidates are consulted and what the supported-parameter list
must contain.
"""
import litellm

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def _patch_supported_params(monkeypatch, params):
    monkeypatch.setattr(litellm, "get_supported_openai_params", lambda model, custom_llm_provider=None: params)


def test_temperature_in_supported_params_returns_true(monkeypatch):
    _patch_supported_params(monkeypatch, ["temperature", "max_tokens"])
    assert LiteLLMAIHandler._litellm_supports_temperature("gpt-4o") is True


def test_temperature_absent_returns_false(monkeypatch):
    _patch_supported_params(monkeypatch, ["max_tokens", "top_p"])
    assert LiteLLMAIHandler._litellm_supports_temperature("gpt-4o") is False


def test_empty_params_return_false(monkeypatch):
    _patch_supported_params(monkeypatch, [])
    assert LiteLLMAIHandler._litellm_supports_temperature("gpt-4o") is False


def test_unknown_model_returns_false(monkeypatch):
    def _raise(_model, custom_llm_provider=None):
        raise ValueError("unknown model")

    monkeypatch.setattr(litellm, "get_supported_openai_params", _raise)
    assert LiteLLMAIHandler._litellm_supports_temperature("openai/unknown-endpoint-model") is False


def test_openrouter_suffix_candidates_resolve(monkeypatch):
    """The leading openrouter/ segment is stripped and every suffix is probed,
    mirroring the reasoning gate."""
    seen = []

    def _record(model, custom_llm_provider=None):
        seen.append(model)
        return ["temperature"] if model == "xai/o3-mini" else []

    monkeypatch.setattr(litellm, "get_supported_openai_params", _record)

    assert LiteLLMAIHandler._litellm_supports_temperature("openrouter/anthropic/o3-mini") is True
    assert seen == ["anthropic/o3-mini", "o3-mini", "xai/o3-mini"]


def test_later_candidate_wins_after_early_raises(monkeypatch):
    def _flaky(model, custom_llm_provider=None):
        if model == "deepseek/deepseek-reasoner":
            raise ValueError("provider resolution failed")
        return ["temperature"]

    monkeypatch.setattr(litellm, "get_supported_openai_params", _flaky)

    assert LiteLLMAIHandler._litellm_supports_temperature("deepseek/deepseek-reasoner") is True


def test_custom_provider_forwarded(monkeypatch):
    kwargs = {}

    def _capture(model, custom_llm_provider=None):
        kwargs["custom_llm_provider"] = custom_llm_provider
        return ["temperature"]

    monkeypatch.setattr(litellm, "get_supported_openai_params", _capture)

    assert LiteLLMAIHandler._litellm_supports_temperature("grok-4.6", custom_llm_provider="openai") is True
    assert kwargs["custom_llm_provider"] == "openai"
