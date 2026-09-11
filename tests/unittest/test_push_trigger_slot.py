import asyncio

import pytest

from pr_agent.servers import utils as servers_utils


@pytest.fixture(autouse=True)
def reset_push_trigger_state(monkeypatch):
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})


@pytest.mark.asyncio
async def test_push_trigger_slot_discards_duplicates_without_backlog():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def first_run():
        async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300) as proceed:
            assert proceed is True
            first_entered.set()
            await release_first.wait()

    first = asyncio.create_task(first_run())
    await first_entered.wait()

    async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300) as proceed:
        assert proceed is False

    release_first.set()
    await first


@pytest.mark.asyncio
async def test_push_trigger_slot_keeps_one_backlog_delegate():
    order = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def run(name):
        async with servers_utils.push_trigger_slot("pr", allow_backlog=True, ttl=300) as proceed:
            if not proceed:
                order.append(f"{name}-skipped")
                return
            order.append(name)
            if name == "first":
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(run("first"))
    await first_entered.wait()
    second = asyncio.create_task(run("second"))
    await asyncio.sleep(0)
    await run("third")
    assert order == ["first", "third-skipped"]

    release_first.set()
    await asyncio.gather(first, second)
    assert order == ["first", "third-skipped", "second"]


@pytest.mark.asyncio
async def test_push_trigger_slot_releases_cancelled_waiter():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def run():
        async with servers_utils.push_trigger_slot("pr", allow_backlog=True, ttl=300) as proceed:
            assert proceed is True
            if not first_entered.is_set():
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(run())
    await first_entered.wait()
    waiter = asyncio.create_task(run())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    state = servers_utils._get_push_trigger_state("pr", 300)
    assert state.active_tasks == 1
    release_first.set()
    await first
    assert state.active_tasks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_push_trigger_slot_releases_failed_or_cancelled_runner(cancel):
    entered = asyncio.Event()
    release = asyncio.Event()
    followed_up = asyncio.Event()

    async def first_run():
        async with servers_utils.push_trigger_slot("pr", allow_backlog=True, ttl=300) as proceed:
            assert proceed
            entered.set()
            await release.wait()
            raise RuntimeError("command failed")

    async def follow_up():
        async with servers_utils.push_trigger_slot("pr", allow_backlog=True, ttl=300) as proceed:
            assert proceed
            followed_up.set()

    first = asyncio.create_task(first_run())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(follow_up())
    await asyncio.sleep(0)
    if cancel:
        first.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await first
    await asyncio.wait_for(second, 1)
    assert followed_up.is_set()
    assert not servers_utils._active_push_trigger_states
    async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300) as proceed:
        assert proceed


@pytest.mark.asyncio
async def test_push_trigger_slot_does_not_block_other_prs():
    async with servers_utils.push_trigger_slot("first-pr", allow_backlog=False, ttl=300) as first:
        async with servers_utils.push_trigger_slot("second-pr", allow_backlog=False, ttl=300) as second:
            assert first and second
    assert not servers_utils._active_push_trigger_states


@pytest.mark.asyncio
async def test_push_trigger_slot_keeps_active_queue_after_ttl_expiry(monkeypatch):
    now = [0]
    monkeypatch.setattr(servers_utils.DefaultDictWithTimeout,
                        "_DefaultDictWithTimeout__time", staticmethod(lambda: now[0]))
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def run(name):
        async with servers_utils.push_trigger_slot("pr", allow_backlog=True, ttl=300) as proceed:
            if not proceed:
                order.append(f"{name}-skipped")
                return
            order.append(name)
            if name == "first":
                entered.set()
                await release.wait()

    first = asyncio.create_task(run("first"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(run("second"))
    await asyncio.sleep(0)
    now[0] = 301
    # Another PR sweeps expired cache entries while the original PR is running.
    async with servers_utils.push_trigger_slot("other", allow_backlog=True, ttl=300) as proceed:
        assert proceed
    await run("third")
    assert order == ["first", "third-skipped"]
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert order == ["first", "third-skipped", "second"]
    assert not servers_utils._active_push_trigger_states


@pytest.mark.asyncio
async def test_push_trigger_slot_keeps_same_queue_when_ttl_changes():
    async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300) as first:
        assert first
        async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=600) as duplicate:
            assert not duplicate


@pytest.mark.asyncio
async def test_push_trigger_slot_expires_idle_state(monkeypatch):
    now = [0]
    monkeypatch.setattr(servers_utils.DefaultDictWithTimeout, "_DefaultDictWithTimeout__time",
                        staticmethod(lambda: now[0]))
    async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300):
        previous = servers_utils._active_push_trigger_states["pr"]
    now[0] = 301
    async with servers_utils.push_trigger_slot("pr", allow_backlog=False, ttl=300) as proceed:
        assert proceed
        assert servers_utils._active_push_trigger_states["pr"] is not previous
