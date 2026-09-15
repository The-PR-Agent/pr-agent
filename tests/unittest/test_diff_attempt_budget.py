from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pr_agent.algo.pr_processing as pr_processing
import pr_agent.algo.token_budget as token_budget_module
from pr_agent.algo.token_handler import TokenEncoder, TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class TaggedEncoder:
    def __init__(self, model, events):
        self.model = model
        self.events = events

    def encode(self, text, disallowed_special=()):
        self.events.append((self.model, text))
        return list(range(len(text.split()) * (2 if self.model == "fallback-model" else 1)))


class Provider:
    def __init__(self):
        self.diff_calls = 0

    def get_diff_files(self):
        self.diff_calls += 1
        return [
            FilePatchInfo("old\n", "new\n", "@@ -1 +1 @@\n-old\n+" + "new " * 80,
                          f"file_{index}.py", edit_type=EDIT_TYPE.MODIFIED)
            for index in range(4)
        ]

    def get_languages(self):
        return {"Python": 100}


@pytest.fixture
def attempt_context(monkeypatch):
    values = {
        "config.model": "primary-model",
        "config.model_weak": "weak-model",
        "config.fallback_models": [],
        "config.patch_extra_lines_before": 0,
        "config.patch_extra_lines_after": 0,
        "config.large_patch_policy": "skip",
        "openai.deployment_id": None,
        "openai.fallback_deployments": [],
    }
    snapshot = snapshot_settings(values)
    for key, value in values.items():
        get_settings().set(key, value)
    events = []
    monkeypatch.setattr(TokenEncoder, "_encoder_instance", None)
    monkeypatch.setattr(TokenEncoder, "_model", None)
    monkeypatch.setattr(TokenEncoder, "_create_encoder", staticmethod(lambda model: TaggedEncoder(model, events)))
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda _languages, files: [{"files": files}])
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda model, **kwargs: 2_000)
    handler = TokenHandler(SimpleNamespace(title="PR"), {"title": "PR"}, "System {{ title }}", "User {{ title }}")
    events.clear()
    yield handler, events
    restore_settings(snapshot)


@pytest.mark.parametrize("name", ["get_pr_diff", "get_pr_diff_multiple_patchs", "get_pr_multi_diffs"])
def test_public_packers_bind_the_attempt_before_prompt_and_diff_counting(monkeypatch, attempt_context, name):
    source, events = attempt_context
    windows = []
    reserves = []

    def window(model, **kwargs):
        windows.append(model)
        return 2_000

    def reserve(model, default):
        reserves.append((model, default))
        return default + 100

    monkeypatch.setattr(token_budget_module, "get_max_tokens", window)
    getattr(pr_processing, name)(Provider(), source, "fallback-model", output_token_reserve=reserve)

    assert windows == ["fallback-model"]
    assert reserves == [("fallback-model", 1_500)] + (
        [] if name == "get_pr_multi_diffs" else [("fallback-model", 1_000)]
    )
    assert {model for model, text in events} == {"fallback-model"}
    assert any(text == "System PR" for model, text in events)
    assert any("+new" in text for model, text in events)
    assert source.model == get_settings().config.model == "primary-model"
    assert TokenEncoder._model == "primary-model"


@pytest.mark.parametrize("name", ["get_pr_diff", "get_pr_diff_multiple_patchs"])
def test_compressed_budgets_keep_default_plus_reasoning_reserves(monkeypatch, attempt_context, name):
    source, _ = attempt_context
    seen = []
    original = pr_processing.pr_generate_compressed_diff

    def compressed(languages, handler, soft, hard, *args, **kwargs):
        seen.append((handler.model, soft, hard))
        return original(languages, handler, soft, hard, *args, **kwargs)

    monkeypatch.setattr(pr_processing, "pr_generate_compressed_diff", compressed)
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda model, **kwargs: 2_400)

    getattr(pr_processing, name)(
        Provider(), source, "fallback-model", output_token_reserve=lambda model, default: default + 500,
    )

    # Compare against the bound fallback prompt's eight tokens, not the primary model's four.
    assert seen == [("fallback-model", 392, 892)]


@pytest.mark.parametrize("name", ["get_pr_diff", "get_pr_diff_multiple_patchs"])
def test_negative_hard_capacity_stops_before_admitting_the_first_patch(attempt_context, name):
    source, _ = attempt_context

    # Preserve the stricter hard stop for both valid callback values, even when
    # the handler's two policies leave positive soft but negative hard room.
    result = getattr(pr_processing, name)(
        Provider(), source, "fallback-model",
        output_token_reserve=lambda model, default: 3_000 if default == 1_000 else 1_500,
    )

    if name == "get_pr_diff":
        assert result == ""
    else:
        assert result[0] == [[]]
        assert set(result[3]) == {f"file_{index}.py" for index in range(4)}


@pytest.mark.parametrize("handler_form", ["source", "bound"])
@pytest.mark.parametrize("reserve", [1_500, 1_800])
def test_prepared_fallback_reuses_bound_counts_and_matches_fresh_packing(attempt_context, handler_form, reserve):
    source, events = attempt_context
    provider = Provider()
    prepared = pr_processing.get_pr_diff(
        provider, source, "fallback-model", add_line_numbers_to_hunks=True, return_prepared=True,
    )
    assert prepared.file_dict
    handler = source if handler_form == "source" else prepared.attempt_budget.token_handler
    events.clear()

    chunks = pr_processing.get_pr_multi_diffs(
        provider, handler, "fallback-model", prepared_diff=prepared,
        output_token_reserve=lambda model, default: reserve,
    )

    assert provider.diff_calls == 1
    assert not any(text == "System PR" for model, text in events)
    assert {model for model, text in events} == {"fallback-model"}
    fresh = pr_processing.get_pr_multi_diffs(
        Provider(), source, "fallback-model", output_token_reserve=lambda model, default: reserve,
    )
    assert chunks == fresh
    assert all(8 + prepared.token_handler.count_tokens(chunk) + reserve <= 2_000 for chunk in chunks)


@pytest.mark.parametrize("reuse", [False, True])
def test_multi_packing_preserves_negative_capacity_and_remaining_files(monkeypatch, attempt_context, reuse):
    source, _ = attempt_context
    provider = Provider()
    prepared = pr_processing.get_pr_diff(
        provider, source, "fallback-model", add_line_numbers_to_hunks=True, return_prepared=True,
    ) if reuse else None
    capacities = []
    original = pr_processing._pack_pr_multi_diffs

    def pack(*args):
        capacities.append(args[-1])
        return original(*args)

    monkeypatch.setattr(pr_processing, "_pack_pr_multi_diffs", pack)
    chunks, remaining = pr_processing.get_pr_multi_diffs(
        provider, source, "fallback-model", prepared_diff=prepared, return_remaining_files=True,
        output_token_reserve=lambda model, default: 2_500,
    )

    assert capacities == [-508]
    assert chunks == []
    assert set(remaining) == {f"file_{index}.py" for index in range(4)}
    assert provider.diff_calls == 1


@pytest.mark.parametrize("changed", ["model", "handler", "mode"])
def test_incompatible_prepared_data_is_rebuilt(attempt_context, changed):
    source, events = attempt_context
    provider = Provider()
    prepared = pr_processing.get_pr_diff(
        provider, source, "fallback-model", add_line_numbers_to_hunks=True, return_prepared=True,
    )
    handler = source.for_model("fallback-model") if changed == "handler" else source
    model = "weak-model" if changed == "model" else "fallback-model"
    events.clear()

    pr_processing.get_pr_multi_diffs(
        provider, handler, model, prepared_diff=prepared, add_line_numbers=changed != "mode",
    )

    assert provider.diff_calls == 2
    assert {tag for tag, text in events} == {model}


@pytest.mark.asyncio
@pytest.mark.parametrize("weak", [False, True])
async def test_routed_weak_and_fallback_attempts_pack_with_their_own_tokenizer(monkeypatch, attempt_context, weak):
    source, events = attempt_context
    get_settings().set("config.fallback_models", ["fallback-model"])
    monkeypatch.setattr(pr_processing, "route_primary_model", lambda *args: None if weak else ("routed-model", None))
    monkeypatch.setattr(pr_processing, "record_model_used", lambda *args, **kwargs: None)
    attempts = []

    async def attempt(model):
        events.clear()
        pr_processing.get_pr_diff(Provider(), source, model)
        attempts.append((model, {tag for tag, text in events}))
        if model != "fallback-model":
            raise RuntimeError("try fallback")
        return "ok"

    result = await pr_processing.retry_with_fallback_models(
        attempt, model_type=ModelType.WEAK if weak else ModelType.REGULAR, git_provider=MagicMock(),
    )

    primary = "weak-model" if weak else "routed-model"
    assert result == "ok"
    assert attempts == [(primary, {primary}), ("fallback-model", {"fallback-model"})]
    assert source.model == get_settings().config.model == "primary-model"
