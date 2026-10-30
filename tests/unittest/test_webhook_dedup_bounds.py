"""Bounded ``DefaultDictWithTimeout`` and GitHub webhook delivery deduplication."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.servers import github_app
from pr_agent.servers import utils as servers_utils
from pr_agent.servers.utils import DefaultDictWithTimeout


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(servers_utils.time, "monotonic", lambda: state["t"])
    return state


def _key_times(d):
    return d._DefaultDictWithTimeout__key_times


class TestBoundedDefaultDictWithTimeout:
    def test_expired_keys_are_purged_on_insert(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=2, refresh_interval=5, max_size=3)
        d["a"] = 1
        d["b"] = 2
        fake_clock["t"] += 10
        d["c"] = 3
        assert set(d) == {"c"}
        assert set(_key_times(d)) == {"c"}

    def test_expired_keys_are_purged_on_access(self, fake_clock):
        d = DefaultDictWithTimeout(int, ttl=2, refresh_interval=5, max_size=3)
        d["a"] = 1
        fake_clock["t"] += 10
        assert d["b"] == 0
        assert "a" not in d

    def test_fifo_eviction_of_oldest_entries(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=1000, refresh_interval=1000, max_size=5, update_key_time_on_get=False)
        for i in range(8):
            fake_clock["t"] += 1
            d[i] = i
        assert list(d) == [3, 4, 5, 6, 7]
        assert list(_key_times(d)) == [3, 4, 5, 6, 7]

    def test_reads_do_not_reorder_without_update_on_get(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=1000, refresh_interval=1000, max_size=3, update_key_time_on_get=False)
        for key in "abc":
            d[key] = 0
        _ = d["a"]
        d["d"] = 0
        assert set(d) == {"b", "c", "d"}

    def test_recently_read_key_survives_eviction(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=1000, refresh_interval=1000, max_size=3)
        for key in "abc":
            d[key] = 0
        _ = d["a"]
        d["d"] = 0
        assert set(d) == {"a", "c", "d"}

    def test_overwriting_existing_key_does_not_evict(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=1000, refresh_interval=1000, max_size=3)
        for key in "abc":
            d[key] = 0
        d["a"] = 1
        assert set(d) == {"a", "b", "c"}
        d["d"] = 0
        assert set(d) == {"a", "c", "d"}

    @pytest.mark.parametrize("insert", ["factory", "setdefault"])
    def test_factory_and_setdefault_inserts_are_bounded(self, fake_clock, insert):
        d = DefaultDictWithTimeout(int, ttl=1000, refresh_interval=1000, max_size=100)
        for i in range(250):
            if insert == "factory":
                _ = d[i]
            else:
                d.setdefault(i, 0)
        assert len(d) == 100
        assert list(d) == list(range(150, 250))
        assert len(_key_times(d)) == 100

    def test_max_size_none_is_unbounded(self, fake_clock):
        d = DefaultDictWithTimeout(ttl=1000, refresh_interval=1000, max_size=None)
        for i in range(20_001):
            d[i] = i
        assert len(d) == 20_001

    def test_default_max_size_is_ten_thousand(self, fake_clock):
        d = DefaultDictWithTimeout(float, ttl=1000, refresh_interval=1000)
        for i in range(10_001):
            d[i] = 1.0
        assert len(d) == 10_000
        assert 0 not in d


@pytest.fixture
def dedup(monkeypatch):
    settings = copy.deepcopy(get_settings())
    settings.set("GITHUB_APP.WEBHOOK_DELIVERY_DEDUPLICATION", True)
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})
    monkeypatch.setattr(
        github_app,
        "_completed_webhook_deliveries",
        DefaultDictWithTimeout(float, ttl=github_app._WEBHOOK_DELIVERY_TTL, update_key_time_on_get=False),
    )
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(github_app, "_dispatch_request", dispatch)
    with request_cycle_context({"settings": settings}):
        yield dispatch


BODY = {"action": "opened"}


async def test_duplicate_suppressed_while_in_flight(dedup):
    release = asyncio.Event()

    async def slow_dispatch(*args):
        await release.wait()
        return True

    dedup.side_effect = slow_dispatch
    first = asyncio.create_task(github_app.handle_request(BODY, "pull_request", "d-1"))
    await asyncio.sleep(0)
    await github_app.handle_request(BODY, "pull_request", "d-1")
    assert dedup.await_count == 1
    release.set()
    await first
    assert dedup.await_count == 1


async def test_duplicate_suppressed_after_completion(dedup):
    await github_app.handle_request(BODY, "pull_request", "d-1")
    await github_app.handle_request(BODY, "pull_request", "d-1")
    assert dedup.await_count == 1
    await github_app.handle_request(BODY, "pull_request", "d-2")
    assert dedup.await_count == 2


async def test_failed_delivery_releases_slot_for_retry(dedup):
    dedup.side_effect = [False, True, True]
    await github_app.handle_request(BODY, "pull_request", "d-1")
    assert github_app._completed_webhook_deliveries.get("d-1", 0.0) == 0.0
    assert not servers_utils._active_push_trigger_states
    await github_app.handle_request(BODY, "pull_request", "d-1")
    await github_app.handle_request(BODY, "pull_request", "d-1")
    assert dedup.await_count == 2
