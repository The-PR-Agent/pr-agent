"""A self-reflection outage must be visible, not indistinguishable from a clean run.

Reflection is what scores suggestions. When the whole reasoning chain fails, every suggestion
falls back to a passing score - which then clears the publish threshold - so a total outage used
to look exactly like "all of these suggestions are fine", with the diagnostic log commented out.
The suggestions are still kept (dropping them would lose real findings), but the failure is now
logged and written into each suggestion's score rationale, where the publish body renders it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.tools.pr_code_suggestions import (
    SELF_REFLECTION_FALLBACK_SCORE,
    SELF_REFLECTION_UNAVAILABLE_REASON,
    PRCodeSuggestions,
)


def _make_tool(suggestions):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.vars = {}
    tool.pr_code_suggestions_prompt_system = "system"
    tool.pr_code_suggestions_prompt_user = "user"
    tool.ai_handler = MagicMock()
    tool.ai_handler.chat_completion = AsyncMock(return_value=("response", "stop"))
    tool._prepare_pr_code_suggestions = MagicMock(return_value={"code_suggestions": suggestions})
    return tool


async def _run(tool):
    with patch("pr_agent.tools.pr_code_suggestions.get_settings") as get_settings:
        get_settings.return_value.config.temperature = 0.2
        get_settings.return_value.config.publish_output = True
        return await tool._get_prediction("model", "diff", "diff_no_lines")


@pytest.mark.asyncio
async def test_a_reflection_outage_is_logged_and_marked_on_every_suggestion():
    tool = _make_tool([{"one_sentence_summary": "a"}, {"one_sentence_summary": "b"}])
    tool._self_reflect_with_fallback = AsyncMock(return_value="")

    with patch("pr_agent.tools.pr_code_suggestions.get_logger") as get_logger:
        data = await _run(tool)

    errors = " ".join(str(call) for call in get_logger.return_value.error.call_args_list)
    assert "Could not self-reflect" in errors
    for suggestion in data["code_suggestions"]:
        assert suggestion["score"] == SELF_REFLECTION_FALLBACK_SCORE
        assert suggestion["score_why"] == SELF_REFLECTION_UNAVAILABLE_REASON


@pytest.mark.asyncio
async def test_a_successful_reflection_leaves_no_failure_marker():
    tool = _make_tool([{"one_sentence_summary": "a"}])
    tool._self_reflect_with_fallback = AsyncMock(return_value="code_suggestions:\n- suggestion_score: 5\n")
    tool.analyze_self_reflection_response = AsyncMock()

    data = await _run(tool)

    tool.analyze_self_reflection_response.assert_awaited_once()
    assert "score_why" not in data["code_suggestions"][0]


@pytest.mark.asyncio
async def test_the_fallback_score_still_clears_the_publish_threshold():
    """Documents why the marker matters: the fallback score is not filtered out."""
    from pr_agent.tools.pr_code_suggestions import get_suggestions_score_threshold

    assert SELF_REFLECTION_FALLBACK_SCORE >= get_suggestions_score_threshold()


def test_the_marker_satisfies_the_condition_that_renders_it_in_the_published_body():
    """The body only emits a rationale for a truthy score_why, so an empty marker would vanish."""
    assert bool(SELF_REFLECTION_UNAVAILABLE_REASON)
