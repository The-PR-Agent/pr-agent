"""The shared chunk runner both chunked tools run through.

Covers the dispatch the runner owns: concurrent and sequential runs, keeping
successful chunks when one fails, advancing the progress reporter as chunks
settle, and re-raising a cancelled chunk only after its siblings settle.
The per-tool failure policies (review's attempt-level fallback loop, improve's
in-attempt recovery) stay in their own test files.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from pr_agent.algo.chunk_runner import run_chunk_batch


def _reporter():
    reporter = AsyncMock()
    reporter.record_settled = AsyncMock()
    return reporter


async def test_parallel_run_keeps_order_and_failed_chunks_as_values():
    calls = []

    async def call(item):
        calls.append(item)
        if item == "b":
            raise RuntimeError("chunk b failed")
        return f"done-{item}"

    results = await run_chunk_batch(["a", "b", "c"], call)

    assert results[0] == "done-a"
    assert isinstance(results[1], RuntimeError) and str(results[1]) == "chunk b failed"
    assert results[2] == "done-c"
    assert sorted(calls) == ["a", "b", "c"]


async def test_sequential_run_continues_past_a_failed_chunk_in_order():
    calls = []

    async def call(item):
        calls.append(item)
        if item == "b":
            raise RuntimeError("chunk b failed")
        return f"done-{item}"

    results = await run_chunk_batch(["a", "b", "c"], call, parallel=False)

    assert calls == ["a", "b", "c"]
    assert results[0] == "done-a"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "done-c"


async def test_parallel_run_propagates_cancellation_after_siblings_settle():
    finished = asyncio.Event()

    async def call(item):
        if item == "b":
            raise asyncio.CancelledError
        await asyncio.sleep(0.01)
        finished.set()
        return "done"

    with pytest.raises(asyncio.CancelledError):
        await run_chunk_batch(["a", "b"], call)

    assert finished.is_set()


async def test_sequential_cancellation_stops_later_chunks():
    calls = []

    async def call(item):
        calls.append(item)
        if item == "a":
            raise asyncio.CancelledError
        return "done"

    with pytest.raises(asyncio.CancelledError):
        await run_chunk_batch(["a", "b"], call, parallel=False)

    assert calls == ["a"]


async def test_reporter_advances_once_per_settled_chunk():
    reporter = _reporter()

    async def call(item):
        if item == "b":
            raise RuntimeError("chunk b failed")
        return "done"

    await run_chunk_batch(["a", "b", "c"], call, reporter=reporter)
    assert reporter.record_settled.await_count == 3

    reporter = _reporter()
    await run_chunk_batch(["a", "b"], call, parallel=False, reporter=reporter)
    assert reporter.record_settled.await_count == 2


async def test_run_without_reporter_and_without_items():
    async def identity(item):
        return item

    assert await run_chunk_batch([], identity) == []
    assert await run_chunk_batch(["a"], lambda item: identity("done")) == ["done"]
