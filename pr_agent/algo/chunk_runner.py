"""Share one chunk runner between the chunked `/review` and `/improve` runs.

Both tools split a too-large diff with `get_pr_multi_diffs` and call the model once
per chunk, but each used to run its own dispatch loop with its own failure handling.
This runner owns the part the two runs genuinely share: starting one model call per
chunk (concurrently, or one at a time when the caller asks), advancing the progress
reporter as each chunk settles, and returning the results aligned with the chunks
so the caller keeps the chunks that succeeded and sees the ones that failed as
values instead of losing the batch to the first error.

What the runner deliberately does *not* own is the retry policy, because that is
where the two tools differ today and the maintainer asked to keep it that way
(#4725): `/review` retries failed chunks on the next fallback model by re-entering
its chunked preparation under the outer fallback loop, resuming from the cached
successful chunks; `/improve` recovers failed chunks on the remaining models inside
the same attempt, re-preparing each chunk against the fallback model's token
budget. Each tool keeps what it publishes and reports as failed when chunks fail;
this runner only hands it the aligned results to decide from.

A chunk that raises a `BaseException` which is not an `Exception` (cancellation,
keyboard interrupt) is re-raised after its siblings settle, so a cancelled run
still waits for the in-flight chunks instead of abandoning them mid-call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol


class ChunkProgressSink(Protocol):
    """The progress-reporting half of a chunked run: advance one settled chunk."""

    async def record_settled(self, count: int = 1) -> None:
        """Advance the progress line by `count` settled chunks."""


async def run_chunk_batch[T, R](
    items: Iterable[T],
    call: Callable[[T], Awaitable[R]],
    *,
    parallel: bool = True,
    reporter: ChunkProgressSink | None = None,
) -> list[R | Exception]:
    """Run one model call per chunk and return the results aligned with the chunks.

    Each entry is the call's return value, or the `Exception` it raised; a failed
    chunk never aborts the batch. Set `parallel=False` to run the chunks one at a
    time, in order, still continuing past a failed chunk. Every settled chunk
    advances `reporter` when one is given.
    """

    async def _run_one(item: T) -> R:
        try:
            return await call(item)
        finally:
            if reporter is not None:
                await reporter.record_settled()

    if parallel:
        results = list(await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True))
    else:
        results = []
        for item in items:
            try:
                results.append(await _run_one(item))
            except Exception as error:
                results.append(error)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
    return results
