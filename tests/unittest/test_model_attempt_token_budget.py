from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pr_agent.algo.pr_processing as pr_processing
import pr_agent.tools.pr_code_suggestions as pr_code_suggestions
from pr_agent.algo.token_handler import TokenEncoder, TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class TaggedEncoder:
    def __init__(self, model, events, weights=None):
        self.model = model
        self.events = events
        self.weights = weights or {}

    def encode(self, text, disallowed_special=()):
        self.events.append((self.model, text))
        token_count = len(text.split()) * self.weights.get(self.model, 1)
        return list(range(token_count))


class FakeProvider:
    def __init__(self, files):
        self.files = files
        self.diff_calls = 0

    def get_diff_files(self):
        self.diff_calls += 1
        return self.files

    def get_languages(self):
        return {"Python": 100}


@pytest.fixture
def model_settings():
    keys = (
        "config.model",
        "config.model_weak",
        "config.fallback_models",
        "config.patch_extra_lines_before",
        "config.patch_extra_lines_after",
        "config.verbosity_level",
        "openai.deployment_id",
        "openai.fallback_deployments",
        "pr_code_suggestions.decouple_hunks",
        "pr_code_suggestions.parallel_calls",
    )
    snapshot = snapshot_settings(keys)
    settings = get_settings()
    settings.set("config.model", "primary-model")
    settings.set("config.model_weak", "weak-model")
    settings.set("config.fallback_models", [])
    settings.set("config.patch_extra_lines_before", 0)
    settings.set("config.patch_extra_lines_after", 0)
    settings.set("config.verbosity_level", 0)
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    settings.set("pr_code_suggestions.decouple_hunks", False)
    settings.set("pr_code_suggestions.parallel_calls", False)
    yield settings
    restore_settings(snapshot)


@pytest.fixture
def encoder_events(monkeypatch):
    events = []
    monkeypatch.setattr(TokenEncoder, "_encoder_instance", None)
    monkeypatch.setattr(TokenEncoder, "_model", None)
    monkeypatch.setattr(
        TokenEncoder,
        "_create_encoder",
        staticmethod(lambda model: TaggedEncoder(model, events)),
    )
    return events


def make_file(name="example.py", edit_type=EDIT_TYPE.MODIFIED, words=4):
    added = " ".join(["new"] * words)
    return FilePatchInfo(
        base_file="old\n",
        head_file=f"{added}\n",
        patch=f"@@ -1 +1 @@\n-old\n+{added}",
        filename=name,
        edit_type=edit_type,
    )


def make_handler():
    return TokenHandler(
        SimpleNamespace(title="PR"),
        {"title": "PR"},
        "System {{ title }}",
        "User {{ title }}",
    )


@pytest.mark.parametrize(
    "pack_diff",
    [
        lambda provider, handler: pr_processing.get_pr_diff(
            provider, handler, "attempt-model"
        ),
        lambda provider, handler: pr_processing.get_pr_diff_multiple_patchs(
            provider, handler, "attempt-model"
        ),
        lambda provider, handler: pr_processing.get_pr_multi_diffs(
            provider, handler, "attempt-model"
        ),
    ],
)
def test_shared_diff_packers_use_attempted_model_for_prompt_diff_and_limit(
    monkeypatch, model_settings, encoder_events, pack_diff
):
    handler = make_handler()
    encoder_events.clear()
    max_token_models = []
    provider = FakeProvider([make_file()])
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, files: [{"language": "Python", "files": files}],
    )

    def max_tokens(model, *args, **kwargs):
        max_token_models.append(model)
        return 10_000

    monkeypatch.setattr(pr_processing, "get_max_tokens", max_tokens)

    pack_diff(provider, handler)

    assert handler.model == "primary-model"
    assert encoder_events
    assert {model for model, _text in encoder_events} == {"attempt-model"}
    assert any("System PR" in text for _model, text in encoder_events)
    assert any("new" in text for _model, text in encoder_events)
    assert set(max_token_models) == {"attempt-model"}


def test_rebinding_preserves_same_model_identity_and_primary_encoder_cache(
    monkeypatch, model_settings
):
    created_models = []
    monkeypatch.setattr(TokenEncoder, "_encoder_instance", None)
    monkeypatch.setattr(TokenEncoder, "_model", None)
    monkeypatch.setattr(
        TokenEncoder,
        "_create_encoder",
        staticmethod(
            lambda model: (
                created_models.append(model)
                or TaggedEncoder(model, [])
            )
        ),
    )
    variables = {"title": "PR"}
    handler = TokenHandler(
        SimpleNamespace(title="PR"), variables, "{{ title }}", "user"
    )
    primary_encoder = TokenEncoder._encoder_instance

    fallback_handler = handler.for_model("fallback-model")

    assert handler.for_model("primary-model") is handler
    assert fallback_handler is not handler
    assert fallback_handler.model == "fallback-model"
    assert fallback_handler.vars is variables
    assert TokenEncoder._encoder_instance is primary_encoder
    assert TokenEncoder._model == "primary-model"
    assert created_models == ["primary-model", "fallback-model"]


@pytest.mark.asyncio
async def test_routed_primary_and_fallback_repack_with_each_attempt_model(
    monkeypatch, model_settings, encoder_events
):
    model_settings.set("config.fallback_models", ["fallback-model"])
    handler = make_handler()
    encoder_events.clear()
    attempts = []
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, files: [{"language": "Python", "files": files}],
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda _model: 10_000)
    monkeypatch.setattr(
        pr_processing, "route_primary_model", lambda *_args: ("routed-model", None)
    )
    monkeypatch.setattr(pr_processing, "record_model_used", lambda *_args, **_kwargs: None)

    async def attempt(model):
        start = len(encoder_events)
        pr_processing.get_pr_diff(FakeProvider([make_file()]), handler, model)
        attempts.append((model, {tag for tag, _text in encoder_events[start:]}))
        if model == "routed-model":
            raise RuntimeError("retry")
        return "ok"

    result = await pr_processing.retry_with_fallback_models(attempt, git_provider=MagicMock())

    assert result == "ok"
    assert attempts == [
        ("routed-model", {"routed-model"}),
        ("fallback-model", {"fallback-model"}),
    ]


@pytest.mark.asyncio
async def test_weak_primary_packs_with_weak_model_not_configured_primary(
    monkeypatch, model_settings, encoder_events
):
    handler = make_handler()
    encoder_events.clear()
    attempts = []
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, files: [{"language": "Python", "files": files}],
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda _model: 10_000)
    monkeypatch.setattr(pr_processing, "route_primary_model", lambda *_args: None)
    monkeypatch.setattr(pr_processing, "record_model_used", lambda *_args, **_kwargs: None)

    async def attempt(model):
        start = len(encoder_events)
        pr_processing.get_pr_diff(FakeProvider([make_file()]), handler, model)
        attempts.append((model, {tag for tag, _text in encoder_events[start:]}))
        return "ok"

    result = await pr_processing.retry_with_fallback_models(
        attempt, model_type=ModelType.WEAK
    )

    assert result == "ok"
    assert attempts == [("weak-model", {"weak-model"})]


def test_supplementary_file_lists_supply_attempted_model_counts_to_clipping(
    monkeypatch, model_settings
):
    events = []
    weights = {"primary-model": 1, "attempt-model": 10}
    monkeypatch.setattr(TokenEncoder, "_encoder_instance", None)
    monkeypatch.setattr(TokenEncoder, "_model", None)
    monkeypatch.setattr(
        TokenEncoder,
        "_create_encoder",
        staticmethod(lambda model: TaggedEncoder(model, events, weights)),
    )
    handler = make_handler()
    files = [
        make_file("added.py", EDIT_TYPE.ADDED, words=80),
        make_file("modified.py", EDIT_TYPE.MODIFIED, words=80),
    ]
    provider = FakeProvider(files)
    clip_calls = []
    original_clip_tokens = pr_processing.clip_tokens
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, diff_files: [
            {"language": "Python", "files": diff_files}
        ],
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda _model: 2_000)

    def record_clip(text, max_tokens, *args, **kwargs):
        if text:
            clip_calls.append((text, kwargs.get("num_input_tokens")))
        return original_clip_tokens(text, max_tokens, *args, **kwargs)

    monkeypatch.setattr(pr_processing, "clip_tokens", record_clip)

    pr_processing.get_pr_diff(provider, handler, "attempt-model")

    assert any(text.startswith(pr_processing.ADDED_FILES_) for text, _ in clip_calls)
    assert any(text.startswith(pr_processing.MORE_MODIFIED_FILES_) for text, _ in clip_calls)
    for text, supplied_count in clip_calls:
        assert supplied_count == len(TaggedEncoder("attempt-model", [], weights).encode(text))


def test_deleted_supplementary_branch_defensively_uses_attempted_model_count(
    monkeypatch, model_settings, encoder_events
):
    handler = make_handler()
    encoder_events.clear()
    clip_calls = []
    provider = FakeProvider([])
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, _files: [],
    )
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_extended_diff",
        lambda *_args, **_kwargs: ([], 10_000, []),
    )
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_compressed_diff",
        lambda *_args, **_kwargs: (
            [[]],
            [10],
            [],
            ["deleted.py"],
            {
                "deleted.py": {
                    "patch": "",
                    "tokens": 0,
                    "edit_type": EDIT_TYPE.DELETED,
                }
            },
            [[]],
        ),
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda _model: 4_000)

    def record_clip(text, max_tokens, *args, **kwargs):
        if text:
            clip_calls.append((text, kwargs.get("num_input_tokens")))
        return text

    monkeypatch.setattr(pr_processing, "clip_tokens", record_clip)

    pr_processing.get_pr_diff(provider, handler, "attempt-model")

    assert len(clip_calls) == 1
    text, supplied_count = clip_calls[0]
    assert text.startswith(pr_processing.DELETED_FILES_)
    assert supplied_count == len(TaggedEncoder("attempt-model", []).encode(text))


def test_prepared_diff_reuses_only_the_same_attempt_bound_real_handler(
    monkeypatch, model_settings, encoder_events
):
    handler = make_handler()
    encoder_events.clear()
    files = [
        make_file(f"file_{index}.py", words=80)
        for index in range(4)
    ]
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda _languages, diff_files: [
            {"language": "Python", "files": diff_files}
        ],
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda _model: 1_700)

    attempt_handler = handler.for_model("attempt-model")
    same_attempt_provider = FakeProvider(files)
    prepared = pr_processing.get_pr_diff(
        same_attempt_provider,
        attempt_handler,
        "attempt-model",
        add_line_numbers_to_hunks=True,
        return_prepared=True,
    )
    assert prepared.file_dict

    pr_processing.get_pr_multi_diffs(
        same_attempt_provider,
        attempt_handler,
        "attempt-model",
        add_line_numbers=True,
        prepared_diff=prepared,
    )

    assert same_attempt_provider.diff_calls == 1

    stale_handler_provider = FakeProvider(files)
    stale_prepared = pr_processing.get_pr_diff(
        stale_handler_provider,
        handler,
        "attempt-model",
        add_line_numbers_to_hunks=True,
        return_prepared=True,
    )
    assert stale_prepared.file_dict

    pr_processing.get_pr_multi_diffs(
        stale_handler_provider,
        handler,
        "attempt-model",
        add_line_numbers=True,
        prepared_diff=stale_prepared,
    )

    assert stale_handler_provider.diff_calls == 2


def make_suggestions_tool(handler):
    tool = pr_code_suggestions.PRCodeSuggestions.__new__(
        pr_code_suggestions.PRCodeSuggestions
    )
    tool.git_provider = MagicMock()
    tool.token_handler = handler
    tool._predict_chunks = AsyncMock(
        return_value=[{"code_suggestions": []}]
    )
    tool._recover_failed_chunks = AsyncMock()
    tool._limit_suggestions_per_file = lambda suggestions: suggestions
    return tool


@pytest.mark.asyncio
async def test_default_improve_attempt_binds_handler_through_conversion_and_clip(
    monkeypatch, model_settings, encoder_events
):
    handler = make_handler()
    tool = make_suggestions_tool(handler)
    packed_models = []
    clip_counts = []
    patch = "## File: 'example.py'\n\n@@ -1 +1 @@\n-old\n+new"

    def pack(_provider, attempt_handler, model, **_kwargs):
        packed_models.append((model, attempt_handler.model))
        return [patch]

    def clip(text, _limit, *args, **kwargs):
        clip_counts.append(kwargs.get("num_input_tokens"))
        return text

    monkeypatch.setattr(pr_code_suggestions, "get_pr_multi_diffs", pack)
    monkeypatch.setattr(
        pr_code_suggestions,
        "decouple_and_convert_to_hunks_with_lines_numbers",
        lambda text, file=None: text,
    )
    monkeypatch.setattr(pr_code_suggestions, "get_max_tokens", lambda *_args, **_kwargs: 2_001)
    monkeypatch.setattr(pr_code_suggestions, "clip_tokens", clip)

    await tool.prepare_prediction_main("attempt-model")

    assert tool.token_handler.model == "attempt-model"
    assert packed_models == [("attempt-model", "attempt-model")]
    assert clip_counts
    assert clip_counts == [tool.token_handler.count_tokens(tool.patches_diff_list[0])]


@pytest.mark.asyncio
async def test_default_improve_routed_primary_failure_rebinds_for_fallback(
    monkeypatch, model_settings, encoder_events
):
    model_settings.set("config.fallback_models", ["fallback-model"])
    handler = make_handler()
    tool = make_suggestions_tool(handler)
    packed_attempts = []
    clipped_attempts = []
    patch = "## File: 'example.py'\n\n@@ -1 +1 @@\n-old\n+new"

    def pack(_provider, attempt_handler, model, **_kwargs):
        packed_attempts.append((model, attempt_handler.model))
        return [patch]

    def clip(text, _limit, *args, **kwargs):
        clipped_attempts.append(
            (tool.token_handler.model, kwargs.get("num_input_tokens"))
        )
        return text

    async def predict(model, _chunk_pairs):
        if model == "routed-model":
            raise RuntimeError("retry")
        return [{"code_suggestions": []}]

    tool._predict_chunks = predict
    monkeypatch.setattr(pr_code_suggestions, "get_pr_multi_diffs", pack)
    monkeypatch.setattr(
        pr_code_suggestions,
        "decouple_and_convert_to_hunks_with_lines_numbers",
        lambda text, file=None: text,
    )
    monkeypatch.setattr(
        pr_code_suggestions,
        "get_max_tokens",
        lambda *_args, **_kwargs: 2_001,
    )
    monkeypatch.setattr(pr_code_suggestions, "clip_tokens", clip)
    monkeypatch.setattr(
        pr_processing,
        "route_primary_model",
        lambda *_args: ("routed-model", None),
    )
    monkeypatch.setattr(
        pr_processing,
        "record_model_used",
        lambda *_args, **_kwargs: None,
    )

    result = await pr_processing.retry_with_fallback_models(
        tool.prepare_prediction_main,
        git_provider=tool.git_provider,
    )

    assert result == {"code_suggestions": []}
    assert packed_attempts == [
        ("routed-model", "routed-model"),
        ("fallback-model", "fallback-model"),
    ]
    assert [model for model, _count in clipped_attempts] == [
        "routed-model",
        "fallback-model",
    ]
    assert all(count is not None for _model, count in clipped_attempts)


@pytest.mark.asyncio
async def test_default_improve_preserves_arbitrary_fake_handler_identity(
    monkeypatch, model_settings
):
    fake_handler = MagicMock()
    tool = make_suggestions_tool(fake_handler)
    observed_handlers = []
    model_settings.set("pr_code_suggestions.decouple_hunks", True)

    def pack(_provider, handler, _model, **_kwargs):
        observed_handlers.append(handler)
        return ["chunk"]

    monkeypatch.setattr(pr_code_suggestions, "get_pr_multi_diffs", pack)

    await tool.prepare_prediction_main("attempt-model")

    assert tool.token_handler is fake_handler
    assert observed_handlers == [fake_handler]
