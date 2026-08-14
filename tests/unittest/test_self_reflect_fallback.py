"""
Tests that self-reflection uses the reasoning-model fallback chain: a model that
returns nothing advances to the next model instead of degrading to score 7.
"""
from unittest.mock import AsyncMock, patch

import pytest

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _Settings:
    """Minimal settings object exposing only what the reflection path reads."""

    class config:
        model = "primary-model"
        model_reasoning = "reasoning-model"
        fallback_models = ["fallback-model"]

    def get(self, key, default=None):
        return {
            "config.model_weak": None,
            "config.model_reasoning": "reasoning-model",
            "openai.deployment_id": None,
            "openai.fallback_deployments": [],
        }.get(key, default)

    def set(self, key, value):
        pass


@pytest.fixture
def settings(monkeypatch):
    stub = _Settings()
    for module in ("pr_agent.tools.pr_code_suggestions",
                   "pr_agent.algo.pr_processing",
                   "pr_agent.algo.utils"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: stub)
    return stub


def _tool():
    return PRCodeSuggestions.__new__(PRCodeSuggestions)


class TestSelfReflectFallback:

    @pytest.mark.asyncio
    async def test_empty_response_advances_to_next_model(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.side_effect = ["", "reflection from fallback"]
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff")

        assert result == "reflection from fallback"
        assert [call.kwargs["model"] for call in reflect.call_args_list] == [
            "reasoning-model", "fallback-model"]

    @pytest.mark.asyncio
    async def test_reasoning_model_is_tried_first(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = "reflection"
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff")

        assert result == "reflection"
        reflect.assert_awaited_once()
        assert reflect.call_args.kwargs["model"] == "reasoning-model"

    @pytest.mark.asyncio
    async def test_all_models_failing_degrades_quietly(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = ""
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff")

        assert result == ""
        assert reflect.await_count == 2

    @pytest.mark.asyncio
    async def test_no_suggestions_skips_all_model_calls(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            result = await tool._self_reflect_with_fallback([], "diff")

        assert result == ""
        reflect.assert_not_awaited()
