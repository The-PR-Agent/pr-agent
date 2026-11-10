from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import pr_agent.commands.pr_add_docs as add_docs_module
import pr_agent.commands.pr_generate_labels as generate_labels_module
import pr_agent.commands.pr_questions as questions_module
import pr_agent.commands.pr_update_changelog as update_changelog_module
from pr_agent.algo.pr_processing import FallbackEligibleError, retry_with_fallback_models
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.mark.parametrize(
    "command_class, command_module, attributes, diff_options",
    [
        (
            add_docs_module.PRAddDocs,
            add_docs_module,
            {},
            {"add_line_numbers_to_hunks": True, "disable_extra_lines": False, "return_remaining_files": True},
        ),
        (
            generate_labels_module.PRGenerateLabels,
            generate_labels_module,
            {"pr_id": "repo#1"},
            {"return_remaining_files": True},
        ),
        (questions_module.PRQuestions, questions_module, {}, {"return_remaining_files": True}),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module, {}, {"return_remaining_files": True}),
    ],
    ids=["add-docs", "generate-labels", "questions", "update-changelog"],
)
@pytest.mark.asyncio
async def test_prepare_prediction_forwards_attempt_output_reserve(
    monkeypatch,
    command_class,
    command_module,
    attributes,
    diff_options,
):
    def output_token_reserve(model, default):
        assert model == "fallback-model"
        assert default == 1_500
        return 5_000

    command = command_class.__new__(command_class)
    command.git_provider = SimpleNamespace(pr=None, get_pr_url=lambda: "https://example.test/pr/1")
    command.token_handler = object()
    command.ai_handler = SimpleNamespace(get_output_token_reserve=output_token_reserve)
    command.vars = {"diff": "", "conversation_history": ""}
    prediction = "labels: []\n" if command_class is generate_labels_module.PRGenerateLabels else "prediction"
    if command_class is add_docs_module.PRAddDocs:
        prediction = "Code Documentation: []\n"
    command._get_prediction = AsyncMock(return_value=prediction)
    for name, value in attributes.items():
        setattr(command, name, value)

    # A pruned diff comes back with the files it left out.
    get_pr_diff = MagicMock(
        side_effect=lambda *_args, **kwargs: ("diff", ["more.py"]) if kwargs.get("return_remaining_files") else "diff"
    )
    monkeypatch.setattr(command_module, "get_pr_diff", get_pr_diff)
    if command_class is generate_labels_module.PRGenerateLabels:
        monkeypatch.setattr(command_module, "set_custom_labels", lambda *_args: None)

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
        command_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )

    await command._prepare_prediction("fallback-model")

    get_pr_diff.assert_called_once_with(
        command.git_provider,
        attempt_handler,
        "fallback-model",
        output_token_reserve=output_token_reserve,
        **diff_options,
    )
    forwarded_reserve = get_pr_diff.call_args.kwargs["output_token_reserve"]
    assert forwarded_reserve("fallback-model", 1_500) == 5_000
    command._get_prediction.assert_awaited_once_with("fallback-model")
    assert command.patches_diff == "diff"
    assert command.prediction == prediction


@pytest.mark.parametrize(
    "command_class, command_module, attributes",
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
    command_class,
    command_module,
    attributes,
):
    command = command_class.__new__(command_class)
    command.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    command._get_prediction = AsyncMock()
    for name, value in attributes.items():
        setattr(command, name, value)

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
        command_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: ClippingBudget(),
    )
    monkeypatch.setattr(command_module, "get_pr_diff", lambda *_args, **_kwargs: "complete-diff")
    if command_class is generate_labels_module.PRGenerateLabels:
        monkeypatch.setattr(command_module, "set_custom_labels", lambda *_args: None)

    with pytest.raises(FallbackEligibleError, match="complete packed .* diff"):
        await command._prepare_prediction("fallback-model")

    command._get_prediction.assert_not_awaited()


def test_no_input_capacity_is_model_specific():
    budget = AttemptTokenBudget(
        "small-model",
        object(),
        SimpleNamespace(prompt_tokens=8),
        context_window=10,
    )

    with pytest.raises(FallbackEligibleError, match="no input capacity"):
        budget.require_input_capacity(3)


@pytest.mark.asyncio
async def test_add_docs_does_not_call_model_when_no_diff_fits(monkeypatch):
    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command.git_provider = SimpleNamespace(pr=None)
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": ""}
    command._get_prediction = AsyncMock()
    budget = SimpleNamespace(token_handler=object())

    monkeypatch.setattr(
        add_docs_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: budget,
    )
    # Unprocessed files keep an empty diff a fallback case; a fully filtered PR skips instead.
    monkeypatch.setattr(add_docs_module, "get_pr_diff", lambda *_args, **_kwargs: ("", ["a.py"]))

    with pytest.raises(ValueError, match="No PR diff fits"):
        await command._prepare_prediction("fallback-model")

    command._get_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_docs_removes_temporary_comment_after_terminal_failure(monkeypatch):
    class Config:
        publish_output = True

        @staticmethod
        def get(_key, default=None):
            return default

    command = add_docs_module.PRAddDocs.__new__(add_docs_module.PRAddDocs)
    command.git_provider = MagicMock()
    command._prepare_prediction = AsyncMock()
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

    await command.run()

    assert command.git_provider.publish_comment.call_args_list == [
        call("Generating Documentation...", is_temporary=True),
    ]
    command.git_provider.publish_command_failure.assert_called_once_with(
        "add_docs", "Failed to generate code documentation for this PR."
    )
    command.git_provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.parametrize(
    "command_class, command_module, attributes, expected_comment",
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
async def test_prompt_command_removes_temporary_comment_after_terminal_failure(
    monkeypatch,
    command_class,
    command_module,
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
    command = command_class.__new__(command_class)
    command.git_provider = MagicMock()
    command._prepare_prediction = AsyncMock()
    for name, value in attributes.items():
        setattr(command, name, value)

    monkeypatch.setattr(command_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        command_module,
        "retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("all attempts failed")),
    )

    with pytest.raises(RuntimeError, match="all attempts failed"):
        await command.run()

    command.git_provider.publish_comment.assert_called_once_with(
        expected_comment,
        is_temporary=True,
    )
    command.git_provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.parametrize(
    "command_class, command_module, command",
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
    command_class,
    command_module,
    command,
):
    instance = command_class.__new__(command_class)
    instance.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    instance.ai_handler = SimpleNamespace()
    instance.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    instance._get_prediction = AsyncMock()

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
        command_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )
    # Unprocessed files keep an empty diff a fallback case; a fully filtered PR skips instead.
    monkeypatch.setattr(
        command_module,
        "get_pr_diff",
        lambda *_args, **kwargs: ("", ["a.py"]) if kwargs.get("return_remaining_files") else "",
    )

    with pytest.raises(ValueError, match=f"No PR diff fits the {command} request"):
        await instance._prepare_prediction("small-model")

    instance._get_prediction.assert_not_awaited()


@pytest.mark.parametrize(
    "command_class, command_module",
    [
        (questions_module.PRQuestions, questions_module),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module),
    ],
)
@pytest.mark.parametrize("fit_mode", ["empty", "clipped"])
@pytest.mark.asyncio
async def test_model_specific_diff_failure_advances_to_fallback_model(
    monkeypatch,
    command_class,
    command_module,
    fit_mode,
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

    command = command_class.__new__(command_class)
    command.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "conversation_history": "", "pr_link": ""}
    command._get_prediction = AsyncMock(return_value="fallback prediction")
    diff_models = []

    class FakeBudget:
        token_handler = object()

        def __init__(self, model):
            self.model = model

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            return SimpleNamespace(
                optional_text=optional_text[:-1] if fit_mode == "clipped" and self.model == "small-model"
                else optional_text,
                system_prompt="system",
                user_prompt="user",
            )

    def get_diff(_provider, _handler, model, **kwargs):
        diff_models.append(model)
        if fit_mode == "empty" and model == "small-model":
            # Unprocessed files keep an empty diff a fallback case; a fully filtered PR skips instead.
            return ("", ["a.py"]) if kwargs.get("return_remaining_files") else ""
        return "diff"

    monkeypatch.setattr(
        command_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda model, *_args, **_kwargs: FakeBudget(model),
    )
    monkeypatch.setattr(command_module, "get_pr_diff", get_diff)

    try:
        await retry_with_fallback_models(command._prepare_prediction)
    finally:
        restore_settings(settings_snapshot)

    assert diff_models == ["small-model", "large-model"]
    command._get_prediction.assert_awaited_once_with("large-model")
    assert command.prediction == "fallback prediction"


@pytest.mark.asyncio
async def test_generate_labels_counts_custom_schema_before_rejecting_empty_diff(monkeypatch):
    captured_variables = []
    command = generate_labels_module.PRGenerateLabels.__new__(generate_labels_module.PRGenerateLabels)
    command.git_provider = SimpleNamespace(pr=None)
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "custom_labels_class": ""}
    command.pr_id = "repo#1"
    command._get_prediction = AsyncMock(return_value="labels: []")

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
    # Unprocessed files keep an empty diff a fallback case; a fully filtered PR skips instead.
    monkeypatch.setattr(generate_labels_module, "get_pr_diff", lambda *_args, **_kwargs: ("", ["a.py"]))

    with pytest.raises(ValueError, match="No PR diff fits the /generate_labels request"):
        await command._prepare_prediction("fallback-model")

    assert captured_variables == [{"diff": "", "custom_labels_class": "Bug | Feature"}]
    command._get_prediction.assert_not_awaited()


@pytest.mark.parametrize(
    "command_class, command_module, attributes",
    [
        (add_docs_module.PRAddDocs, add_docs_module, {}),
        (generate_labels_module.PRGenerateLabels, generate_labels_module, {"pr_id": "repo#1"}),
        (update_changelog_module.PRUpdateChangelog, update_changelog_module, {}),
    ],
    ids=["add-docs", "generate-labels", "update-changelog"],
)
@pytest.mark.asyncio
async def test_prepare_prediction_skips_when_all_changed_files_are_filtered(
    monkeypatch,
    command_class,
    command_module,
    attributes,
):
    command = command_class.__new__(command_class)
    command.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "pr_link": ""}
    command.prediction = None
    command._get_prediction = AsyncMock()
    for name, value in attributes.items():
        setattr(command, name, value)

    class FakeBudget:
        token_handler = object()

        def require_input_capacity(self, *_args, **_kwargs):
            return 1

    monkeypatch.setattr(
        command_module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda *_args, **_kwargs: FakeBudget(),
    )
    # A lockfile-only or asset-only PR has no diff left after file-type filtering.
    monkeypatch.setattr(command_module, "get_pr_diff", lambda *_args, **_kwargs: "")
    if command_class is generate_labels_module.PRGenerateLabels:
        monkeypatch.setattr(command_module, "set_custom_labels", lambda *_args: None)

    await command._prepare_prediction("fallback-model")

    command._get_prediction.assert_not_awaited()
    assert command.prediction is None


@pytest.mark.asyncio
async def test_changelog_counts_pr_link_before_packing(monkeypatch):
    captured_variables = []
    settings_snapshot = snapshot_settings(("pr_update_changelog.add_pr_link",))
    command = update_changelog_module.PRUpdateChangelog.__new__(
        update_changelog_module.PRUpdateChangelog
    )
    command.git_provider = SimpleNamespace(
        pr=None,
        get_pr_url=MagicMock(return_value="https://example.test/pr/1"),
    )
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "pr_link": ""}
    command._get_prediction = AsyncMock(return_value="prediction")

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
        await command._prepare_prediction("fallback-model")
    finally:
        restore_settings(settings_snapshot)

    assert captured_variables == [
        {"diff": "", "pr_link": "https://example.test/pr/1"}
    ]
    command.git_provider.get_pr_url.assert_called_once_with()
    command._get_prediction.assert_awaited_once_with("fallback-model")


@pytest.mark.asyncio
async def test_questions_keep_recent_history_before_diff_packing(monkeypatch):
    budget_variables = []
    budget_images = []
    fit_calls = []
    command = questions_module.PRQuestions.__new__(questions_module.PRQuestions)
    command.git_provider = SimpleNamespace(pr=None)
    command.ai_handler = SimpleNamespace()
    command.vars = {
        "diff": "",
        "conversation_history": "old\nrecent",
        "img_path": "https://example.test/image.png",
    }
    command._get_prediction = AsyncMock(return_value="prediction")

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

    await command._prepare_prediction("fallback-model")

    assert budget_variables == [
        {
            "diff": "",
            "conversation_history": "",
            "img_path": "https://example.test/image.png",
            "related_tickets": [],
            "related_tickets_omitted": 0,
        },
        {
            "diff": "",
            "conversation_history": "recent",
            "img_path": "https://example.test/image.png",
            "related_tickets": [],
            "related_tickets_omitted": 0,
        },
    ]
    assert budget_images == ["https://example.test/image.png"] * 2
    assert fit_calls == [
        (1, "conversation_history", "old\nrecent", "suffix", "https://example.test/image.png"),
        (2, "diff", "diff", None, "https://example.test/image.png"),
    ]
    assert command.vars["conversation_history"] == "old\nrecent"
    command._get_prediction.assert_awaited_once_with("fallback-model")


@pytest.mark.asyncio
async def test_questions_retry_candidate_rejects_exhausted_fixed_prompt(monkeypatch):
    command = questions_module.PRQuestions.__new__(questions_module.PRQuestions)
    command.git_provider = SimpleNamespace(pr=None)
    command.ai_handler = SimpleNamespace()
    command.vars = {"diff": "", "conversation_history": ""}
    command._get_prediction = AsyncMock(return_value="prediction")
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
        await command._prepare_prediction("small-model")
    await command._prepare_prediction("fallback-model")

    get_pr_diff.assert_called_once()
    command._get_prediction.assert_awaited_once_with("fallback-model")
