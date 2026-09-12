"""Behavior of opt-in recovery after a partially successful suggestion pass."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from starlette_context import request_cycle_context

import pr_agent.tools.pr_code_suggestions as module
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


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
        "pr_code_suggestions.recover_failed_chunks": True,
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


def make_tool(monkeypatch, failures):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.token_handler = MagicMock()
    tool.vars = {"instructions": "scope-one"}
    tool.pr_code_suggestions_prompt_system = "Review {{ instructions }}"
    tool.pr_code_suggestions_prompt_user = "{{ diff }}"
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
        suggestion = {
            "relevant_file": user + ".py", "one_sentence_summary": "Check " + user,
            "label": "possible issue", "suggestion_content": "Handle the error.",
            "existing_code": "old()", "improved_code": "new()",
            "relevant_lines_start": 1, "relevant_lines_end": 1,
        }
        return yaml.safe_dump({"code_suggestions": [suggestion]}), "stop"

    async def reflect(*args):
        return ""

    tool.ai_handler = SimpleNamespace(chat_completion=completion)
    tool._self_reflect_with_fallback = reflect
    monkeypatch.setattr(module, "get_pr_multi_diffs", lambda *a, **k: ["a", "b", "c"])
    return tool, calls


@pytest.mark.parametrize("parallel", [True, False])
async def test_recovery_restores_missing_results_without_replacing_successes(configured, monkeypatch, parallel):
    get_settings().set("pr_code_suggestions.parallel_calls", parallel)
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): TimeoutError("provider timeout")})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [(m, chunk, dep) for m, chunk, dep, _ in calls] == [
        ("gpt-4o", "a", "primary"), ("gpt-4o", "b", "primary"),
        ("gpt-4o", "c", "primary"), ("gpt-4o-mini", "b", "secondary"),
    ]
    assert tool.failed_chunk_count == 0
    assert tool.total_chunk_count == 3
    assert get_settings().get("openai.deployment_id") == "primary"


async def test_recovery_exhaustion_keeps_successes_and_reports_remaining_gap(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {
        ("gpt-4o", "b"): RuntimeError("primary failed"),
        ("gpt-4o", "c"): RuntimeError("primary failed"),
        ("gpt-4o-mini", "b"): RuntimeError("secondary failed"),
        ("gpt-4o-mini", "c"): RuntimeError("secondary failed"),
        ("gpt-4.1", "c"): RuntimeError("last failed"),
    })
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py"]
    assert tool.failed_chunk_count == 1
    assert [(m, c) for m, c, _, _ in calls].count(("gpt-4o", "a")) == 1
    assert [c for m, c, _, _ in calls if m == "gpt-4.1"] == ["b", "c"]


async def test_healthy_run_never_enters_recovery(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert len(result["code_suggestions"]) == 3
    assert tool.failed_chunk_count == 0
    assert {m for m, _, _, _ in calls} == {"gpt-4o"}


@pytest.mark.parametrize("fallbacks, enabled", [([], True), (["gpt-4o-mini"], False)])
async def test_disabled_or_no_fallback_preserves_partial_policy(configured, monkeypatch, fallbacks, enabled):
    get_settings().set("config.fallback_models", fallbacks)
    get_settings().set("pr_code_suggestions.recover_failed_chunks", enabled)
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert tool.failed_chunk_count == 1
    assert len(calls) == 3


async def test_all_failed_primary_keeps_existing_outer_fallback(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {("gpt-4o", c): RuntimeError("failure") for c in "abc"})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o"] * 3 + ["gpt-4o-mini"] * 3
    assert tool.failed_chunk_count == 0


async def test_partial_success_on_outer_fallback_only_tries_later_models(configured, monkeypatch):
    failures = {("gpt-4o", c): RuntimeError("failure") for c in "abc"}
    failures[("gpt-4o-mini", "b")] = RuntimeError("secondary failed")
    tool, calls = make_tool(monkeypatch, failures)
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o"] * 3 + ["gpt-4o-mini"] * 3 + ["gpt-4.1"]
    assert get_settings().get("openai.deployment_id") == "primary"


@pytest.mark.parametrize("remaining_model", [True, False])
async def test_oversized_fallback_is_skipped_without_truncating_context(configured, monkeypatch, remaining_model):
    if not remaining_model:
        get_settings().set("config.fallback_models", ["gpt-4o-mini"])
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    tool.vars["instructions"] = "must retain these instructions " * 500
    # Use the real per-model tokenizer, with a deliberately smaller configured capacity.
    monkeypatch.setattr(module, "get_max_tokens", lambda model: 1600 if model == "gpt-4o-mini" else 10000)
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert not any(m == "gpt-4o-mini" for m, _, _, _ in calls)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == (
        ["a.py", "b.py", "c.py"] if remaining_model else ["a.py", "c.py"]
    )
    assert tool.failed_chunk_count == (0 if remaining_model else 1)
    assert all(system == "Review " + tool.vars["instructions"] for _, _, _, system in calls)


async def test_marker_text_in_diff_is_counted_literally_for_recovery(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "<|endoftext|>"): RuntimeError("failure")})
    # Patch after make_tool so the fixture's default chunk list does not win.
    monkeypatch.setattr(module, "get_pr_multi_diffs", lambda *a, **k: ["a", "<|endoftext|>", "c"])
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert ("gpt-4o-mini", "<|endoftext|>", "secondary") in [(m, c, d) for m, c, d, _ in calls]
    assert len(result["code_suggestions"]) == 3


async def test_boundary_size_chunk_is_eligible_for_recovery(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    monkeypatch.setattr(module.TokenHandler, "count_tokens", lambda self, s: 1)
    monkeypatch.setattr(module, "get_max_tokens", lambda model: 1502 if model == "gpt-4o-mini" else 10000)
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert tool.failed_chunk_count == 0


async def test_empty_prediction_is_a_success_not_a_retry_trigger(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {
        ("gpt-4o", "a"): "code_suggestions: []",
        ("gpt-4o", "b"): TimeoutError("failure"),
        ("gpt-4o-mini", "b"): "code_suggestions: []",
    })
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["c.py"]
    assert tool.failed_chunk_count == 0
    assert len(calls) == 4


async def test_invalid_yaml_recovery_preserves_parse_failure_notice(configured, monkeypatch):
    tool, calls = make_tool(monkeypatch, {
        ("gpt-4o", "b"): TimeoutError("failure"),
        ("gpt-4o-mini", "b"): "not a suggestions mapping",
    })
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert tool.failed_chunk_count == 1
    assert tool.parse_failure_count == 1
    assert len(calls) == 4


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

    tool, calls = make_tool(monkeypatch, {
        ("gpt-4o", "b"): RuntimeError("failure"),
        ("gpt-4o-mini", "b"): wait_or_cancel,
    })
    task = asyncio.create_task(retry_with_fallback_models(tool.prepare_prediction_main))
    await asyncio.wait_for(started.wait(), timeout=5)
    if parent_cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not any(m == "gpt-4.1" for m, _, _, _ in calls)
    assert get_settings().get("openai.deployment_id") == "primary"
    tool.git_provider.publish_comment.assert_not_called()


async def test_model_switch_waits_for_successful_sibling_to_finish(configured, monkeypatch):
    completed = asyncio.Event()

    async def finish_later():
        await asyncio.sleep(0)
        assert get_settings().get("openai.deployment_id") == "primary"
        completed.set()

    async def recover():
        assert completed.is_set()
        assert get_settings().get("openai.deployment_id") == "secondary"

    tool, _ = make_tool(monkeypatch, {
        ("gpt-4o", "a"): finish_later,
        ("gpt-4o", "b"): RuntimeError("failure"),
        ("gpt-4o-mini", "b"): recover,
    })
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert len(result["code_suggestions"]) == 3


async def test_instances_do_not_reuse_predictions_or_prompt_context(configured, monkeypatch):
    first, first_calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    await retry_with_fallback_models(first.prepare_prediction_main)
    second, second_calls = make_tool(monkeypatch, {("gpt-4o", "a"): RuntimeError("failure")})
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

            tool, calls = make_tool(monkeypatch, {
                ("gpt-4o", "b"): TimeoutError("failure"), ("gpt-4o-mini", "b"): overlap,
            })
            tool.vars["instructions"] = name
            result = await retry_with_fallback_models(tool.prepare_prediction_main)
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
    tool, _ = make_tool(monkeypatch, failures)
    tool.git_provider.diff_files = [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"]
    tool.git_provider.publish_code_suggestions.return_value = True
    tool.git_provider.supports_code_suggestions_artifact.return_value = False
    init_run_details()
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    await tool.push_inline_code_suggestions(result)
    published = tool.git_provider.publish_code_suggestions.call_args.args[0]
    assert [s["relevant_file"] for s in published] == (["a.py", "b.py", "c.py"] if recovered else ["a.py", "c.py"])
    assert all("```suggestion\nnew()\n```" in s["body"] for s in published)
    assert bool(tool._get_suggestions_coverage_footer()) is not recovered
    assert tool.git_provider.publish_comment.called is not recovered
    details = get_run_details()
    assert details.fallback_used is recovered
    # The outer wrapper records the primary model after recovery restores the deployment,
    # so the model line names the primary while the sticky flag preserves the fallback.
    assert details.model_used == "gpt-4o"
    assert details.num_ai_calls == 0


async def test_recovered_suggestion_still_passes_anchor_validation(configured, monkeypatch):
    tool, _ = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    tool.git_provider.diff_files = [FilePatchInfo("", "old()\n", "", c + ".py") for c in "ac"]
    tool.git_provider.supports_code_suggestions_artifact.return_value = False
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    await tool.push_inline_code_suggestions(result)
    published = tool.git_provider.publish_code_suggestions.call_args.args[0]
    assert [s["relevant_file"] for s in published] == ["a.py", "c.py"]
    assert "b.py" in tool.git_provider.publish_comment.call_args.args[0]
    assert "```suggestion" not in tool.git_provider.publish_comment.call_args.args[0]


async def test_recovered_run_details_report_primary_model_and_sticky_fallback(configured, monkeypatch):
    tool, _ = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    tool.git_provider.diff_files = [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"]
    tool.git_provider.publish_code_suggestions.return_value = True
    tool.git_provider.supports_code_suggestions_artifact.return_value = False
    init_run_details()
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    await tool.push_inline_code_suggestions(result)
    details = get_run_details()
    assert details.fallback_used is True
    assert details.model_used == "gpt-4o"
    assert details.num_ai_calls == 0


async def test_ambiguous_model_deployment_chain_keeps_partial_result(configured, monkeypatch):
    get_settings().set("config.fallback_models", ["gpt-4o", "gpt-4.1"])
    get_settings().set("openai.fallback_deployments", ["primary", "last"])
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert len(calls) == 3


async def test_repeated_fallback_pair_skips_recoveries_without_retrying_it(configured, monkeypatch):
    # A unique fallback pair appearing twice in the chain would route the same
    # failed chunk to an identical (model, deployment) a second time, so recovery
    # gives up and keeps the partial result instead of adding duplicate inference.
    get_settings().set("config.fallback_models", ["gpt-4o-mini", "gpt-4o-mini"])
    get_settings().set("openai.fallback_deployments", ["secondary", "secondary"])
    tool, calls = make_tool(monkeypatch, {("gpt-4o", "b"): RuntimeError("failure")})
    result = await retry_with_fallback_models(tool.prepare_prediction_main)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "c.py"]
    assert len(calls) == 3
    assert tool.failed_chunk_count == 1


async def test_recovery_uses_existing_reflection_before_publishing_results(configured, monkeypatch):
    keys = ("config.model_reasoning", "pr_code_suggestions_reflect_prompt.system",
            "pr_code_suggestions_reflect_prompt.user")
    snapshot = snapshot_settings(keys)
    try:
        get_settings().set(keys[0], "gpt-4o")
        get_settings().set(keys[1], "Reflect")
        get_settings().set(keys[2], "{{ diff }}")
        tool, _ = make_tool(monkeypatch, {("gpt-4o", "b"): TimeoutError("failure")})
        del tool._self_reflect_with_fallback
        generation = tool.ai_handler.chat_completion
        reflections = []

        async def completion(*, model, system, user, **kwargs):
            if system != "Reflect":
                return await generation(model=model, system=system, user=user, **kwargs)
            reflections.append((model, user, get_settings().get("openai.deployment_id")))
            return yaml.safe_dump({"code_suggestions": [{"suggestion_score": 9, "why": "Verified"}]}), "stop"

        tool.ai_handler.chat_completion = completion
        result = await retry_with_fallback_models(tool.prepare_prediction_main)
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
    get_settings().set("model_routing.rules",
                       [{"model": "gpt-4o-mini", "max_files": 10, "deployment_id": "secondary"}])
    tool, calls = make_tool(monkeypatch, {("gpt-4o-mini", "b"): RuntimeError("failure")})
    tool.git_provider.get_diff_files.return_value = [FilePatchInfo("old()\n", "old()\n", "", c + ".py") for c in "abc"]
    result = await retry_with_fallback_models(tool.prepare_prediction_main,
                                              git_provider=tool.git_provider)
    assert [s["relevant_file"] for s in result["code_suggestions"]] == ["a.py", "b.py", "c.py"]
    assert [m for m, _, _, _ in calls] == ["gpt-4o-mini"] * 3 + ["gpt-4.1"]
    assert get_settings().get("openai.deployment_id") == "primary"
