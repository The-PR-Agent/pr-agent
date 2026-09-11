"""One review must not burst the provider (`pr_reviewer.max_concurrent_calls`).

Chunk fan-out nests sample fan-out, so without a cap a single review issues
max_number_of_calls x num_samples completions in one wave. A per-key rate limit answers that with
a 429, which the handler declines to retry, so the burst costs findings rather than time.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from pr_agent.algo.pr_processing import ChunkPlan
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer

REVIEW = yaml.safe_dump({"review": {"score": "90"}}, sort_keys=False)


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    return reviewer


@pytest.fixture
def fan_out(request):
    """num_samples x chunks, with a concurrency cap. Params: (samples, chunks, cap)."""
    samples, chunks, cap = getattr(request, "param", (3, 2, 4))
    settings = get_settings()
    keys = ("pr_reviewer.num_samples", "pr_reviewer.min_votes", "pr_reviewer.max_concurrent_calls",
            "pr_reviewer.max_number_of_calls", "pr_reviewer.enable_large_pr_chunking",
            "config.temperature")
    saved = {key: settings.get(key, None) for key in keys}
    settings.set("pr_reviewer.num_samples", samples)
    settings.set("pr_reviewer.min_votes", 1)
    settings.set("pr_reviewer.max_concurrent_calls", cap)
    settings.set("pr_reviewer.max_number_of_calls", chunks)
    settings.set("pr_reviewer.enable_large_pr_chunking", True)
    settings.set("config.temperature", 0.4)
    yield SimpleNamespace(samples=samples, chunks=chunks, cap=cap)
    for key, value in saved.items():
        settings.set(key, value)


async def _run_chunked(reviewer, chunks):
    """Drive the chunked+sampled flow, returning the peak number of calls held at once."""
    peak, live = 0, 0

    async def chat_completion(**kwargs):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)  # let every started call pile up before any finishes
        live -= 1
        return REVIEW, "stop"

    reviewer.ai_handler = SimpleNamespace(chat_completion=chat_completion)
    reviewer.vars = {"diff": ""}
    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.tools.pr_reviewer.get_pr_multi_diffs_with_files",
              return_value=([ChunkPlan(diff=f"chunk-{i}", files=(), clipped=()) for i in range(chunks)], [])),
        patch("pr_agent.tools.pr_reviewer.Environment") as environment,
    ):
        environment.return_value.from_string.return_value.render.return_value = "prompt"
        await reviewer._prepare_prediction("model")
    return peak


@pytest.mark.asyncio
@pytest.mark.parametrize("fan_out", [(3, 2, 2)], indirect=True)
async def test_the_cap_bounds_the_nested_chunk_and_sample_fan_out(fan_out):
    reviewer = _make_reviewer()
    peak = await _run_chunked(reviewer, fan_out.chunks)
    assert peak <= fan_out.cap
    assert reviewer.review_chunk_count == fan_out.chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("fan_out", [(3, 2, 0)], indirect=True)
async def test_a_cap_of_zero_leaves_the_calls_unbounded(fan_out):
    """The escape hatch, for a provider that would rather have the whole wave at once."""
    reviewer = _make_reviewer()
    peak = await _run_chunked(reviewer, fan_out.chunks)
    assert peak == fan_out.samples * fan_out.chunks


def test_the_default_cap_is_above_the_shipped_defaults_peak():
    """The cap must change nothing until either fan-out knob is raised."""
    settings = get_settings().pr_reviewer
    shipped_peak = int(settings.max_number_of_calls) * int(settings.num_samples)
    assert int(settings.max_concurrent_calls) >= shipped_peak


def test_a_malformed_cap_falls_back_to_the_default_rather_than_failing_the_review():
    settings = get_settings()
    saved = settings.get("pr_reviewer.max_concurrent_calls", None)
    try:
        settings.set("pr_reviewer.max_concurrent_calls", "many")
        semaphore = PRReviewer._build_call_semaphore()
        assert isinstance(semaphore, asyncio.Semaphore)
    finally:
        settings.set("pr_reviewer.max_concurrent_calls", saved)
