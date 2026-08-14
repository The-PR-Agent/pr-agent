"""
Tests that self-reflection walks the reasoning-model chain: a model that returns
nothing advances to the next one instead of degrading every suggestion to score 7.
"""
from unittest.mock import AsyncMock, patch

import pytest

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _Settings:
    """Minimal settings object exposing only what the reflection path reads."""

    def __init__(self, model_reasoning="reasoning-model", fallback_deployments=()):
        self._model_reasoning = model_reasoning
        self._fallback_deployments = fallback_deployments
        self.set_calls = []

        class config:
            model = "primary-model"
            fallback_models = ["fallback-model"]

        config.model_reasoning = model_reasoning
        self.config = config

    def get(self, key, default=None):
        return {
            "config.model_weak": None,
            "config.model_reasoning": self._model_reasoning,
            "openai.deployment_id": None,
            "openai.fallback_deployments": self._fallback_deployments,
        }.get(key, default)

    def set(self, key, value):
        self.set_calls.append((key, value))


def _install(monkeypatch, stub):
    for module in ("pr_agent.tools.pr_code_suggestions",
                   "pr_agent.algo.pr_processing",
                   "pr_agent.algo.utils"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: stub)
    return stub


@pytest.fixture
def settings(monkeypatch):
    return _install(monkeypatch, _Settings())


@pytest.fixture
def settings_no_reasoning_model(monkeypatch):
    return _install(monkeypatch, _Settings(model_reasoning=None))


@pytest.fixture
def settings_pinned_deployments(monkeypatch):
    return _install(monkeypatch, _Settings(fallback_deployments=["fallback-deployment"]))


def _tool():
    return PRCodeSuggestions.__new__(PRCodeSuggestions)


class TestSelfReflectFallback:

    @pytest.mark.asyncio
    async def test_empty_response_advances_to_next_model(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.side_effect = ["", "reflection from fallback"]
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == "reflection from fallback"
        assert [call.kwargs["model"] for call in reflect.call_args_list] == [
            "reasoning-model", "fallback-model"]

    @pytest.mark.asyncio
    async def test_reasoning_model_is_tried_first(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = "reflection"
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == "reflection"
        reflect.assert_awaited_once()
        assert reflect.call_args.kwargs["model"] == "reasoning-model"

    @pytest.mark.asyncio
    async def test_all_models_failing_degrades_quietly(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = ""
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == ""
        assert reflect.await_count == 2

    @pytest.mark.asyncio
    async def test_no_suggestions_skips_all_model_calls(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            result = await tool._self_reflect_with_fallback([], "diff", "primary-model")

        assert result == ""
        reflect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_models_already_burned_by_the_outer_loop_are_skipped(
            self, settings_no_reasoning_model):
        # With no dedicated reasoning model the reasoning chain is the regular chain. If the
        # outer fallback loop already failed over to "fallback-model", reflection must not
        # start again on the dead "primary-model".
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = "reflection"
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "fallback-model")

        assert result == "reflection"
        reflect.assert_awaited_once()
        assert reflect.call_args.kwargs["model"] == "fallback-model"

    @pytest.mark.asyncio
    async def test_pinned_deployments_do_not_retry_other_models(self, settings_pinned_deployments):
        # With fallback_deployments configured each model lives on its own deployment, and
        # openai.deployment_id is global. Retrying the next model here would send it to the
        # current model's deployment, so reflection stops after one attempt.
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = ""
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == ""
        reflect.assert_awaited_once()
        assert reflect.call_args.kwargs["model"] == "reasoning-model"

    @pytest.mark.asyncio
    async def test_reflection_does_not_mutate_the_global_deployment_id(self, settings):
        # retry_with_fallback_models sets openai.deployment_id without restoring it. Reflection
        # runs inside a chunk call, so mutating it here would leak into the run's remaining
        # chunks and race them (parallel_calls is on by default).
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.side_effect = ["", "reflection from fallback"]
            await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff", "primary-model")

        assert not [key for key, _ in settings.set_calls if key == "openai.deployment_id"]
