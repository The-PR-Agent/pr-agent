"""GitLab diff-readiness backoff stays monkeypatchable and off the event loop when offloaded to a thread."""
import asyncio
import threading
import time

import pytest

from pr_agent.git_providers import gitlab_provider
from pr_agent.git_providers.gitlab_provider import GitLabDiffNotReadyError, GitLabProvider, IncompleteGitLabDiffError

READY_CHANGES = {"changes_count": "1", "changes": []}


def _provider(not_ready_attempts):
    """A provider whose diff collection reports not-ready for the first ``not_ready_attempts`` calls."""
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/repo"
    provider.id_mr = 7
    provider.collect_calls = 0

    def collect(path):
        provider.collect_calls += 1
        if provider.collect_calls <= not_ready_attempts:
            raise GitLabDiffNotReadyError("diff not ready")
        return dict(READY_CHANGES)

    provider._collect_merge_request_changes = collect
    return provider


def test_backoff_keeps_default_delays_and_sync_sleep():
    assert gitlab_provider._DIFF_READY_RETRY_DELAYS == (2, 5, 10)
    assert gitlab_provider._diff_ready_sleep is time.sleep


def test_monkeypatched_sleep_receives_each_retry_delay(monkeypatch):
    delays = []
    monkeypatch.setattr(gitlab_provider, "_diff_ready_sleep", delays.append)
    provider = _provider(not_ready_attempts=len(gitlab_provider._DIFF_READY_RETRY_DELAYS))

    assert provider._get_merge_request_changes() == READY_CHANGES
    assert delays == list(gitlab_provider._DIFF_READY_RETRY_DELAYS)


def test_monkeypatched_sleep_exhausts_retry_budget(monkeypatch):
    delays = []
    monkeypatch.setattr(gitlab_provider, "_diff_ready_sleep", delays.append)
    provider = _provider(not_ready_attempts=99)

    with pytest.raises(IncompleteGitLabDiffError, match="re-run the command"):
        provider._get_merge_request_changes()
    assert delays == list(gitlab_provider._DIFF_READY_RETRY_DELAYS)


async def test_offloaded_backoff_sleeps_off_the_loop_and_keeps_it_responsive(monkeypatch):
    loop_thread = threading.get_ident()
    sleep_threads = []

    def short_sleep(delay):
        sleep_threads.append(threading.get_ident())
        time.sleep(0.05)

    monkeypatch.setattr(gitlab_provider, "_DIFF_READY_RETRY_DELAYS", (1, 1, 1))
    monkeypatch.setattr(gitlab_provider, "_diff_ready_sleep", short_sleep)
    provider = _provider(not_ready_attempts=3)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    try:
        changes = await asyncio.to_thread(provider._get_merge_request_changes)
    finally:
        ticker_task.cancel()

    assert changes == READY_CHANGES
    assert len(sleep_threads) == 3
    assert loop_thread not in sleep_threads
    # ~150 ms of blocking sleeps ran while the loop kept ticking every ~5 ms.
    assert ticks >= 10
