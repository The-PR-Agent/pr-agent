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
    github_polling.get_logger().info.assert_not_called()


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
        github_polling.get_logger().info.assert_called_once_with(
            "Polling dispatch waiting for capacity: 10 workers active, 3 tasks queued"
        )
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
async def test_capacity_wait_logs_once_per_batch(workers, monkeypatch):
    active = []
    await github_polling._start_queued_processes(_queue(10), 10, active)
    info = github_polling.get_logger().info
    info.assert_not_called()
    capacity_checks = 0

    async def release_capacity(delay):
        nonlocal capacity_checks
        assert delay == github_polling.POLLING_CAPACITY_CHECK_INTERVAL
        capacity_checks += 1
        if capacity_checks % 3 == 0:
            active[0].alive = False

    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=release_capacity))
    for batch in range(2):
        await github_polling._start_queued_processes(_queue(2), 10, active)
        assert info.call_count == batch + 1
        info.assert_called_with("Polling dispatch waiting for capacity: 10 workers active, 2 tasks queued")
    assert capacity_checks == 12
    await github_polling._start_queued_processes(_queue(0), 10, active)
    assert info.call_count == 2


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
async def test_polling_loop_keeps_drifted_notification_unread_for_unconditional_retry(monkeypatch):
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    mark_read = AsyncMock()
    monkeypatch.setattr(github_polling, "mark_notification_as_read", mark_read)
    monkeypatch.setattr(
        github_polling,
        "is_valid_notification",
        AsyncMock(side_effect=[
            (False, set(), github_polling._RETRY_POLLING_NOTIFICATION),
            (False, set()),
        ]),
    )
    notification = {"id": 1}
    requests = []
    finished = asyncio.Event()

    class Session:
        status = 200
        headers = {"Last-Modified": "retry-test"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            requests.append(kwargs)
            return self

        async def json(self):
            return [notification]

    async def sleep(_delay):
        if len(requests) >= 2:
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(github_polling.polling_loop())
    try:
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    mark_read.assert_awaited_once()
    assert mark_read.await_args.args[1] == notification
    assert len(requests) == 2
    assert "If-Modified-Since" not in requests[1]["headers"]


@pytest.mark.asyncio
async def test_polling_loop_keeps_notification_unread_when_drift_retry_times_out(monkeypatch):
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    mark_read = AsyncMock()
    monkeypatch.setattr(github_polling, "mark_notification_as_read", mark_read)
    scan_calls = 0

    async def scan(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            return None
        raise asyncio.TimeoutError("retry deadline exhausted")

    monkeypatch.setattr(github_polling, "_fetch_comment_history_scan", scan)
    notification = {
        "id": 1,
        "reason": "mention",
        "subject": {
            "type": "PullRequest",
            "url": "https://example.test/repos/owner/repo/pulls/1",
            "latest_comment_url": "https://example.test/latest",
        },
    }
    notification_requests = []
    finished = asyncio.Event()

    class Response:
        status = 200

        def __init__(self, body, headers=None):
            self.body = body
            self.headers = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return self.body

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            if url == github_polling.NOTIFICATION_URL:
                notification_requests.append(kwargs)
                return Response([notification], {"Last-Modified": "unchanged"})
            assert url == notification["subject"]["latest_comment_url"]
            return Response({"id": 99, "body": "Other discussion", "user": {"login": "human"}})

    async def sleep(_delay):
        if notification_requests:
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(
        github_polling,
        "asyncio",
        SimpleNamespace(
            sleep=sleep,
            get_running_loop=asyncio.get_running_loop,
            TimeoutError=asyncio.TimeoutError,
        ),
    )
    task = asyncio.create_task(github_polling.polling_loop())
    try:
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert scan_calls == 2
    assert len(notification_requests) == 1
    mark_read.assert_not_awaited()


@pytest.mark.parametrize("valid_command", [True, False], ids=["valid-command", "invalid-notification"])
@pytest.mark.asyncio
async def test_polling_loop_rolls_back_validation_ids_when_mark_read_fails(monkeypatch, valid_command):
    settings = SimpleNamespace(
        github=SimpleNamespace(deployment_type="user", user_token="test-token"), set=MagicMock()
    )
    monkeypatch.setattr(github_polling, "get_settings", lambda: settings)
    monkeypatch.setattr(
        github_polling,
        "get_git_provider",
        lambda: lambda: SimpleNamespace(get_user_id=lambda: "bot"),
    )
    monkeypatch.setattr(github_polling, "get_logger", MagicMock())
    preexisting_notification = {"id": 42}
    notification = {"id": 1}
    comment = {"id": 99}
    large_preexisting_ids = set(range(10_000, 20_000))
    handled_at_validation = []
    handled_references = []

    class NonCopyingSet(set):
        def copy(self):
            raise AssertionError("polling must not copy the full handled-ID history")

    monkeypatch.setattr(github_polling, "set", NonCopyingSet, raising=False)

    async def validate(
        _notification, _headers, handled_ids, _session, _user_id, added_handled_ids=None
    ):
        if _notification is preexisting_notification:
            handled_ids.update(large_preexisting_ids)
            return False, handled_ids
        handled_at_validation.append((len(handled_ids), large_preexisting_ids <= handled_ids))
        handled_references.append(handled_ids)
        assert comment["id"] not in handled_ids
        handled_ids.add(comment["id"])
        added_handled_ids.add(comment["id"])
        if valid_command:
            return True, handled_ids, comment, "@bot /review", "https://example.test/pull/1", "@bot"
        return False, handled_ids

    monkeypatch.setattr(github_polling, "is_valid_notification", validate)
    mark_attempts = 0

    async def mark_read(_headers, _notification, _session):
        nonlocal mark_attempts
        mark_attempts += 1
        if mark_attempts == 2:
            raise RuntimeError("temporary PATCH failure")

    monkeypatch.setattr(github_polling, "mark_notification_as_read", mark_read)
    requests = []
    dispatched = []
    finished = asyncio.Event()

    class Response:
        status = 200
        headers = {"Last-Modified": "unchanged-notification"}

        def __init__(self, notifications):
            self.notifications = notifications

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return self.notifications

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            requests.append(kwargs)
            notifications = [preexisting_notification, notification] if len(requests) == 1 else [notification]
            return Response(notifications)

    async def start_queued(task_queue, _limit, _active_processes):
        dispatched.extend(task_queue)
        finished.set()

    async def sleep(_delay):
        if len(requests) >= 2:
            if not valid_command:
                finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(github_polling, "_start_queued_processes", start_queued)
    monkeypatch.setattr(github_polling, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(github_polling, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(github_polling.polling_loop())
    try:
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert handled_at_validation == [(len(large_preexisting_ids) + 1, True)] * 2
    assert mark_attempts == 3
    assert len(requests) == 2
    assert "If-Modified-Since" not in requests[1]["headers"]
    assert len(dispatched) == int(valid_command)
    assert large_preexisting_ids <= handled_references[-1]
    assert {preexisting_notification["id"], notification["id"], comment["id"]} <= handled_references[-1]


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
