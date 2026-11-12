"""The staggered first chunk: cache-aware dispatch in run_chunk_batch (#4876)."""

import asyncio

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.chunk_runner import run_chunk_batch


def _recorder(order: list, fail_first: bool = False):
    async def call(index):
        order.append(("start", index))
        await asyncio.sleep(0.01)
        if fail_first and index == 0:
            raise ValueError("first chunk failed")
        order.append(("end", index))
        return index

    return call


async def test_stagger_first_chunk_settles_before_siblings_start():
    order = []
    results = await run_chunk_batch([0, 1, 2], _recorder(order), stagger_first=True)
    assert results == [0, 1, 2]
    assert order.index(("end", 0)) < order.index(("start", 1))
    assert order.index(("end", 0)) < order.index(("start", 2))
    # The siblings still run concurrently with each other.
    assert order.index(("start", 2)) < order.index(("end", 1))


async def test_without_stagger_all_chunks_start_before_any_ends():
    order = []
    results = await run_chunk_batch([0, 1, 2], _recorder(order))
    assert results == [0, 1, 2]
    assert order == [("start", 0), ("start", 1), ("start", 2), ("end", 0), ("end", 1), ("end", 2)]


async def test_stagger_survives_a_failed_first_chunk():
    order = []
    results = await run_chunk_batch([0, 1, 2], _recorder(order, fail_first=True), stagger_first=True)
    assert isinstance(results[0], ValueError)
    assert results[1:] == [1, 2]
    assert ("start", 2) in order


async def test_stagger_with_a_single_chunk_behaves_like_plain_gather():
    order = []
    results = await run_chunk_batch([0], _recorder(order), stagger_first=True)
    assert results == [0]
    assert order == [("start", 0), ("end", 0)]


def test_prompt_cache_active_requires_points_and_a_claude_model(monkeypatch):
    handler = LiteLLMAIHandler()
    points = [{"location": "message", "role": "system"}]
    monkeypatch.setattr(
        LiteLLMAIHandler, "_resolve_cache_control_injection_points", staticmethod(lambda: points)
    )
    assert handler.prompt_cache_active("claude-sonnet-5") is True
    assert handler.prompt_cache_active("gpt-4o") is False
    monkeypatch.setattr(
        LiteLLMAIHandler, "_resolve_cache_control_injection_points", staticmethod(lambda: None)
    )
    assert handler.prompt_cache_active("claude-sonnet-5") is False


def test_prompt_cache_active_rejects_a_prefix_below_the_model_minimum(monkeypatch):
    # Below prompt_cache_min_tokens Anthropic silently does not cache, so staggering
    # would only add latency; the predicate must say so (calibrated against the
    # call-time warning's rule).
    handler = LiteLLMAIHandler()
    points = [{"location": "message", "role": "system"}]
    monkeypatch.setattr(
        LiteLLMAIHandler, "_resolve_cache_control_injection_points", staticmethod(lambda: points)
    )
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.get_model_info",
        lambda model: {"prompt_cache_min_tokens": 1024},
    )
    assert handler.prompt_cache_active("claude-sonnet-5", prompt_tokens=500) is False
    assert handler.prompt_cache_active("claude-sonnet-5", prompt_tokens=5000) is True


def test_prompt_cache_active_skips_the_length_check_without_metadata_or_tokens(monkeypatch):
    handler = LiteLLMAIHandler()
    points = [{"location": "message", "role": "system"}]
    monkeypatch.setattr(
        LiteLLMAIHandler, "_resolve_cache_control_injection_points", staticmethod(lambda: points)
    )

    def explode(model):
        raise KeyError(model)

    monkeypatch.setattr("pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.get_model_info", explode)
    # A metadata gap or a missing token count never blocks the call-time behavior.
    assert handler.prompt_cache_active("claude-sonnet-5", prompt_tokens=500) is True
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.get_model_info",
        lambda model: {"prompt_cache_min_tokens": 1024},
    )
    assert handler.prompt_cache_active("claude-sonnet-5") is True
