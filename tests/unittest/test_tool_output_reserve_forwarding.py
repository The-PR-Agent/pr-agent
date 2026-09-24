from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pr_agent.tools.pr_add_docs as add_docs_module
import pr_agent.tools.pr_generate_labels as generate_labels_module
import pr_agent.tools.pr_questions as questions_module
import pr_agent.tools.pr_update_changelog as update_changelog_module
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


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
    tool.git_provider = SimpleNamespace(pr=None, get_pr_url=lambda: "https://example.test/pr/1")
    tool.token_handler = object()
    tool.ai_handler = SimpleNamespace(get_output_token_reserve=output_token_reserve)
    tool.vars = {"diff": "", "conversation_history": ""}
    prediction = "labels: []\n" if tool_class is generate_labels_module.PRGenerateLabels else "prediction"
    tool._get_prediction = AsyncMock(return_value=prediction)
    for name, value in attributes.items():
        setattr(tool, name, value)

    get_pr_diff = MagicMock(return_value="diff")
    monkeypatch.setattr(tool_module, "get_pr_diff", get_pr_diff)
    if tool_class is generate_labels_module.PRGenerateLabels:
        monkeypatch.setattr(tool_module, "set_custom_labels", lambda *_args: None)

    attempt_handler = object()

    class FakeBudget:
        token_handler = attempt_handler

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    monkeypatch.setattr(
        tool_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )

    await tool._prepare_prediction("fallback-model")

    get_pr_diff.assert_called_once_with(
        tool.git_provider,
        attempt_handler,
        "fallback-model",
        output_token_reserve=output_token_reserve,
        **diff_options,
    )
    forwarded_reserve = get_pr_diff.call_args.kwargs["output_token_reserve"]
    assert forwarded_reserve("fallback-model", 1_500) == 5_000
    tool._get_prediction.assert_awaited_once_with("fallback-model")
    assert tool.patches_diff == "diff"
    assert tool.prediction == prediction


@pytest.mark.parametrize(
    "tool_class, tool_module, attributes",
    [
        (add_docs_module.PRAddDocs, add_docs_module, {}),
        (generate_labels_module.PRGenerateLabels, generate_labels_module, {"pr_id": "repo#1"}),
        (questions_module.PRQuestions, questions_module, {}),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module, {}),
    ],
    ids=["add-docs", "generate-labels", "questions", "update-changelog"],
)
@pytest.mark.asyncio
async def test_prepare_prediction_rejects_clipped_packed_diff(
    monkeypatch,
    tool_class,
    tool_module,
    attributes,
):
    tool = tool_class.__new__(tool_class)
    tool.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    tool._get_prediction = AsyncMock()
    for name, value in attributes.items():
        setattr(tool, name, value)

    class ClippingBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text[:-1],
                system_prompt="system",
                user_prompt="user",
            )

    monkeypatch.setattr(
        tool_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: ClippingBudget(),
    )
    monkeypatch.setattr(tool_module, "get_pr_diff", lambda *_args, **_kwargs: "complete-diff")
    if tool_class is generate_labels_module.PRGenerateLabels:
        monkeypatch.setattr(tool_module, "set_custom_labels", lambda *_args: None)

    with pytest.raises(ValueError, match="complete packed .* diff"):
        await tool._prepare_prediction("fallback-model")

    tool._get_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_docs_does_not_call_model_when_no_diff_fits(monkeypatch):
    tool = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    tool.git_provider = SimpleNamespace(pr=None)
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": ""}
    tool._get_prediction = AsyncMock()
    budget = SimpleNamespace(token_handler=object())

    monkeypatch.setattr(
        add_docs_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: budget,
    )
    monkeypatch.setattr(add_docs_module, "get_pr_diff", lambda *_args, **_kwargs: "")

    with pytest.raises(ValueError, match="No PR diff fits"):
        await tool._prepare_prediction("fallback-model")

    tool._get_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_docs_removes_temporary_comment_after_terminal_failure(monkeypatch):
    class Config:
        publish_output = True

        @staticmethod
        def get(_key, default=None):
            return default

    tool = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    tool.git_provider = MagicMock()
    tool._prepare_prediction = AsyncMock()
    monkeypatch.setattr(
        add_docs_module,
        "get_settings",
        lambda: SimpleNamespace(config=Config()),
    )
    monkeypatch.setattr(
        add_docs_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("all attempts failed")),
    )

    await tool.run()

    tool.git_provider.publish_comment.assert_called_once_with(
        "Generating Documentation...",
        is_temporary=True,
    )
    tool.git_provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.parametrize(
    "tool_class, tool_module, attributes, expected_comment",
    [
        (
            questions_module.PRQuestions,
            questions_module,
            {
                "pr_url": "https://example.test/pr/1",
                "identify_image_in_comment": MagicMock(return_value=None),
            },
            "Preparing answer...",
        ),
        (
            update_changelog_module.PRUpdateChangelog,
            update_changelog_module,
            {"push_skipped_reason": None},
            "Preparing changelog updates...",
        ),
    ],
    ids=["questions", "update-changelog"],
)
@pytest.mark.asyncio
async def test_prompt_tool_removes_temporary_comment_after_terminal_failure(
    monkeypatch,
    tool_class,
    tool_module,
    attributes,
    expected_comment,
):
    class AttrDict(dict):
        __getattr__ = dict.__getitem__

    settings = SimpleNamespace(
        config=AttrDict(publish_output=True),
        pr_questions=AttrDict(),
        get=lambda *_args, **_kwargs: {},
    )
    tool = tool_class.__new__(tool_class)
    tool.git_provider = MagicMock()
    tool._prepare_prediction = AsyncMock()
    for name, value in attributes.items():
        setattr(tool, name, value)

    monkeypatch.setattr(tool_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tool_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("all attempts failed")),
    )

    with pytest.raises(RuntimeError, match="all attempts failed"):
        await tool.run()

    tool.git_provider.publish_comment.assert_called_once_with(
        expected_comment,
        is_temporary=True,
    )
    tool.git_provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.parametrize(
    "tool_class, tool_module, command",
    [
        (questions_module.PRQuestions, questions_module, "/ask"),
        (
            update_changelog_module.PRUpdateChangelog,
            update_changelog_module,
            "/update_changelog",
        ),
    ],
)
@pytest.mark.asyncio
async def test_empty_attempt_diff_retries_instead_of_succeeding(
    monkeypatch,
    tool_class,
    tool_module,
    command,
):
    tool = tool_class.__new__(tool_class)
    tool.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    tool._get_prediction = AsyncMock()

    class FakeBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    monkeypatch.setattr(
        tool_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )
    monkeypatch.setattr(tool_module, "get_pr_diff", lambda *_args, **_kwargs: "")

    with pytest.raises(ValueError, match=f"No PR diff fits the {command} request"):
        await tool._prepare_prediction("small-model")

    tool._get_prediction.assert_not_awaited()


@pytest.mark.parametrize(
    "tool_class, tool_module",
    [
        (questions_module.PRQuestions, questions_module),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module),
    ],
)
@pytest.mark.asyncio
async def test_empty_attempt_diff_advances_to_fallback_model(
    monkeypatch,
    tool_class,
    tool_module,
):
    settings_snapshot = snapshot_settings(
        (
            "config.model",
            "config.fallback_models",
            "openai.deployment_id",
            "openai.fallback_deployments",
        )
    )
    get_settings().set("config.model", "small-model")
    get_settings().set("config.fallback_models", ["large-model"])
    get_settings().set("openai.deployment_id", "primary")
    get_settings().set("openai.fallback_deployments", ["fallback"])

    tool = tool_class.__new__(tool_class)
    tool.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    tool._get_prediction = AsyncMock(return_value="fallback prediction")
    diff_models = []

    class FakeBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    def get_diff(_provider, _handler, model, **_kwargs):
        diff_models.append(model)
        return "" if model == "small-model" else "diff"

    monkeypatch.setattr(
        tool_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )
    monkeypatch.setattr(tool_module, "get_pr_diff", get_diff)

    try:
        await retry_with_fallback_models(tool._prepare_prediction)
    finally:
        restore_settings(settings_snapshot)

    assert diff_models == ["small-model", "large-model"]
    tool._get_prediction.assert_awaited_once_with("large-model")
    assert tool.prediction == "fallback prediction"


@pytest.mark.asyncio
async def test_generate_labels_counts_custom_schema_before_rejecting_empty_diff(monkeypatch):
    captured_variables = []
    tool = generate_labels_module.PRGenerateLabels.__new__(generate_labels_module.PRGenerateLabels)
    tool.git_provider = SimpleNamespace(pr=None)
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "custom_labels_class": ""}
    tool.pr_id = "repo#1"
    tool._get_prediction = AsyncMock(return_value="labels: []")

    class FakeBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    def make_budget(_model, _pr, variables, *_args, **_kwargs):
        captured_variables.append(variables.copy())
        return FakeBudget()

    def set_custom_labels(variables, _provider):
        variables["custom_labels_class"] = "Bug | Feature"

    monkeypatch.setattr(generate_labels_module, "set_custom_labels", set_custom_labels)
    monkeypatch.setattr(
        generate_labels_module.AttemptTokenBudget,
        "for_prompt_attempt",
        make_budget,
    )
    monkeypatch.setattr(generate_labels_module, "get_pr_diff", lambda *_args, **_kwargs: "")

    with pytest.raises(ValueError, match="No PR diff fits the /generate_labels request"):
        await tool._prepare_prediction("fallback-model")

    assert captured_variables == [{"diff": "", "custom_labels_class": "Bug | Feature"}]
    tool._get_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_changelog_counts_pr_link_before_packing(monkeypatch):
    captured_variables = []
    settings_snapshot = snapshot_settings(("pr_update_changelog.add_pr_link",))
    tool = update_changelog_module.PRUpdateChangelog.__new__(
        update_changelog_module.PRUpdateChangelog
    )
    tool.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "pr_link": ""}
    tool._get_prediction = AsyncMock(return_value="prediction")

    class FakeBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    def make_budget(_model, _pr, variables, *_args, **_kwargs):
        captured_variables.append(variables.copy())
        return FakeBudget()

    monkeypatch.setattr(
        update_changelog_module.AttemptTokenBudget,
        "for_prompt_attempt",
        make_budget,
    )
    monkeypatch.setattr(update_changelog_module, "get_pr_diff", lambda *_args, **_kwargs: "diff")

    try:
        get_settings().set("pr_update_changelog.add_pr_link", True)
        await tool._prepare_prediction("fallback-model")
    finally:
        restore_settings(settings_snapshot)

    assert captured_variables == [
        {"diff": "", "pr_link": "https://example.test/pr/1"}
    ]
    tool.git_provider.get_pr_url.assert_called_once_with()
    tool._get_prediction.assert_awaited_once_with("fallback-model")


@pytest.mark.asyncio
async def test_questions_keep_recent_history_before_diff_packing(monkeypatch):
    budget_variables = []
    budget_images = []
    fit_calls = []
    tool = questions_module.PRQuestions.__new__(questions_module.PRQuestions)
    tool.git_provider = SimpleNamespace(pr=None)
    tool.ai_handler = SimpleNamespace()
    tool.vars = {
        "diff": "",
        "conversation_history": "old\nrecent",
        "img_path": "https://example.test/image.png",
    }
    tool._get_prediction = AsyncMock(return_value="prediction")

    class FakeBudget:
        def __init__(self, index):
            self.index = index
            self.token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, name, optional_text, **kwargs):
            fit_calls.append(
                (self.index, name, optional_text, kwargs.get("keep"), kwargs.get("image_path"))
            )
            fitted_text = "recent" if name == "conversation_history" else optional_text
            return SimpleNamespace(
                optional_text=fitted_text,
                system_prompt="system",
                user_prompt="user",
            )

    def make_budget(_model, _pr, variables, *_args, **kwargs):
        budget_variables.append(variables.copy())
        budget_images.append(kwargs.get("image_path"))
        return FakeBudget(len(budget_variables))

    monkeypatch.setattr(
        questions_module.AttemptTokenBudget,
        "for_prompt_attempt",
        make_budget,
    )
    monkeypatch.setattr(questions_module, "get_pr_diff", lambda *_args, **_kwargs: "diff")

    await tool._prepare_prediction("fallback-model")

    assert budget_variables == [
        {
            "diff": "",
            "conversation_history": "",
            "img_path": "https://example.test/image.png",
        },
        {
            "diff": "",
            "conversation_history": "recent",
            "img_path": "https://example.test/image.png",
        },
    ]
    assert budget_images == ["https://example.test/image.png"] * 2
    assert fit_calls == [
        (1, "conversation_history", "old\nrecent", "suffix", "https://example.test/image.png"),
        (2, "diff", "diff", None, "https://example.test/image.png"),
    ]
    assert tool.vars["conversation_history"] == "old\nrecent"
    tool._get_prediction.assert_awaited_once_with("fallback-model")


@pytest.mark.asyncio
async def test_questions_retry_candidate_rejects_exhausted_fixed_prompt(monkeypatch):
    tool = questions_module.PRQuestions.__new__(questions_module.PRQuestions)
    tool.git_provider = SimpleNamespace(pr=None)
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "conversation_history": ""}
    tool._get_prediction = AsyncMock(return_value="prediction")
    get_pr_diff = MagicMock(return_value="diff")

    class FakeBudget:
        def __init__(self, model):
            self.model = model
            self.token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            if self.model == "small-model":
                raise ValueError("required prompt leaves no input capacity")
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    monkeypatch.setattr(
        questions_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda model, *_args, **_kwargs: FakeBudget(model),
    )
    monkeypatch.setattr(questions_module, "get_pr_diff", get_pr_diff)

    with pytest.raises(ValueError, match="no input capacity"):
        await tool._prepare_prediction("small-model")
    await tool._prepare_prediction("fallback-model")

    get_pr_diff.assert_called_once()
    tool._get_prediction.assert_awaited_once_with("fallback-model")
