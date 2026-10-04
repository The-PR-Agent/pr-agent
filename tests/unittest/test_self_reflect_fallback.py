"""
Tests that self-reflection walks the reasoning-model chain: a model that returns
nothing advances to the next one instead of degrading every suggestion to score 7.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import (
    _REFLECTION_FAILURE_SCORE_WHY,
    PRCodeSuggestions,
    apply_reflection_failure_score,
    filter_suggestions_by_score_threshold,
)
from tests.unittest._settings_helpers import _remove_key, restore_settings, snapshot_settings


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
    async def test_routed_primary_reflects_on_itself_before_the_fallbacks(
            self, settings_no_reasoning_model):
        # [model_routing] can put a model from outside the configured chain in front of the same
        # fallbacks. Reflection then starts on that model, not on the config.model it replaced.
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.side_effect = ["", "reflection"]
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "routed-model")

        assert result == "reflection"
        assert [call.kwargs["model"] for call in reflect.call_args_list] == [
            "routed-model", "fallback-model"]

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


_SUGGESTION_RESPONSE = """
code_suggestions:
- one_sentence_summary: "Avoid duplicated work"
  label: maintainability
  relevant_file: app.py
  relevant_lines_start: 1
  relevant_lines_end: 1
  suggestion_content: "Use the shared helper."
  existing_code: "old()"
  improved_code: "new()"
"""


def _prediction_tool():
    """Build a /improve tool whose self-reflection always fails, with the token budget stubbed out."""
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.ai_handler = MagicMock()
    tool.ai_handler.chat_completion = AsyncMock(return_value=(_SUGGESTION_RESPONSE, "stop"))
    tool.ai_handler.get_output_token_reserve = MagicMock(return_value=0)
    tool.vars = {"diff": "", "diff_no_line_numbers": ""}
    tool.pr_code_suggestions_prompt_system = "system"
    tool.pr_code_suggestions_prompt_user = "user"
    tool._suggestion_attempt_budget = SimpleNamespace(
        model="primary-model",
        fit_optional_text=MagicMock(return_value=SimpleNamespace(
            optional_text="complete diff",
            system_prompt="system",
            user_prompt="user",
        )),
    )
    tool._self_reflect_with_fallback = AsyncMock(return_value="")
    return tool


class TestReflectionFailureScore:
    """The score assigned when no model could vet a suggestion must be visible and configurable."""

    @pytest.mark.asyncio
    async def test_default_score_and_explanation_when_reflection_fails(self):
        snapshot = snapshot_settings(("pr_code_suggestions.score_on_reflection_failure",))
        try:
            # Drop the configured value so the resolver's built-in default is exercised.
            _remove_key(get_settings(), "pr_code_suggestions.score_on_reflection_failure")
            tool = _prediction_tool()
            data = await tool._get_prediction("primary-model", "numbered diff", "complete diff")
        finally:
            restore_settings(snapshot)

        suggestion = data["code_suggestions"][0]
        assert suggestion["score"] == 7
        assert suggestion["score_why"] == _REFLECTION_FAILURE_SCORE_WHY

    @pytest.mark.asyncio
    async def test_configured_score_is_used_when_reflection_fails(self):
        snapshot = snapshot_settings(("pr_code_suggestions.score_on_reflection_failure",))
        try:
            get_settings().set("pr_code_suggestions.score_on_reflection_failure", 3)
            tool = _prediction_tool()
            data = await tool._get_prediction("primary-model", "numbered diff", "complete diff")
        finally:
            restore_settings(snapshot)

        suggestion = data["code_suggestions"][0]
        assert suggestion["score"] == 3
        assert suggestion["score_why"] == _REFLECTION_FAILURE_SCORE_WHY

    def test_unvetted_suggestions_below_the_threshold_are_filtered_out(self):
        snapshot = snapshot_settings((
            "pr_code_suggestions.score_on_reflection_failure",
            "pr_code_suggestions.suggestions_score_threshold",
        ))
        try:
            get_settings().set("pr_code_suggestions.score_on_reflection_failure", 5)
            get_settings().set("pr_code_suggestions.suggestions_score_threshold", 8)
            suggestions = [{"one_sentence_summary": "Avoid duplicated work"}]
            apply_reflection_failure_score(suggestions)
            kept = filter_suggestions_by_score_threshold(suggestions)
        finally:
            restore_settings(snapshot)

        assert suggestions[0]["score"] == 5
        assert suggestions[0]["score_why"] == _REFLECTION_FAILURE_SCORE_WHY
        assert kept == []
