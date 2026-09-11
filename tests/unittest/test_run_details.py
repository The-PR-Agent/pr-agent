import asyncio
from decimal import Decimal

import pytest

from pr_agent.algo.run_details import (
    RunDetails,
    add_token_usage,
    get_run_details,
    init_run_details,
    record_ai_call,
    record_model_used,
    set_call_findings,
)


class _Usage:
    """Stand-in for litellm's usage object (attribute access)."""

    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def test_init_returns_fresh_instance_with_zeroed_counters():
    details = init_run_details()

    assert isinstance(details, RunDetails)
    assert details.model_used is None
    assert details.fallback_used is False
    assert details.prompt_tokens == 0
    assert details.completion_tokens == 0
    assert details.total_tokens == 0
    assert details.num_ai_calls == 0
    assert details.total_cost_usd == Decimal("0")
    assert details.known_cost_call_count == 0
    assert details.model_costs_usd == {}
    assert details.cost_status == "unavailable"
    assert details.has_token_usage is False
    assert details.duration_seconds >= 0


def test_init_replaces_previous_instance():
    first = init_run_details()
    record_model_used("model-a", is_fallback=True)

    second = init_run_details()

    assert second is not first
    assert get_run_details() is second
    assert second.model_used is None
    assert second.fallback_used is False


def test_record_model_used_tracks_model_and_fallback_flag():
    init_run_details()

    record_model_used("openai/gpt-5.4", is_fallback=True)

    details = get_run_details()
    assert details.model_used == "openai/gpt-5.4"
    assert details.fallback_used is True


def test_fallback_flag_is_sticky_once_a_fallback_was_used():
    init_run_details()

    record_model_used("fallback-model", is_fallback=True)
    record_model_used("primary-model", is_fallback=False)

    details = get_run_details()
    # last successful model wins, but the fallback flag must not be cleared
    assert details.model_used == "primary-model"
    assert details.fallback_used is True


def test_add_token_usage_accumulates_across_calls():
    init_run_details()

    add_token_usage(_Usage(100, 10, 110))
    add_token_usage(
        {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    )

    details = get_run_details()
    assert details.prompt_tokens == 105
    assert details.completion_tokens == 11
    assert details.total_tokens == 116
    assert details.has_token_usage is True


def test_add_token_usage_derives_total_when_missing():
    init_run_details()

    add_token_usage({"prompt_tokens": 20, "completion_tokens": 5})

    assert get_run_details().total_tokens == 25


def test_add_token_usage_ignores_none_and_partial_objects():
    init_run_details()

    add_token_usage(None)
    add_token_usage(object())

    details = get_run_details()
    assert details.total_tokens == 0
    assert details.has_token_usage is False


def test_record_ai_call_counts_calls_even_without_usage():
    init_run_details()

    record_ai_call(_Usage(10, 2, 12))
    record_ai_call(None)

    details = get_run_details()
    assert details.num_ai_calls == 2
    assert details.total_tokens == 12


def test_record_ai_call_aggregates_decimal_costs_by_model():
    init_run_details()
    record_model_used("model-b", is_fallback=True)

    record_ai_call(_Usage(10, 2, 12), model="model-a", cost_usd=Decimal("0.0710"))
    record_ai_call(_Usage(5, 1, 6), model="model-b", cost_usd="0.0132")
    record_ai_call(_Usage(1, 1, 2), model="model-a", cost_usd=0.00001)

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0.08421")
    assert details.known_cost_call_count == 3
    assert details.cost_status == "complete"
    assert details.fallback_used is True
    assert details.model_costs_usd == {
        "model-a": Decimal("0.07101"),
        "model-b": Decimal("0.0132"),
    }


def test_record_ai_call_treats_zero_cost_as_unpriced():
    """litellm.completion_cost returns 0.0 for unpriced models and empty usage;
    recording it would render a false '$0.00' with cost status complete."""
    init_run_details()

    record_ai_call(_Usage(10, 2, 12), model="zero-priced", cost_usd=0.0)
    record_ai_call(_Usage(10, 2, 12), model="zero-priced", cost_usd=Decimal("0"))

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0")
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    assert details.model_costs_usd == {}


def test_record_ai_call_marks_partial_and_unavailable_cost_without_fabricating_zero():
    init_run_details()

    record_ai_call(_Usage(10, 2, 12), model="known", cost_usd=Decimal("0.0042"))
    record_ai_call(None, model="unknown", cost_usd=None)

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0.0042")
    assert details.known_cost_call_count == 1
    assert details.cost_status == "partial"

    init_run_details()
    record_ai_call(None, model="unknown", cost_usd=None)

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0")
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    assert details.model_costs_usd == {}


@pytest.mark.asyncio
async def test_concurrent_child_tasks_accumulate_into_parent_collector():
    init_run_details()

    async def record(prompt_tokens, completion_tokens):
        await asyncio.sleep(0)
        record_ai_call(_Usage(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens))

    await asyncio.gather(
        record(10, 1),
        record(20, 2),
        record(30, 3),
    )

    details = get_run_details()
    assert details.num_ai_calls == 3
    assert details.prompt_tokens == 60
    assert details.completion_tokens == 6
    assert details.total_tokens == 66


@pytest.mark.asyncio
async def test_concurrent_runs_keep_collectors_isolated():
    async def run_with_usage(prompt_tokens, completion_tokens):
        init_run_details()
        await asyncio.sleep(0)
        record_ai_call(_Usage(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens))
        return get_run_details()

    first, second = await asyncio.gather(
        run_with_usage(10, 1),
        run_with_usage(20, 2),
    )

    assert first.num_ai_calls == 1
    assert first.prompt_tokens == 10
    assert first.completion_tokens == 1
    assert first.total_tokens == 11

    assert second.num_ai_calls == 1
    assert second.prompt_tokens == 20
    assert second.completion_tokens == 2
    assert second.total_tokens == 22


def test_helpers_are_noops_when_not_initialized():
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    try:
        assert get_run_details() is None
        record_model_used("m", is_fallback=False)  # must not raise
        record_ai_call(_Usage(1, 1, 2))  # must not raise
        add_token_usage({"total_tokens": 5})  # must not raise
    finally:
        run_details._run_details.reset(token)


def test_set_call_findings_sets_the_matching_record():
    details = init_run_details()
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=0, sample_index=None)
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=1, sample_index=None)

    found = set_call_findings("review", 1, None, 3)

    assert found is True
    assert details.calls[0].findings_emitted is None  # the non-matching chunk is untouched
    assert details.calls[1].findings_emitted == 3


def test_set_call_findings_matches_by_identity_not_call_order():
    """Chunks run under asyncio.gather, so completion order need not match chunk order:
    the last-appended record must not be assumed to be the one just parsed."""
    details = init_run_details()
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=1, sample_index=None)  # appended first
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=0, sample_index=None)  # appended second

    set_call_findings("review", 0, None, 5)

    assert details.calls[0].findings_emitted is None
    assert details.calls[1].findings_emitted == 5


def test_set_call_findings_returns_false_when_no_call_matches():
    init_run_details()
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=0, sample_index=None)

    assert set_call_findings("review", 99, None, 3) is False
    assert set_call_findings("verify", 0, None, 3) is False
    assert set_call_findings("review", 0, 5, 3) is False


def test_set_call_findings_does_not_overwrite_an_already_set_record():
    """A retried chunk reuses the same (stage, chunk_index, sample_index) triple as its
    failed predecessor; the first call to reach a findings count wins."""
    details = init_run_details()
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=0, sample_index=None)
    record_ai_call(_Usage(1, 1, 2), stage="review", chunk_index=0, sample_index=None)  # the retry

    assert set_call_findings("review", 0, None, 2) is True
    assert set_call_findings("review", 0, None, 7) is True  # falls through to the second record

    assert details.calls[0].findings_emitted == 2
    assert details.calls[1].findings_emitted == 7


def test_set_call_findings_returns_false_when_not_initialized():
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    try:
        assert set_call_findings("review", 0, None, 3) is False
    finally:
        run_details._run_details.reset(token)
