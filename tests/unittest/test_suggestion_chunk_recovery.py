"""Behavior of recovery after a partially successful suggestion pass."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from starlette_context import request_cycle_context

import pr_agent.commands.pr_code_suggestions as module
from pr_agent.algo import token_budget as token_budget_module
from pr_agent.algo.pr_processing import PackedPRDiffs, retry_with_fallback_models
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

PRCodeSuggestions = module.PRCodeSuggestions


@pytest.fixture
def configured():
    values = {
        "config.model": "gpt-4o",
        "config.fallback_models": ["gpt-4o-mini", "gpt-4.1"],
        "config.publish_output": True,
        "openai.deployment_id": "primary",
        "openai.fallback_deployments": ["secondary", "last"],
        "pr_code_suggestions.decouple_hunks": True,
        "pr_code_suggestions.parallel_calls": True,
        "pr_code_suggestions.suggestions_score_threshold": 0,
        "pr_code_suggestions.max_suggestions_per_file": 0,
        # Not used by most tests here, but snapshotted so the routed-primary test's
        # writes are restored and no routing state leaks into later tests.
        "model_routing.enable": False,
        "model_routing.rules": [],
    }
    snapshot = snapshot_settings(tuple(values))
    for key, value in values.items():
        get_settings().set(key, value)
    yield
    restore_settings(snapshot)


@pytest.fixture(params=[
    pytest.param(None, id="invalid-schema"),
    pytest.param("code_suggestions:\n- relevant_file: b.py\n", id="all-invalid-entries"),
    pytest.param(("code_suggestions:\n- relevant_file: b.py\n  improved_code: |\n    new(", "length"),
                 id="truncated-yaml"),
])
def truncated_response(request):
    return request.param


def make_command(monkeypatch, failures):
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = MagicMock()
    command.token_handler = MagicMock()
    command.vars = {"instructions": "scope-one"}
    command.pr_code_suggestions_prompt_system = "Review {{ instructions }}"
    command.pr_code_suggestions_prompt_user = "{{ diff }}"
    calls = []

    async def completion(*, model, system, user, **kwargs):
        calls.append((model, user, get_settings().get("openai.deployment_id"), system))
        failure = failures.get((model, user))
        if isinstance(failure, BaseException):
            raise failure
        if callable(failure):
            await failure()
        if isinstance(failure, str):
            return failure, "stop"
        if isinstance(failure, tuple):
            return failure
        suggestion = {
            "relevant_file": user + ".py", "one_sentence_summary": "Check " + user,
            "label": "possible issue", "suggestion_content": "Handle the error.",
            "existing_code": "old()", "improved_code": "new()",
            "relevant_lines_start": 1, "relevant_lines_end": 1,
        }
        return yaml.safe_dump({"code_suggestions": [suggestion]}), "stop"

    async def reflect(*args):
        return ""

    command.ai_handler = SimpleNamespace(chat_completion=completion)
    command._self_reflect_with_fallback = reflect
    monkeypatch.setattr(module, "get_pr_multi_diffs", lambda *a, **k: PackedPRDiffs(["a", "b", "c"], [], []))
    return command, calls


@pytest.mark.parametrize("parallel", [True, False])
async def test_recovery_restores_missing_results_without_replacing_successes(configured, monkeypatch, parallel):
    get_settings().set("pr_code_suggestions.parallel_calls", parallel)
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): TimeoutError("provider timeout")})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [(m, chunk, dep) for m, chunk, dep, _ in calls] == [
        ("gpt-4o", "a", "primary"), ("gpt-4o", "b", "primary"),
        ("gpt-4o", "c", "primary"), ("gpt-4o-mini", "b", "secondary"),
    ]
    assert command.failed_chunk_count == 0
    assert command.total_chunk_count == 3
    assert get_settings().get("openai.deployment_id") == "primary"


async def test_recovery_exhaustion_keeps_successes_and_reports_remaining_gap(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {
        ("gpt-4o", "b"): RuntimeError("primary failed"),
        ("gpt-4o", "c"): RuntimeError("primary failed"),
        ("gpt-4o-mini", "b"): RuntimeError("secondary failed"),
        ("gpt-4o-mini", "c"): RuntimeError("secondary failed"),
        ("gpt-4.1", "c"): RuntimeError("last failed"),
    })
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py"]
    assert command.failed_chunk_count == 1
    assert [(m, c) for m, c, _, _ in calls].count(("gpt-4o", "a")) == 1
    assert [c for m, c, _, _ in calls if m == "gpt-4.1"] == ["b", "c"]


async def test_healthy_run_never_enters_recovery(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert len(result["code_suggestions"]) == 3
    assert command.failed_chunk_count == 0
    assert {m for m, _, _, _ in calls} == {"gpt-4o"}


@pytest.mark.parametrize("fallbacks, expected", [([], ["a.py", "c.py"]), (["gpt-4o-mini"], ["a.py", "b.py", "c.py"])])
async def test_fallback_recovery_runs_whenever_fallbacks_are_set(configured, monkeypatch, fallbacks, expected):
    # Recovery is the default behaviour whenever config.fallback_models is set,
    # so an empty fallback list preserves the partial-success policy while a
    # configured fallback repairs the failed chunk.
    get_settings().set("config.fallback_models", fallbacks)
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == expected
    assert command.failed_chunk_count == (1 if not fallbacks else 0)
    assert len(calls) == (3 if not fallbacks else 4)


@pytest.mark.asyncio
async def test_larger_fallback_recovers_when_earlier_one_is_over_budget(configured, monkeypatch):
    # The first fallback model cannot fit the full prompt, so it is skipped and
    # recovery still moves on to the later model instead of giving up.
    get_settings().set("config.fallback_models", ["gpt-4o-mini", "gpt-4.1"])
    monkeypatch.setattr(
        token_budget_module,
        "get_max_tokens",
        lambda model, **kwargs: 1600 if model == "gpt-4o-mini" else 10000,
    )
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    command.vars["instructions"] = "must retain these instructions " * 500
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert command.failed_chunk_count == 0
    assert [(m, c) for m, c, _, _ in calls if m == "gpt-4.1"] == [("gpt-4.1", "b")]


@pytest.mark.asyncio
async def test_recovery_skips_over_budget_fallback_without_truncation(configured, monkeypatch):
    command, _calls = make_command(monkeypatch, {})
    command._recovery_chain = MagicMock(
        return_value=(
            [
                ("gpt-4o", "primary"),
                ("gpt-4o-mini", "secondary"),
                ("gpt-4.1", "last"),
            ],
            1,
        )
    )
    recovered = {"code_suggestions": []}
    command._predict_chunks = AsyncMock(return_value=[recovered])

    prepared_inputs = []

    class FakeBudget:
        def __init__(self, model):
            self.model = model

        def render_prompt_templates(self, variables):
            prepared_inputs.append((self.model, variables["diff"], variables["diff_no_line_numbers"]))
            return "system", "user"

        def prepare_request(self, *_args):
            return SimpleNamespace(input_tokens=101 if self.model == "gpt-4o-mini" else 100)

        def input_token_limit(self, *_args, **_kwargs):
            return 100

    monkeypatch.setattr(
        module.AttemptTokenBudget,
        "for_prompt_attempt",
        lambda model, *_args, **_kwargs: FakeBudget(model),
    )
    results = [{"code_suggestions": []}, RuntimeError("primary failed")]
    chunk_pairs = [("numbered-a", "a"), ("numbered-b", "complete-b")]

    await command._recover_failed_chunks("gpt-4o", chunk_pairs, results)

    command._predict_chunks.assert_awaited_once_with(
        "gpt-4.1",
        [("numbered-b", "complete-b")],
    )
    assert results[1] is recovered
    assert prepared_inputs == [
        ("gpt-4o-mini", "numbered-b", "complete-b"),
        ("gpt-4.1", "numbered-b", "complete-b"),
    ]


async def test_all_failed_primary_keeps_existing_outer_fallback(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {("gpt-4o", c): TimeoutError("failure") for c in "abc"})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o"] * 3 + ["gpt-4o-mini"] * 3
    assert command.failed_chunk_count == 0


@pytest.mark.parametrize("unprocessed_kind", ["remaining", "partial"])
async def test_empty_primary_chunk_list_keeps_outer_fallback(configured, monkeypatch, unprocessed_kind):
    get_settings().set("config.fallback_models", ["gpt-4.1"])
    command, calls = make_command(monkeypatch, {})
    packed_models = []

    def pack_for_model(_provider, _token_handler, model, **_kwargs):
        packed_models.append(model)
        if model == "gpt-4o":
            return (PackedPRDiffs([], ["primary-only.py"], []) if unprocessed_kind == "remaining"
                    else PackedPRDiffs([], [], ["primary-only.py"]))
        return PackedPRDiffs(["a"], [], [])

    monkeypatch.setattr(module, "get_pr_multi_diffs", pack_for_model)

    result = await retry_with_fallback_models(command.prepare_prediction_main)

    assert packed_models == ["gpt-4o", "gpt-4.1"]
    assert [(model, chunk) for model, chunk, _, _ in calls] == [("gpt-4.1", "a")]
    assert [suggestion["relevant_file"] for suggestion in result["code_suggestions"]] == ["a.py"]
    assert command.remaining_files_list == []
    assert command.partial_files_list == []


@pytest.mark.parametrize("decouple_hunks", [True, False])
async def test_no_analyzable_diff_skips_outer_fallback(configured, monkeypatch, decouple_hunks):
    get_settings().set("pr_code_suggestions.decouple_hunks", decouple_hunks)
    command, calls = make_command(monkeypatch, {})
    packed_models = []

    def pack_for_model(_provider, _token_handler, model, **_kwargs):
        packed_models.append(model)
        return PackedPRDiffs([], [], [])

    monkeypatch.setattr(module, "get_pr_multi_diffs", pack_for_model)

    result = await retry_with_fallback_models(command.prepare_prediction_main)

    assert result == {"code_suggestions": []}
    assert packed_models == ["gpt-4o"] * (1 if decouple_hunks else 2)
    assert calls == []
    assert command.total_chunk_count == 0


async def test_partial_success_on_outer_fallback_only_tries_later_models(configured, monkeypatch):
    failures = {("gpt-4o", c): TimeoutError("failure") for c in "abc"}
    failures[("gpt-4o-mini", "b")] = RuntimeError("secondary failed")
    command, calls = make_command(monkeypatch, failures)
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o"] * 3 + ["gpt-4o-mini"] * 3 + ["gpt-4.1"]
    assert get_settings().get("openai.deployment_id") == "primary"


@pytest.mark.parametrize("remaining_model", [True, False])
@pytest.mark.asyncio
async def test_oversized_fallback_is_skipped_without_truncating_context(configured, monkeypatch, remaining_model):
    if not remaining_model:
        get_settings().set("config.fallback_models", ["gpt-4o-mini"])
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    command.vars["instructions"] = "must retain these instructions " * 500
    # Use the real per-model tokenizer, with a deliberately smaller configured capacity.
    monkeypatch.setattr(
        token_budget_module,
        "get_max_tokens",
        lambda model, **kwargs: 1600 if model == "gpt-4o-mini" else 10000,
    )
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert not any(m == "gpt-4o-mini" for m, _, _, _ in calls)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == (
        ["a.py", "b.py", "c.py"] if remaining_model else ["a.py", "c.py"]
    )
    assert command.failed_chunk_count == (0 if remaining_model else 1)
    assert all(system == "Review " + command.vars["instructions"] for _, _, _, system in calls)


async def test_marker_text_in_diff_is_counted_literally_for_recovery(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {("gpt-4o", "<|endoftext|>"): RuntimeError("failure")})
    # Patch after make_command so the fixture's default chunk list does not win.
    monkeypatch.setattr(
        module, "get_pr_multi_diffs", lambda *a, **k: PackedPRDiffs(["a", "<|endoftext|>", "c"], [], []))
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert ("gpt-4o-mini", "<|endoftext|>", "secondary") in [(m, c, d) for m, c, d, _ in calls]
    assert len(result["code_suggestions"]) == 3


@pytest.mark.asyncio
async def test_boundary_size_chunk_is_eligible_for_recovery(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    monkeypatch.setattr(module.TokenHandler, "count_tokens", lambda self, s: 1)
    monkeypatch.setattr(
        token_budget_module,
        "get_max_tokens",
        lambda model, **kwargs: 1050 if model == "gpt-4o-mini" else 10000,
    )
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert command.failed_chunk_count == 0


async def test_recovery_reserves_handler_output_budget_for_fallback(configured, monkeypatch):
    # When the AI handler exposes a concrete output allowance (e.g. a large
    # config.max_output_tokens), recovery reserves it for the completion instead of
    # the fixed soft threshold. The first fallback then cannot fit the prompt
    # (5001 - 5000 = 1 < 50 tokens including framing), while the fixed 1500 threshold would have let
    # it recover the chunk; only the later model recovers it.
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    reserves = []

    def get_output_token_reserve(model, default):
        reserves.append((model, get_settings().get("openai.deployment_id")))
        return 5000

    command.ai_handler = SimpleNamespace(
        chat_completion=command.ai_handler.chat_completion,
        get_output_token_reserve=get_output_token_reserve,
    )
    monkeypatch.setattr(module.TokenHandler, "count_tokens", lambda self, s: 1)
    monkeypatch.setattr(
        token_budget_module,
        "get_max_tokens",
        lambda model, **kwargs: 5001 if model == "gpt-4o-mini" else 10000,
    )
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert command.failed_chunk_count == 0
    assert [m for m, _, _, _ in calls] == ["gpt-4o"] * 3 + ["gpt-4.1"]
    assert [(m, c) for m, c, _, _ in calls if m == "gpt-4.1"] == [("gpt-4.1", "b")]
    fallback_reserves = [(model, deployment) for model, deployment in reserves if model != "gpt-4o"]
    assert fallback_reserves == [
        ("gpt-4o-mini", "secondary"),
        ("gpt-4.1", "last"),
        ("gpt-4.1", "last"),
    ]


async def test_empty_prediction_is_a_success_not_a_retry_trigger(configured, monkeypatch):
    command, calls = make_command(monkeypatch, {
        ("gpt-4o", "a"): "code_suggestions: []",
        ("gpt-4o", "b"): TimeoutError("failure"),
        ("gpt-4o-mini", "b"): "code_suggestions: []",
    })
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["c.py"]
    assert command.failed_chunk_count == 0
    assert len(calls) == 4


@pytest.mark.parametrize("parallel", [True, False])
async def test_invalid_suggestion_response_recovers_only_failed_chunk(
        configured, monkeypatch, parallel, truncated_response):
    get_settings().set("pr_code_suggestions.parallel_calls", parallel)
    command, calls = make_command(monkeypatch, {
        ("gpt-4o", "b"): truncated_response or "code_suggestions: 5",
        ("gpt-4o-mini", "b"): "not a suggestions mapping",
    })
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert command.failed_chunk_count == 0
    assert not command._get_suggestions_coverage_footer()
    assert [(m, c) for m, c, _, _ in calls] == [
        ("gpt-4o", "a"), ("gpt-4o", "b"), ("gpt-4o", "c"),
        ("gpt-4o-mini", "b"), ("gpt-4.1", "b"),
    ]


async def test_invalid_suggestion_response_without_fallback_reports_partial_coverage(
        configured, monkeypatch, truncated_response):
    get_settings().set("config.fallback_models", [])
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): truncated_response or "code_suggestions: null"})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert command.failed_chunk_count == 1
    assert "1 of 3 analysis chunks failed" in command._get_suggestions_coverage_footer()
    assert len(calls) == 3


@pytest.mark.parametrize("recover_on_fallback", [True, False])
async def test_all_invalid_suggestions_use_outer_fallback(
        configured, monkeypatch, recover_on_fallback, truncated_response):
    invalid_models = ["gpt-4o"] if recover_on_fallback else ["gpt-4o", "gpt-4o-mini", "gpt-4.1"]
    command, calls = make_command(monkeypatch, {
        (model, chunk): truncated_response or "code_suggestions: null"
        for model in invalid_models for chunk in "abc"
    })
    if recover_on_fallback:
        result = await retry_with_fallback_models(command.prepare_prediction_main)
        assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
        assert command.failed_chunk_count == 0
        assert len(calls) == 6
    else:
        with pytest.raises(Exception, match="Failed to generate prediction with any model"):
            await retry_with_fallback_models(command.prepare_prediction_main)
        assert len(calls) == 9


async def test_all_invalid_suggestions_mark_the_command_as_failed(configured, monkeypatch, truncated_response):
    invalid = {
        (model, chunk): truncated_response or "code_suggestions: null"
        for model in ["gpt-4o", "gpt-4o-mini", "gpt-4.1"] for chunk in "abc"
    }
    command, calls = make_command(monkeypatch, invalid)
    command.git_provider.get_files.return_value = [object()]
    command.git_provider.is_supported.return_value = False
    command.pr_url = "https://example.invalid/pull/1"
    command.progress_response = None
    snapshot = snapshot_settings(("config.publish_output_progress", "config.propagate_command_errors"))
    try:
        get_settings().set("config.publish_output_progress", False)
        get_settings().set("config.propagate_command_errors", False)
        await command.run()
        assert get_run_details().command_failed is True
        assert len(calls) == 9
    finally:
        restore_settings(snapshot)


@pytest.mark.parametrize("parent_cancel", [True, False])
async def test_cancellation_stops_recovery_and_restores_deployment(configured, monkeypatch, parent_cancel):
    started = asyncio.Event()
    finished = asyncio.Event()

    async def wait_or_cancel():
        started.set()
        try:
            if parent_cancel:
                await asyncio.Event().wait()
            raise asyncio.CancelledError()
        finally:
            finished.set()

    command, calls = make_command(monkeypatch, {
        ("gpt-4o", "b"): RuntimeError("failure"),
        ("gpt-4o-mini", "b"): wait_or_cancel,
    })
    task = asyncio.create_task(retry_with_fallback_models(command.prepare_prediction_main))
    await asyncio.wait_for(started.wait(), timeout=5)
    if parent_cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not any(m == "gpt-4.1" for m, _, _, _ in calls)
    assert get_settings().get("openai.deployment_id") == "primary"
    command.git_provider.publish_comment.assert_not_called()


async def test_model_switch_waits_for_successful_sibling_to_finish(configured, monkeypatch):
    completed = asyncio.Event()

    async def finish_later():
        await asyncio.sleep(0)
        assert get_settings().get("openai.deployment_id") == "primary"
        completed.set()

    async def recover():
        assert completed.is_set()
        assert get_settings().get("openai.deployment_id") == "secondary"

    command, _ = make_command(monkeypatch, {
        ("gpt-4o", "a"): finish_later,
        ("gpt-4o", "b"): RuntimeError("failure"),
        ("gpt-4o-mini", "b"): recover,
    })
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert len(result["code_suggestions"]) == 3


async def test_instances_do_not_reuse_predictions_or_prompt_context(configured, monkeypatch):
    first, first_calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    await retry_with_fallback_models(first.prepare_prediction_main)
    second, second_calls = make_command(monkeypatch, {("gpt-4o", "a"): RuntimeError("failure")})
    second.vars["instructions"] = "scope-two"
    result = await retry_with_fallback_models(second.prepare_prediction_main)
    assert len(result["code_suggestions"]) == 3
    assert all(system == "Review scope-one" for _, _, _, system in first_calls)
    assert all(system == "Review scope-two" for _, _, _, system in second_calls)
    assert [c for m, c, _, _ in second_calls if m == "gpt-4o-mini"] == ["a"]


async def test_concurrent_requests_keep_separate_deployments(configured, monkeypatch):
    both_recovering = asyncio.Event()
    started = []

    async def invoke(name):
        with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
            get_settings().set("openai.deployment_id", name + "-primary")
            get_settings().set("openai.fallback_deployments", [name + "-secondary", name + "-last"])

            async def overlap():
                started.append(name)
                if len(started) == 2:
                    both_recovering.set()
                await asyncio.wait_for(both_recovering.wait(), timeout=5)
                assert get_settings().get("openai.deployment_id") == name + "-secondary"

            command, calls = make_command(monkeypatch, {
                ("gpt-4o", "b"): TimeoutError("failure"), ("gpt-4o-mini", "b"): overlap,
            })
            command.vars["instructions"] = name
            result = await retry_with_fallback_models(command.prepare_prediction_main)
            assert len(result["code_suggestions"]) == 3
            assert all(dep.startswith(name) and system == "Review " + name for _, _, dep, system in calls)
            assert get_settings().get("openai.deployment_id") == name + "-primary"

    await asyncio.gather(invoke("one"), invoke("two"))
    assert get_settings().get("openai.deployment_id") == "primary"


@pytest.mark.parametrize("recovered", [True, False])
async def test_published_suggestions_and_coverage_match_completed_chunks(configured, monkeypatch, recovered):
    failures = {("gpt-4o", "b"): RuntimeError("primary failed")}
    if not recovered:
        failures.update({(model, "b"): RuntimeError("failed") for model in ["gpt-4o-mini", "gpt-4.1"]})
    command, _ = make_command(monkeypatch, failures)
    command.git_provider.diff_files = [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"]
    command.git_provider.publish_code_suggestions.return_value = True
    command.git_provider.supports_code_suggestions_artifact.return_value = False
    init_run_details()
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    await command.push_inline_code_suggestions(result)
    published = command.git_provider.publish_code_suggestions.call_args.args[0]
    assert [s["relevant_file"] for s in published] == (["a.py", "b.py", "c.py"] if recovered else ["a.py", "c.py"])
    assert all("```suggestion\nnew()\n```" in s["body"] for s in published)
    assert bool(command._get_suggestions_coverage_footer()) is not recovered
    assert command.git_provider.publish_comment.called is not recovered
    details = get_run_details()
    assert details.fallback_used is recovered
    # The outer wrapper records the primary model after recovery restores the deployment,
    # so the model line names the primary while the sticky flag preserves the fallback.
    assert details.model_used == "gpt-4o"
    assert details.num_ai_calls == 0


async def test_recovered_suggestion_still_passes_anchor_validation(configured, monkeypatch):
    command, _ = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    command.git_provider.diff_files = [FilePatchInfo("", "old()\n", "", c + ".py") for c in "ac"]
    command.git_provider.supports_code_suggestions_artifact.return_value = False
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    await command.push_inline_code_suggestions(result)
    published = command.git_provider.publish_code_suggestions.call_args.args[0]
    assert [s["relevant_file"] for s in published] == ["a.py", "c.py"]
    assert "b.py" in command.git_provider.publish_comment.call_args.args[0]
    assert "```suggestion" not in command.git_provider.publish_comment.call_args.args[0]


async def test_recovered_run_details_report_primary_model_and_sticky_fallback(configured, monkeypatch):
    command, _ = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    command.git_provider.diff_files = [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"]
    command.git_provider.publish_code_suggestions.return_value = True
    command.git_provider.supports_code_suggestions_artifact.return_value = False
    init_run_details()
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    await command.push_inline_code_suggestions(result)
    details = get_run_details()
    assert details.fallback_used is True
    assert details.model_used == "gpt-4o"
    assert details.num_ai_calls == 0


@pytest.mark.parametrize("fallback_deployment", ["primary", "secondary"])
def test_recovery_chain_distinguishes_deployments_for_the_same_model(configured, monkeypatch, fallback_deployment):
    chain = [("gpt-4o", "primary"), ("gpt-4o", fallback_deployment)]
    monkeypatch.setattr(module, "get_effective_fallback_chain", lambda: chain)
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)

    expected = None if fallback_deployment == "primary" else (chain, 1)
    assert command._recovery_chain("gpt-4o", get_settings()) == expected


async def test_ambiguous_model_deployment_chain_keeps_partial_result(configured, monkeypatch):
    get_settings().set("config.fallback_models", ["gpt-4o", "gpt-4.1"])
    get_settings().set("openai.fallback_deployments", ["primary", "last"])
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert len(calls) == 3


async def test_repeated_fallback_pair_skips_recoveries_without_retrying_it(configured, monkeypatch):
    # A unique fallback pair appearing twice in the chain would route the same
    # failed chunk to an identical (model, deployment) a second time, so recovery
    # gives up and keeps the partial result instead of adding duplicate inference.
    get_settings().set("config.fallback_models", ["gpt-4o-mini", "gpt-4o-mini"])
    get_settings().set("openai.fallback_deployments", ["secondary", "secondary"])
    command, calls = make_command(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(command.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert len(calls) == 3
    assert command.failed_chunk_count == 1


async def test_recovery_uses_existing_reflection_before_publishing_results(configured, monkeypatch):
    keys = ("config.model_reasoning", "pr_code_suggestions_reflect_prompt.system",
            "pr_code_suggestions_reflect_prompt.user")
    snapshot = snapshot_settings(keys)
    try:
        get_settings().set(keys[0], "gpt-4o")
        get_settings().set(keys[1], "Reflect")
        get_settings().set(keys[2], "{{ diff }}")
        command, _ = make_command(monkeypatch, {("gpt-4o", "b"): TimeoutError("failure")})
        del command._self_reflect_with_fallback
        generation = command.ai_handler.chat_completion
        reflections = []

        async def completion(*, model, system, user, **kwargs):
            if system != "Reflect":
                return await generation(model=model, system=system, user=user, **kwargs)
            reflections.append((model, user, get_settings().get("openai.deployment_id")))
            return yaml.safe_dump({"code_suggestions": [{"suggestion_score": 9, "why": "Verified"}]}), "stop"

        command.ai_handler.chat_completion = completion
        result = await retry_with_fallback_models(command.prepare_prediction_main)
        assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
        assert all(s["score"] == 9 and s["score_why"] == "Verified" for s in result["code_suggestions"])
        assert ("gpt-4o-mini", "b", "secondary") in reflections
        assert len(reflections) == 3
    finally:
        restore_settings(snapshot)


async def test_routed_primary_recovers_failed_slots_with_configured_fallback(configured, monkeypatch):
    # A routed primary ([model_routing]) replaces config.model ahead of the same
    # fallbacks; recovery must reproduce that substitution instead of scanning
    # the configured chain. The routed model is kept out of the fallback list so
    # the (model, deployment) pair stays unique in the effective chain.
    get_settings().set("config.fallback_models", ["gpt-4.1"])
    get_settings().set("openai.fallback_deployments", ["last"])
    get_settings().set("model_routing.enable", True)
    get_settings().set("model_routing.rules", [{"model": "gpt-4o-mini", "max_files": 10, "deployment_id": "secondary"}])
    command, calls = make_command(monkeypatch, {("gpt-4o-mini", "b"): RuntimeError("failure")})
    command.git_provider.get_diff_files.return_value = [
        FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"
    ]
    result = await retry_with_fallback_models(command.prepare_prediction_main, git_provider=command.git_provider)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o-mini"] * 3 + ["gpt-4.1"]
    assert get_settings().get("openai.deployment_id") == "primary"
    command.git_provider.get_diff_files.assert_called_once_with()


@pytest.mark.parametrize("parallel", [True, False])
@pytest.mark.parametrize("deployment, fallback_deployments", [(None, []), ("", []), (None, [""]), ("", [None])])
async def test_routed_collision_recovers_failed_chunks_with_displaced_primary(configured, monkeypatch, parallel,
                                                                            deployment, fallback_deployments):
    get_settings().set("pr_code_suggestions.parallel_calls", parallel)
    get_settings().set("config.model", "gpt-5.6")
    get_settings().set("config.fallback_models", ["gpt-5.6-terra"])
    get_settings().set("openai.deployment_id", deployment)
    get_settings().set("openai.fallback_deployments", fallback_deployments)
    get_settings().set("model_routing.enable", True)
    get_settings().set("model_routing.rules", [{"model": "gpt-5.6-terra", "max_files": 10}])
    command, calls = make_command(monkeypatch, {("gpt-5.6-terra", "b"): RuntimeError("failure")})
    command.git_provider.get_diff_files.return_value = [
        FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"
    ]

    result = await retry_with_fallback_models(command.prepare_prediction_main, git_provider=command.git_provider)

    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [(model, chunk, dep) for model, chunk, dep, _ in calls] == [
        ("gpt-5.6-terra", "a", None), ("gpt-5.6-terra", "b", None), ("gpt-5.6-terra", "c", None),
        ("gpt-5.6", "b", None),
    ]
    assert command.failed_chunk_count == 0
    assert get_settings().get("openai.deployment_id") == deployment
    command.git_provider.get_diff_files.assert_called_once_with()


async def test_routing_is_not_repeated_during_recovery(configured, monkeypatch):
    get_settings().set("config.fallback_models", ["gpt-4.1"])
    get_settings().set("openai.fallback_deployments", ["last"])
    get_settings().set("model_routing.enable", True)
    get_settings().set("model_routing.rules", [{"model": "gpt-4o-mini", "max_files": 10, "deployment_id": "secondary"}])
    command, calls = make_command(monkeypatch, {("gpt-4o-mini", "b"): RuntimeError("failure")})
    command.git_provider.get_diff_files.side_effect = [
        [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"],
        RuntimeError("transient routing failure"),
    ]

    result = await retry_with_fallback_models(command.prepare_prediction_main, git_provider=command.git_provider)

    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o-mini"] * 3 + ["gpt-4.1"]
    command.git_provider.get_diff_files.assert_called_once_with()
