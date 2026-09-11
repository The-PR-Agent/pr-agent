import asyncio
import multiprocessing
import random
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.servers import github_polling


@pytest.fixture
def workers(monkeypatch):
    created = []

    class FakeProcess:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.pid = None
            self.alive = False
            self.closed = False
            self.joined = False
            created.append(self)

        def start(self):
            self.pid = len(created)
            self.alive = True
            assert sum(p.alive for p in created) <= 10

        def is_alive(self):
            assert not self.closed
            return self.alive

        def join(self, timeout):
            assert timeout == 0
            assert not self.alive
            self.joined = True

        def close(self):
            assert not self.alive
            self.closed = True

    # Replace the module reference, not multiprocessing.Process process-wide.
    monkeypatch.setattr(github_polling, "multiprocessing", SimpleNamespace(Process=FakeProcess))
    monkeypatch.setattr(github_polling, "get_logger", MagicMock())
    return created, FakeProcess


def _queue(size):
    return deque((time.sleep, (i,)) for i in range(size))


@pytest.mark.asyncio
async def test_start_queued_processes_respects_parallel_limit(workers):
    created, _ = workers
    active = []
    queue = _queue(12)
    await github_polling._start_queued_processes(queue, 10, active)
    assert len(created) == len(active) == 10
    assert all(process.alive for process in active)
    assert [process.args for process in active] == [(i,) for i in range(10)]
    assert not queue


@pytest.mark.asyncio
async def test_next_batch_waits_without_dropping_accepted_work(workers, monkeypatch):
    created, _ = workers
    active = []
    await github_polling._start_queued_processes(_queue(10), 10, active)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_capacity(delay):
        assert delay == 0.25
        waiting.set()
        await release.wait()

    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=wait_for_capacity))
    queue = _queue(3)
    task = asyncio.create_task(github_polling._start_queued_processes(queue, 10, active))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert len(created) == 10
        assert len(queue) == 3
        for process in created[:3]:
            process.alive = False
        release.set()
        await asyncio.wait_for(task, 2)
        assert len(created) == 13
        assert len(active) == 10
        assert all(p.joined and p.closed for p in created[:3])
        assert not queue
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_capacity_wait_does_not_start_or_discard_work(workers, monkeypatch):
    created, _ = workers
    active = []
    await github_polling._start_queued_processes(_queue(10), 10, active)
    entered = asyncio.Event()

    async def wait_for_capacity(delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=wait_for_capacity))
    queue = _queue(2)
    task = asyncio.create_task(github_polling._start_queued_processes(queue, 10, active))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task
        assert len(created) == len(active) == 10
        assert len(queue) == 2
        github_polling.get_logger().error.assert_called_once_with(
            "Polling dispatch stopped with 2 tasks not dispatched"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_start", [False, True])
async def test_start_failure_keeps_previous_workers_tracked(workers, monkeypatch, partial_start):
    created, process_type = workers
    start = process_type.start

    def fail_second(process):
        if len(created) == 2:
            if partial_start:
                start(process)
            raise OSError("Cannot start worker")
        start(process)

    monkeypatch.setattr(process_type, "start", fail_second)
    active = []
    queue = _queue(3)
    with pytest.raises(github_polling._PollingWorkerStartError, match="startup failed") as failure:
        await github_polling._start_queued_processes(queue, 10, active)
    assert isinstance(failure.value.__cause__, OSError)
    assert active == (created if partial_start else created[:1])
    assert created[0].alive
    assert created[1].closed is (not partial_start)
    expected_remaining = 1 if partial_start else 2
    assert len(queue) == expected_remaining
    github_polling.get_logger().error.assert_called_once_with(
        f"Polling dispatch stopped with {expected_remaining} tasks not dispatched"
    )
    assert len(created) == 2


@pytest.mark.asyncio
async def test_many_batches_keep_the_active_limit(workers, monkeypatch):
    created, _ = workers
    active = []
    rng = random.Random(42)
    accepted = 0

    async def complete_workers(delay):
        for process in active[:3]:
            process.alive = False

    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=complete_workers))
    for _ in range(100):
        size = rng.randint(1, 12)
        accepted += min(size, 10)
        await github_polling._start_queued_processes(_queue(size), 10, active)
        assert len(active) <= 10
        for process in active[:rng.randint(0, len(active))]:
            process.alive = False
    assert len(created) == accepted
    for process in active:
        process.alive = False
    github_polling._reap_finished_processes(active)
    assert not active
    assert all(p.joined and p.closed for p in created)


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_response", [304, 200, 500, OSError("poll failed")])
async def test_polling_loop_reaps_workers_on_idle_and_failed_polls(workers, monkeypatch, idle_response):
    created, _ = workers
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    monkeypatch.setattr(github_polling, "mark_notification_as_read", AsyncMock())
    monkeypatch.setattr(github_polling, "is_valid_notification", AsyncMock(return_value=(
        True, set(), {"id": 2}, "@bot /review", "https://example.test/pull/1", "@bot"
    )))
    finished = asyncio.Event()
    polls = 0
    sleeps = 0

    class Response:
        def __init__(self, status):
            self.status = status
            self.headers = {}

        async def __aenter__(self):
            if isinstance(self.status, Exception):
                raise self.status
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return [{"id": 1}] if polls == 1 else []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            nonlocal polls
            polls += 1
            return Response(200 if polls == 1 else idle_response)

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            created[0].alive = False
        if sleeps == 3:
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(github_polling.polling_loop())
    try:
        await asyncio.wait_for(finished.wait(), 2)
        assert len(created) == 1
        assert created[0].joined and created[0].closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reap_real_spawned_worker(monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    monkeypatch.setattr(github_polling, "multiprocessing", SimpleNamespace(Process=ctx.Process))
    active = []
    await github_polling._start_queued_processes(deque([(time.sleep, (0,))]), 1, active)
    process = active[0]
    try:
        await asyncio.to_thread(process.join, 5)
        assert not process.is_alive()
        github_polling._reap_finished_processes(active)
        assert not active
        with pytest.raises(ValueError, match="closed"):
            process.is_alive()
    finally:
        if active:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            process.close()


@pytest.mark.asyncio
async def test_polling_loop_shares_capacity_between_batches(workers, monkeypatch):
    created, _ = workers
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    monkeypatch.setattr(github_polling, "mark_notification_as_read", AsyncMock())
    monkeypatch.setattr(github_polling, "is_valid_notification", AsyncMock(return_value=(
        True, set(), {"id": 2}, "@bot /review", "https://example.test/pull/1", "@bot"
    )))
    waiting = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    polls = 0

    class Session:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            nonlocal polls
            polls += 1
            return self

        async def json(self):
            return [{"id": i} for i in range(10 if polls == 1 else 2)]

    async def sleep(delay):
        if delay == 0.25:
            waiting.set()
            await release.wait()
        elif polls == 2:
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(github_polling.polling_loop())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert len(created) == 10
        assert polls == 2
        for process in created[:2]:
            process.alive = False
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert len(created) == 12
        assert sum(process.alive for process in created) == 10
    finally:
        task.cancel()
        result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_polling_loop_logs_accepted_work_cancelled_before_dispatch(workers, monkeypatch):
    created, _ = workers
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    monkeypatch.setattr(github_polling, "mark_notification_as_read", AsyncMock())
    entered_second = asyncio.Event()
    validation_calls = 0

    async def validate(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return True, set(), {"id": 2}, "@bot /review", "https://example.test/pull/1", "@bot"
        entered_second.set()
        await asyncio.Event().wait()
        raise AssertionError("Second validation should remain blocked")

    monkeypatch.setattr(github_polling, "is_valid_notification", validate)

    class Session:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return self

        async def json(self):
            return [{"id": 1}, {"id": 2}]

    async def sleep(delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(github_polling.polling_loop())
    await asyncio.wait_for(entered_second.wait(), 2)
    task.cancel()
    result, = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result, asyncio.CancelledError)
    assert not created
    github_polling.get_logger().error.assert_any_call(
        "Polling dispatch stopped with 1 tasks not dispatched"
    )


@pytest.mark.asyncio
async def test_polling_loop_stops_after_worker_start_failure(workers, monkeypatch):
    _, process_type = workers
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    monkeypatch.setattr(github_polling, "mark_notification_as_read", AsyncMock())
    monkeypatch.setattr(github_polling, "is_valid_notification", AsyncMock(return_value=(
        True, set(), {"id": 2}, "@bot /review", "https://example.test/pull/1", "@bot"
    )))
    start = MagicMock(side_effect=OSError("Cannot start worker"))
    monkeypatch.setattr(process_type, "start", start)

    class Session:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return self

        async def json(self):
            return [{"id": 1}]

    async def sleep(delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(github_polling._PollingWorkerStartError, match="startup failed"):
        await asyncio.wait_for(github_polling.polling_loop(), timeout=2)
    start.assert_called_once()
