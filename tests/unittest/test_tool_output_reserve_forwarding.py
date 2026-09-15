from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pr_agent.tools.pr_add_docs as add_docs_module
import pr_agent.tools.pr_generate_labels as generate_labels_module
import pr_agent.tools.pr_questions as questions_module
import pr_agent.tools.pr_update_changelog as update_changelog_module


@pytest.mark.parametrize(
    "tool_class, tool_module, attributes, diff_options",
    [
        (
            add_docs_module.PRAddDocs,
            add_docs_module,
            {},
            {"add_line_numbers_to_hunks": True, "disable_extra_lines": False},
        ),
        (generate_labels_module.PRGenerateLabels, generate_labels_module, {"pr_id": "repo#1"}, {}),
        (questions_module.PRQuestions, questions_module, {}, {}),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module, {}, {}),
    ],
    ids=["add-docs", "generate-labels", "questions", "update-changelog"],
)
@pytest.mark.asyncio
async def test_prepare_prediction_forwards_attempt_output_reserve(
    monkeypatch,
    tool_class,
    tool_module,
    attributes,
    diff_options,
):
    def output_token_reserve(model, default):
        assert model == "fallback-model"
        assert default == 1_500
        return 5_000

    tool = tool_class.__new__(tool_class)
    tool.git_provider = object()
    tool.token_handler = object()
    tool.ai_handler = SimpleNamespace(get_output_token_reserve=output_token_reserve)
    tool._get_prediction = AsyncMock(return_value="prediction")
    for name, value in attributes.items():
        setattr(tool, name, value)

    get_pr_diff = MagicMock(return_value="diff")
    monkeypatch.setattr(tool_module, "get_pr_diff", get_pr_diff)

    await tool._prepare_prediction("fallback-model")

    get_pr_diff.assert_called_once_with(
        tool.git_provider,
        tool.token_handler,
        "fallback-model",
        output_token_reserve=output_token_reserve,
        **diff_options,
    )
    forwarded_reserve = get_pr_diff.call_args.kwargs["output_token_reserve"]
    assert forwarded_reserve("fallback-model", 1_500) == 5_000
    tool._get_prediction.assert_awaited_once_with("fallback-model")
    assert tool.patches_diff == "diff"
    assert tool.prediction == "prediction"
