"""
Verify that self-reflection advances when feedback is unusable, preserving
suggestions and the existing fallback policy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import (
    _REFLECTION_FAILURE_SCORE_WHY,
    PRCodeSuggestions,
    apply_reflection_failure_score,
    filter_suggestions_by_score_threshold,
)
from tests.unittest._settings_helpers import _remove_key, restore_settings, snapshot_settings

_REFLECTION_FEEDBACK = {"code_suggestions": [{
    "suggestion_score": 9,
    "why": "Use the shared helper.",
    "relevant_lines_start": 1,
    "relevant_lines_end": 1,
}]}
_REFLECTION_RESPONSE = yaml.safe_dump(_REFLECTION_FEEDBACK)


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


def _keep_suggestion(suggestion):
    return suggestion


class TestSelfReflectFallback:

    @pytest.mark.asyncio
    async def test_empty_response_advances_to_next_model(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.side_effect = ["", _REFLECTION_RESPONSE]
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == _REFLECTION_FEEDBACK
        assert [call.kwargs["model"] for call in reflect.call_args_list] == [
            "reasoning-model", "fallback-model"]

    @pytest.mark.asyncio
    async def test_reasoning_model_is_tried_first(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = _REFLECTION_RESPONSE
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == _REFLECTION_FEEDBACK
        reflect.assert_awaited_once()
        assert reflect.call_args.kwargs["model"] == "reasoning-model"

    @pytest.mark.parametrize("response", ["", "code_suggestions: []"])
    @pytest.mark.asyncio
    async def test_all_models_failing_degrades_quietly(self, settings, response):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            reflect.return_value = response
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "primary-model")

        assert result == {}
        assert reflect.await_count == 2

    @pytest.mark.asyncio
    async def test_no_suggestions_skips_all_model_calls(self, settings):
        tool = _tool()
        with patch.object(PRCodeSuggestions, "self_reflect_on_suggestions",
                          new_callable=AsyncMock) as reflect:
            result = await tool._self_reflect_with_fallback([], "diff", "primary-model")

        assert result == {}
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
            reflect.return_value = _REFLECTION_RESPONSE
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "fallback-model")

        assert result == _REFLECTION_FEEDBACK
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
            reflect.side_effect = ["", _REFLECTION_RESPONSE]
            result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff",
                                                            "routed-model")

        assert result == _REFLECTION_FEEDBACK
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

        assert result == {}
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
            reflect.side_effect = ["", _REFLECTION_RESPONSE]
            await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff", "primary-model")

        assert not [key for key, _ in settings.set_calls if key == "openai.deployment_id"]

    @pytest.mark.parametrize("response", [
        "No suggestions require changes.",
        "code_suggestions: null",
        "code_suggestions: []",
        "code_suggestions: [null]",
        "code_suggestions: [null, null]",
        "code_suggestions: [{suggestion_score: invalid, why: reason}]",
        "code_suggestions: [{suggestion_score: -1, why: reason}]",
        "code_suggestions: [{suggestion_score: 11, why: reason, relevant_lines_start: 1, relevant_lines_end: 1}]",
        "code_suggestions: [{suggestion_score: true, why: reason, relevant_lines_start: 1, relevant_lines_end: 1}]",
        "code_suggestions: [{suggestion_score: 9.5, why: reason, relevant_lines_start: 1, relevant_lines_end: 1}]",
        "code_suggestions: [{suggestion_score: 9}]",
        "code_suggestions: [{suggestion_score: 9, why: reason}]",
        "code_suggestions: [{suggestion_score: 9, why: reason, relevant_lines_start: bad, relevant_lines_end: 1}]",
        "code_suggestions: [{suggestion_score: 9, why: reason, relevant_lines_start: -1, relevant_lines_end: -1}]",
        "code_suggestions: [{suggestion_score: 9, why: reason, relevant_lines_start: 2, relevant_lines_end: 1}]",
    ])
    @pytest.mark.asyncio
    async def test_unusable_feedback_advances_to_next_model(self, settings, response):
        tool = _tool()
        tool.self_reflect_on_suggestions = AsyncMock(side_effect=[response, _REFLECTION_RESPONSE])

        result = await tool._self_reflect_with_fallback([{"suggestion": "a"}], "diff", "primary-model")

        assert [call.kwargs["model"] for call in tool.self_reflect_on_suggestions.call_args_list] == [
            "reasoning-model", "fallback-model"]
        assert result == _REFLECTION_FEEDBACK

    @pytest.mark.parametrize("feedback", [
        [{"suggestion_score": 0, "why": "The suggestion is incorrect."}],
        [None, _REFLECTION_FEEDBACK["code_suggestions"][0]],
    ], ids=["valid-rejection-without-lines", "partly-usable"])
    @pytest.mark.asyncio
    async def test_usable_feedback_does_not_retry(self, settings, feedback):
        tool = _tool()
        expected = {"code_suggestions": feedback}
        tool.self_reflect_on_suggestions = AsyncMock(return_value=yaml.safe_dump(expected))

        result = await tool._self_reflect_with_fallback([{} for _ in feedback], "diff", "primary-model")

        tool.self_reflect_on_suggestions.assert_awaited_once()
        assert result == expected

    @pytest.mark.asyncio
    async def test_normalizes_anchors_for_every_usable_entry(self, settings):
        tool = _tool()
        feedback = {"code_suggestions": [
            {"suggestion_score": 0, "why": "Reject", "relevant_lines_start": "1", "relevant_lines_end": "1"},
            {"suggestion_score": 9, "why": "Fix this", "relevant_lines_start": "2", "relevant_lines_end": "2"},
        ]}
        tool.self_reflect_on_suggestions = AsyncMock(return_value=yaml.safe_dump(feedback))

        result = await tool._self_reflect_with_fallback([{}, {}], "diff", "primary-model")

        tool.self_reflect_on_suggestions.assert_awaited_once()
        assert [entry["relevant_lines_start"] for entry in result["code_suggestions"]] == [1, 2]
        assert [entry["relevant_lines_end"] for entry in result["code_suggestions"]] == [1, 2]

    @pytest.mark.parametrize("invalid_score", [-1, 11, 9.5, True])
    @pytest.mark.asyncio
    async def test_partial_feedback_invalid_scores_use_failure_score(self, settings, invalid_score):
        settings.config.publish_output = False
        tool = _tool()
        tool.validate_one_liner_suggestion_not_repeating_code = _keep_suggestion
        suggestions = [
            {"relevant_lines_start": 1, "relevant_lines_end": 1,
             "label": "maintainability", "existing_code": "old()", "improved_code": "new()"},
            {"relevant_lines_start": 2, "relevant_lines_end": 2,
             "label": "maintainability", "existing_code": "before()", "improved_code": "after()"},
        ]
        feedback = {"code_suggestions": [
            {"suggestion_score": 9, "why": "Useful"},
            {"suggestion_score": invalid_score, "why": "Invalid"},
        ]}
        tool.self_reflect_on_suggestions = AsyncMock(return_value=yaml.safe_dump(feedback))

        response = await tool._self_reflect_with_fallback(suggestions, "diff", "primary-model")
        tool.self_reflect_on_suggestions.assert_awaited_once()
        assert response["code_suggestions"][1] is None

        await tool.analyze_self_reflection_response({"code_suggestions": suggestions}, response)
        assert suggestions[0]["score"] == 9
        assert suggestions[1]["score"] == 7
        assert suggestions[1]["score_why"] == _REFLECTION_FAILURE_SCORE_WHY

    @pytest.mark.asyncio
    async def test_partial_feedback_invalid_anchors_use_failure_score(self, settings):
        settings.config.publish_output = False
        tool = _tool()
        tool.validate_one_liner_suggestion_not_repeating_code = _keep_suggestion
        suggestions = [
            {"relevant_lines_start": 1, "relevant_lines_end": 1,
             "label": "maintainability", "existing_code": "old()", "improved_code": "new()"},
            {"label": "maintainability", "existing_code": "before()", "improved_code": "after()"},
        ]
        feedback = {"code_suggestions": [
            {"suggestion_score": 9, "why": "Useful"},
            {"suggestion_score": 9, "why": "Invalid anchors",
             "relevant_lines_start": -1, "relevant_lines_end": -1},
        ]}
        tool.self_reflect_on_suggestions = AsyncMock(return_value=yaml.safe_dump(feedback))

        response = await tool._self_reflect_with_fallback(suggestions, "diff", "primary-model")
        tool.self_reflect_on_suggestions.assert_awaited_once()
        assert response["code_suggestions"][1] is None

        await tool.analyze_self_reflection_response({"code_suggestions": suggestions}, response)
        assert suggestions[0]["score"] == 9
        assert suggestions[1]["score"] == 7
        assert suggestions[1]["score_why"] == _REFLECTION_FAILURE_SCORE_WHY

    @pytest.mark.asyncio
    async def test_numeric_string_score_is_normalized_before_summary_sorting(self):
        tool = _tool()
        tool.git_provider = MagicMock()
        tool.git_provider.diff_files = [
            FilePatchInfo("old()\n", "old()\n", "@@ -1 +1 @@\n-old()\n+old()", "app.py")
        ]
        tool.git_provider.get_line_link.return_value = "https://example.test/app.py#L1"
        tool.validate_one_liner_suggestion_not_repeating_code = _keep_suggestion
        high = {
            "one_sentence_summary": "Higher scored improvement",
            "label": "security",
            "relevant_file": "app.py",
            "relevant_lines_start": 1,
            "relevant_lines_end": 1,
            "suggestion_content": "Improve this code.",
            "existing_code": "old()",
            "improved_code": "new()",
        }
        low = {**high, "one_sentence_summary": "Lower scored improvement", "label": "maintainability"}
        data = {"code_suggestions": [high, low]}
        feedback = {"code_suggestions": [
            {"suggestion_score": "9", "why": "High impact"},
            {"suggestion_score": 8, "why": "Medium impact"},
        ]}

        assert tool._has_usable_reflection_feedback(feedback, data["code_suggestions"])
        assert [entry["suggestion_score"] for entry in feedback["code_suggestions"]] == [9, 8]

        await tool.analyze_self_reflection_response(data, feedback)

        assert [suggestion["score"] for suggestion in data["code_suggestions"]] == [9, 8]
        rendered = tool.generate_summarized_suggestions(data)
        assert "<table>" in rendered
        assert rendered.index("Higher scored improvement") < rendered.index("Lower scored improvement")

    @pytest.mark.parametrize("suggestion", [
        {"relevant_lines_start": 1, "relevant_lines_end": 1},
        {"relevant_lines_start": -1, "relevant_lines_end": -1},
        {"relevant_lines_start": 1},
    ], ids=["valid-original-lines", "invalid-original-lines", "original-start-only"])
    @pytest.mark.asyncio
    async def test_existing_start_keeps_feedback_lines_optional(self, settings, suggestion):
        # Preserve the consumer's choice to ignore feedback lines when a start already exists.
        tool = _tool()
        expected = {"code_suggestions": [{"suggestion_score": 9, "why": "Use the shared helper."}]}
        tool.self_reflect_on_suggestions = AsyncMock(return_value=yaml.safe_dump(expected))

        result = await tool._self_reflect_with_fallback([suggestion], "diff", "primary-model")

        tool.self_reflect_on_suggestions.assert_awaited_once()
        assert result == expected


_SUGGESTION_RESPONSE = """
code_suggestions:
- one_sentence_summary: "Avoid duplicated work"
  label: maintainability
  relevant_file: app.py
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
        render_prompt_templates=MagicMock(return_value=("system", "user")),
        prepare_request=MagicMock(return_value=SimpleNamespace(
            input_tokens=10, system_prompt="system", user_prompt="user"
        )),
        input_token_limit=MagicMock(return_value=100),
    )
    tool._self_reflect_with_fallback = AsyncMock(return_value="")
    return tool


@pytest.mark.parametrize("response, expected_models", [
    ("code_suggestions: []", ["reasoning-model", "fallback-model"]),
    ("code_suggestions: [{suggestion_score: 9, why: reason}]", ["reasoning-model", "fallback-model"]),
    ("code_suggestions: [{suggestion_score: -1, why: reason}]", ["reasoning-model", "fallback-model"]),
    ("code_suggestions: [{suggestion_score: 11, why: reason, relevant_lines_start: 1, "
     "relevant_lines_end: 1}]", ["reasoning-model", "fallback-model"]),
    ('code_suggestions: [{suggestion_score: 9, why: reason, relevant_lines_start: "1", '
     'relevant_lines_end: "1"}]', ["reasoning-model"]),
])
@pytest.mark.asyncio
async def test_prediction_recovers_line_numbers_without_repeating_generation(response, expected_models):
    snapshot = snapshot_settings(("config.model_reasoning", "config.fallback_models", "openai.fallback_deployments"))
    try:
        get_settings().set("config.model_reasoning", "reasoning-model")
        get_settings().set("config.fallback_models", ["fallback-model"])
        get_settings().set("openai.fallback_deployments", [])
        tool = _prediction_tool()
        del tool._self_reflect_with_fallback
        tool.self_reflect_on_suggestions = AsyncMock(side_effect=[response, _REFLECTION_RESPONSE])
        tool.git_provider.diff_files = [
            FilePatchInfo("old()\n", "old()\n", "@@ -1 +1 @@\n-old()\n+old()", "app.py")
        ]

        data = await tool._get_prediction("primary-model", "numbered diff", "complete diff")
    finally:
        restore_settings(snapshot)

    suggestion = data["code_suggestions"][0]
    assert suggestion["score"] == 9
    assert suggestion["relevant_lines_start"] == 1
    assert suggestion["relevant_lines_end"] == 1
    assert tool._is_suggestion_line_range_valid(suggestion)
    tool.ai_handler.chat_completion.assert_awaited_once()
    assert [call.kwargs["model"] for call in tool.self_reflect_on_suggestions.call_args_list] == expected_models


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
