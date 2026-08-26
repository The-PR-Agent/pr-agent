"""
Regression tests for issue #2378 — litellm success callbacks never fire from
pr-agent's async run loop.

LiteLLM defers async success logging twice: once via ``asyncio.create_task`` when
the completion resolves, and again when that task enqueues the callback onto a
module-global ``LoggingWorker``. Entry points that wrap a command in
``asyncio.run`` cancel both on teardown, so callbacks are silently dropped unless
we drain them first.
"""

import asyncio
import importlib
import inspect
import time
from types import FunctionType

import litellm
import pytest

from pr_agent.algo.ai_handlers import litellm_helpers
from pr_agent.config_loader import global_settings
from pr_agent.log import get_logger

_LITELLM_BATCH_CALLBACK_TYPES = litellm_helpers._LITELLM_BATCH_CALLBACK_TYPES
_LITELLM_CALLBACK_ATTRS = litellm_helpers._LITELLM_CALLBACK_ATTRS
_LITELLM_TEST_CALLBACK_ATTRS = (*_LITELLM_CALLBACK_ATTRS, "input_callback", "_async_input_callback")
_get_global_logging_worker = litellm_helpers._get_global_logging_worker
_is_litellm_task = litellm_helpers._is_litellm_task
drain_litellm_callbacks = litellm_helpers.drain_litellm_callbacks
litellm_callbacks_registered = litellm_helpers.litellm_callbacks_registered
_real_litellm_acompletion = litellm.acompletion
CANCELLATION_TIMING_TOLERANCE_SECONDS = 0.1
CANCELLATION_DEADLINE_TOLERANCE_SECONDS = 0.005


def _async_function_with_metadata(template, module, function, filename=None):
    code = template.__code__.replace(co_name=function)
    if filename is not None:
        code = code.replace(co_filename=filename)
    namespace = {"__name__": module, "asyncio": asyncio}
    return FunctionType(code, namespace, function, closure=template.__closure__)


def _record_cancellation_cleanup_deadlines(monkeypatch):
    cleanup_records = []
    for helper_name in ("_cancel_and_reap_tasks", "_reap_cancelled_tasks"):
        original_helper = getattr(litellm_helpers, helper_name)

        async def record_deadline(tasks, cleanup_timeout, *, _original_helper=original_helper, **kwargs):
            cleanup_records.append((asyncio.get_running_loop().time() + cleanup_timeout, cleanup_timeout))
            await _original_helper(tasks, cleanup_timeout, **kwargs)

        monkeypatch.setattr(litellm_helpers, helper_name, record_deadline)
    return cleanup_records


def _assert_cleanup_deadline_is_shared(cleanup_records):
    assert len(cleanup_records) >= 2
    _assert_cleanup_timeouts_are_bounded(cleanup_records)
    first_deadline, first_timeout = cleanup_records[0]
    assert first_timeout > 0
    assert all(
        abs(deadline - first_deadline) <= CANCELLATION_DEADLINE_TOLERANCE_SECONDS
        for deadline, cleanup_timeout in cleanup_records[1:]
        if cleanup_timeout > 0
    )


def _assert_cleanup_deadline_is_exhausted(cleanup_records):
    _assert_cleanup_deadline_is_shared(cleanup_records)
    assert all(cleanup_timeout == 0 for _, cleanup_timeout in cleanup_records[1:])


def _assert_cleanup_timeouts_are_bounded(cleanup_records):
    assert cleanup_records
    assert all(
        0 <= cleanup_timeout <= litellm_helpers.CANCELLATION_CLEANUP_SECONDS
        for _, cleanup_timeout in cleanup_records
    )


@pytest.fixture
def clean_litellm_callbacks():
    """Snapshot litellm's module-level callback lists and restore them afterwards."""
    snapshot = {attr: getattr(litellm, attr, None) for attr in _LITELLM_TEST_CALLBACK_ATTRS}
    enable_callbacks = global_settings.get("LITELLM.ENABLE_CALLBACKS", False)
    for attr in _LITELLM_TEST_CALLBACK_ATTRS:
        if snapshot[attr] is not None:
            setattr(litellm, attr, [])
    global_settings.set("LITELLM.ENABLE_CALLBACKS", False)
    yield
    for attr, value in snapshot.items():
        if value is not None:
            setattr(litellm, attr, value)
    global_settings.set("LITELLM.ENABLE_CALLBACKS", enable_callbacks)


class _CountingLogger(litellm.integrations.custom_logger.CustomLogger):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.calls += 1


async def _one_completion():
    await litellm.acompletion(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        mock_response="ok",
    )


def _cleanup_logging_worker_coroutines():
    worker = _get_global_logging_worker()
    if worker is None:
        return
    queue = getattr(worker, "_queue", None)
    if queue is None:
        return
    closed_coroutines = set()

    def close_if_idle(coroutine):
        coroutine_id = id(coroutine)
        if coroutine_id in closed_coroutines:
            return
        closed_coroutines.add(coroutine_id)
        if getattr(coroutine, "cr_frame", None) is None or getattr(coroutine, "cr_running", False):
            return
        try:
            coroutine.close()
        except RuntimeError:
            # Cleanup is best-effort if the coroutine starts running or rejects close().
            pass

    while True:
        try:
            queued_task = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        queued_coroutine = queued_task.get("coroutine") if isinstance(queued_task, dict) else queued_task
        close_if_idle(queued_coroutine)
        queue.task_done()


@pytest.fixture
def real_litellm_acompletion():
    assert litellm.acompletion is _real_litellm_acompletion
    yield
    _cleanup_logging_worker_coroutines()


# --- litellm_callbacks_registered ------------------------------------------------

def test_callbacks_not_registered_on_clean_state(clean_litellm_callbacks):
    assert litellm_callbacks_registered() is False


@pytest.mark.parametrize("empty_callbacks", [(), set()])
def test_empty_callback_containers_are_not_registered(clean_litellm_callbacks, monkeypatch, empty_callbacks):
    monkeypatch.setattr(litellm, "callbacks", empty_callbacks)
    assert litellm_callbacks_registered() is False


def test_programmatic_callbacks_are_detected(clean_litellm_callbacks):
    """The issue's repro path: callbacks set in code, configuration.toml untouched."""
    litellm.callbacks = [_CountingLogger()]
    assert litellm_callbacks_registered() is True


def test_config_flag_alone_is_enough(clean_litellm_callbacks):
    """Pre-existing behaviour: enable_callbacks=true still triggers a drain."""
    global_settings.set("LITELLM.ENABLE_CALLBACKS", True)
    assert litellm_callbacks_registered() is True


@pytest.mark.parametrize("attr", ["success_callback", "failure_callback", "service_callback"])
def test_string_callback_lists_are_detected(clean_litellm_callbacks, attr):
    setattr(litellm, attr, ["langsmith"])
    assert litellm_callbacks_registered() is True


# --- drain_litellm_callbacks -----------------------------------------------------

def test_callbacks_are_dropped_without_the_drain(clean_litellm_callbacks, real_litellm_acompletion):
    """Pins the regression: this is exactly what issue #2378 reports."""
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    asyncio.run(_one_completion())

    assert logger.calls == 0


def test_drain_delivers_callbacks_before_the_loop_closes(clean_litellm_callbacks, real_litellm_acompletion):
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    async def inner():
        await _one_completion()
        await drain_litellm_callbacks()

    asyncio.run(inner())

    assert logger.calls == 1


def test_drain_delivers_every_concurrent_callback(clean_litellm_callbacks, real_litellm_acompletion):
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    async def inner():
        await asyncio.gather(*(_one_completion() for _ in range(3)))
        await drain_litellm_callbacks()

    asyncio.run(inner())

    assert logger.calls == 3


def test_drain_does_not_wait_on_the_logging_worker(clean_litellm_callbacks, real_litellm_acompletion):
    """
    The worker's own loop task never completes. Waiting on it made every run with
    callbacks enabled stall for the full timeout; the drain must exclude it.
    """
    logger = _CountingLogger()
    litellm.callbacks = [logger]
    elapsed = {}

    async def inner():
        await _one_completion()
        loop = asyncio.get_running_loop()
        start = loop.time()
        await drain_litellm_callbacks(timeout=30)
        elapsed["seconds"] = loop.time() - start

    asyncio.run(inner())

    assert logger.calls == 1
    assert elapsed["seconds"] < 5


def test_unrelated_pending_tasks_do_not_delay_the_drain(clean_litellm_callbacks):
    """Background work that has nothing to do with litellm must not hold up exit."""
    elapsed = {}

    async def inner():
        stuck = asyncio.create_task(asyncio.sleep(30))
        loop = asyncio.get_running_loop()
        start = loop.time()
        await drain_litellm_callbacks(timeout=30)
        elapsed["seconds"] = loop.time() - start
        stuck.cancel()

    asyncio.run(inner())

    assert elapsed["seconds"] < 5


def test_drain_still_flushes_after_a_task_timeout(clean_litellm_callbacks, monkeypatch):
    """
    A stuck callback task must not cost us the callbacks already on the queue: the
    drain has to reach worker.flush() even once the task wait has timed out.
    """
    flushed = {"value": False}

    class _SpyWorker:
        _worker_task = None

        async def flush(self):
            flushed["value"] = True

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
        lambda: _SpyWorker(),
    )
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._is_litellm_task", lambda task: True)

    async def inner():
        stuck = asyncio.create_task(asyncio.sleep(30))
        await drain_litellm_callbacks(timeout=0.2)
        stuck.cancel()

    asyncio.run(inner())

    assert flushed["value"] is True


def test_drain_never_exceeds_the_configured_timeout(clean_litellm_callbacks, monkeypatch):
    """
    callback_timeout_seconds is documented as the max wait, so a slow flush on top
    of an already-exhausted task drain must not push the total past it.
    """
    class _SlowWorker:
        _worker_task = None

        async def flush(self):
            await asyncio.sleep(30)

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
        lambda: _SlowWorker(),
    )
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._is_litellm_task", lambda task: True)
    elapsed = {}

    async def inner():
        stuck = asyncio.create_task(asyncio.sleep(30))
        loop = asyncio.get_running_loop()
        start = loop.time()
        await drain_litellm_callbacks(timeout=0.5)
        elapsed["seconds"] = loop.time() - start
        stuck.cancel()

    asyncio.run(inner())

    # Both phases are slow, so this is the worst case: it must still fit the budget.
    assert elapsed["seconds"] <= 0.5 + 0.25, elapsed["seconds"]


def test_cancellation_cleanup_uses_remaining_callback_timeout(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        drain_started = asyncio.Event()
        drain_start = {}
        producer_started = asyncio.Event()
        release_producer = asyncio.Event()

        async def resistant_producer():
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_producer.wait()

        producer = _async_function_with_metadata(
            resistant_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        producer_task = asyncio.create_task(producer())
        loop = asyncio.get_running_loop()

        def get_worker():
            drain_start["time"] = loop.time()
            drain_started.set()
            return None

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", get_worker)
        monkeypatch.setattr(litellm_helpers, "CANCELLATION_CLEANUP_SECONDS", 1.0)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        timeout = 0.5
        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=timeout))
        await asyncio.wait_for(drain_started.wait(), timeout=1)
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        cleanup_records.clear()
        cancel_at = loop.time()
        drain_task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await drain_task
            elapsed = loop.time() - drain_start["time"]
            remaining = max(0.0, drain_start["time"] + timeout - cancel_at)
            deadline = drain_start["time"] + timeout

            assert cleanup_records
            bounded_cleanup_records = [record for record in cleanup_records if record[1] > 0]
            assert bounded_cleanup_records
            assert all(
                cleanup_deadline <= deadline + CANCELLATION_DEADLINE_TOLERANCE_SECONDS
                for cleanup_deadline, _ in bounded_cleanup_records
            )
            assert all(
                cleanup_timeout <= remaining + CANCELLATION_DEADLINE_TOLERANCE_SECONDS
                for _, cleanup_timeout in cleanup_records
            )
            assert elapsed <= timeout + CANCELLATION_TIMING_TOLERANCE_SECONDS
        finally:
            release_producer.set()
            await asyncio.gather(producer_task, return_exceptions=True)

    asyncio.run(run())


def test_batch_cancellation_cleanup_uses_overall_callback_timeout(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        drain_start = {}
        drain_task_holder = {}
        release_callback = asyncio.Event()

        class BlockingBatchCallback:
            def __init__(self):
                self.started = asyncio.Event()
                self.flush_task = None
                self.cancel_at = None

            async def flush_queue(self):
                self.flush_task = asyncio.current_task()
                self.started.set()
                self.cancel_at = loop.time()
                drain_task_holder["task"].cancel()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release_callback.wait()

        loop = asyncio.get_running_loop()

        def get_worker():
            drain_start["time"] = loop.time()
            return None

        callback = BlockingBatchCallback()
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", get_worker)
        monkeypatch.setattr(litellm_helpers, "CANCELLATION_CLEANUP_SECONDS", 2.0)
        monkeypatch.setattr(litellm, "callbacks", [callback])

        timeout = 1.0
        cleanup_records.clear()
        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=timeout))
        drain_task_holder["task"] = drain_task
        try:
            with pytest.raises(asyncio.CancelledError):
                await drain_task

            assert callback.started.is_set()
            assert callback.cancel_at is not None
            deadline = drain_start["time"] + timeout
            remaining = max(0.0, deadline - callback.cancel_at)
            assert 0 < remaining < litellm_helpers.CANCELLATION_CLEANUP_SECONDS
            assert cleanup_records
            bounded_cleanup_records = [record for record in cleanup_records if record[1] > 0]
            assert bounded_cleanup_records
            assert all(
                cleanup_deadline <= deadline + CANCELLATION_DEADLINE_TOLERANCE_SECONDS
                for cleanup_deadline, _ in bounded_cleanup_records
            )
            assert all(
                cleanup_timeout <= remaining + CANCELLATION_DEADLINE_TOLERANCE_SECONDS
                for _, cleanup_timeout in cleanup_records
            )
            assert bounded_cleanup_records[0][0] >= deadline - CANCELLATION_DEADLINE_TOLERANCE_SECONDS
        finally:
            release_callback.set()
            if callback.flush_task is not None:
                await asyncio.gather(callback.flush_task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.asyncio
async def test_batch_flush_skips_callback_attribute_lookup_failures(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class BatchCallback:
        def __init__(self):
            self.flushes = 0

        async def flush_queue(self):
            self.flushes += 1

    class FailingCallbackLookup:
        def __getattr__(self, attribute):
            if attribute == "callbacks":
                raise RuntimeError("callback lookup failed")
            return getattr(litellm, attribute)

    callback = BatchCallback()
    monkeypatch.setattr(litellm, "success_callback", [callback])
    monkeypatch.setattr(litellm_helpers, "litellm", FailingCallbackLookup())

    await litellm_helpers._flush_litellm_batch_callbacks(timeout=1)

    assert callback.flushes == 1


def test_drain_retrieves_task_exceptions(clean_litellm_callbacks, monkeypatch):
    """
    A callback that raises must be reported, not left to resurface as an opaque
    "Task exception was never retrieved" during interpreter shutdown.
    """
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._is_litellm_task", lambda task: True)
    messages = []
    sink_id = get_logger().add(lambda m: messages.append(m.record["message"]))

    async def boom():
        raise RuntimeError("callback exploded")

    async def inner():
        task = asyncio.create_task(boom())
        await drain_litellm_callbacks(timeout=5)
        return task

    try:
        task = asyncio.run(inner())
    finally:
        get_logger().remove(sink_id)

    assert any("callback exploded" in message for message in messages)
    # The drain consumed it, so asyncio has nothing left to complain about.
    assert task.exception() is not None


def test_drain_swallows_errors(clean_litellm_callbacks, monkeypatch):
    """Draining is best-effort telemetry; it must never fail the command."""
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    async def inner():
        await drain_litellm_callbacks(timeout=1)

    asyncio.run(inner())  # must not raise


def test_drain_without_the_logging_worker_still_drains_tasks(clean_litellm_callbacks, monkeypatch):
    """If litellm relocates the worker, we degrade to the task drain instead of raising."""
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
        lambda: None,
    )
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._is_litellm_task", lambda task: True)
    ran = {"value": False}

    async def side_task():
        await asyncio.sleep(0)
        ran["value"] = True

    async def inner():
        task = asyncio.create_task(side_task())
        await drain_litellm_callbacks(timeout=5)
        drained = ran["value"]
        _ = await task
        return drained

    drained = asyncio.run(inner())

    assert drained is True


def test_missing_global_logging_worker_attribute_is_treated_as_unavailable(monkeypatch):
    from litellm.litellm_core_utils import logging_worker

    monkeypatch.delattr(logging_worker, "GLOBAL_LOGGING_WORKER", raising=False)

    assert _get_global_logging_worker() is None


def _make_datadog_logger():
    async def flush_queue(self):
        self.flush_count += 1

    logger_type = type(
        "DataDogLogger",
        (),
        {
            "__module__": "litellm.integrations.datadog.datadog",
            "flush_queue": flush_queue,
        },
    )
    logger = logger_type()
    logger.flush_count = 0
    return logger


@pytest.mark.asyncio
async def test_service_datadog_logger_is_flushed_with_registered_logger(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import litellm_logging, logging_utils

    registered_logger = _make_datadog_logger()
    service_logger = _make_datadog_logger()
    service_logging = type("ServiceLogging", (), {"dd_logger": service_logger})()

    monkeypatch.setattr(litellm, "service_callback", ["datadog"])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [registered_logger])
    monkeypatch.setattr(logging_utils, "_service_logger", service_logging)

    await drain_litellm_callbacks(timeout=5)

    assert registered_logger.flush_count == 1
    assert service_logger.flush_count == 1
    assert litellm_logging._in_memory_loggers == [registered_logger]


@pytest.mark.asyncio
async def test_service_datadog_logger_is_flushed_for_instance_callback(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import litellm_logging, logging_utils

    callback_logger = _make_datadog_logger()
    service_logger = _make_datadog_logger()
    service_logging = type("ServiceLogging", (), {"dd_logger": service_logger})()

    monkeypatch.setattr(litellm, "service_callback", [callback_logger])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [])
    monkeypatch.setattr(logging_utils, "_service_logger", service_logging)

    await drain_litellm_callbacks(timeout=5)

    assert callback_logger.flush_count == 1
    assert service_logger.flush_count == 1


def test_instance_callback_ignores_unrelated_service_logger(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import logging_utils

    callback_logger = _make_datadog_logger()
    unrelated_logger = type("UnrelatedLogger", (), {"flush_queue": lambda self: None})()
    service_logging = type("ServiceLogging", (), {"dd_logger": unrelated_logger})()

    monkeypatch.setattr(logging_utils, "_service_logger", service_logging)

    assert litellm_helpers._resolve_litellm_callbacks(callback_logger) == [callback_logger]


@pytest.mark.asyncio
async def test_service_datadog_logger_is_flushed_once_when_also_registered(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import litellm_logging, logging_utils

    datadog_logger = _make_datadog_logger()
    service_logging = type("ServiceLogging", (), {"dd_logger": datadog_logger})()

    monkeypatch.setattr(litellm, "service_callback", ["datadog"])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [datadog_logger])
    monkeypatch.setattr(logging_utils, "_service_logger", service_logging)

    await drain_litellm_callbacks(timeout=5)

    assert datadog_logger.flush_count == 1


@pytest.mark.asyncio
async def test_registered_datadog_logger_is_flushed_without_service_module(clean_litellm_callbacks, monkeypatch):
    import sys

    from litellm.litellm_core_utils import litellm_logging

    datadog_logger = _make_datadog_logger()

    monkeypatch.setattr(litellm, "service_callback", ["datadog"])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [datadog_logger])
    monkeypatch.delitem(sys.modules, "litellm.litellm_core_utils.logging_utils", raising=False)

    await drain_litellm_callbacks(timeout=5)

    assert datadog_logger.flush_count == 1


@pytest.mark.asyncio
async def test_service_datadog_logger_is_not_created_during_drain(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import litellm_logging, logging_utils

    calls = 0

    def fail_if_called():
        nonlocal calls
        calls += 1
        raise AssertionError("draining must not initialize LiteLLM's service logger")

    monkeypatch.setattr(litellm, "service_callback", ["datadog"])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [])
    monkeypatch.setattr(logging_utils, "_service_logger", None)
    monkeypatch.setattr(logging_utils, "_get_service_logger", fail_if_called)

    await drain_litellm_callbacks(timeout=5)

    assert calls == 0
    assert logging_utils._service_logger is None


def test_service_datadog_logger_location_matches_litellm():
    import sys

    from litellm._service_logger import ServiceLogging
    from litellm.litellm_core_utils import logging_utils

    assert sys.modules.get("litellm.litellm_core_utils.logging_utils") is logging_utils
    assert "_service_logger" in vars(logging_utils)
    assert "_service_logger" in logging_utils._get_service_logger.__code__.co_names
    assert "ServiceLogging" in logging_utils._get_service_logger.__code__.co_names
    assert logging_utils._service_logger is None or isinstance(logging_utils._service_logger, ServiceLogging)
    assert "dd_logger" in ServiceLogging.init_datadog_logger_if_none.__code__.co_names
    assert "DataDogLogger" in ServiceLogging.init_datadog_logger_if_none.__code__.co_names


def test_callback_with_uninspectable_type_still_resolves():
    class RaisingModule(type):
        @property
        def __module__(cls):
            raise RuntimeError("module metadata unavailable")

    class Callback(metaclass=RaisingModule):
        pass

    callback = Callback()

    assert litellm_helpers._resolve_litellm_callbacks(callback) == [callback]


@pytest.mark.asyncio
async def test_uninspectable_registry_entry_does_not_block_datadog_loggers(clean_litellm_callbacks, monkeypatch):
    from litellm.litellm_core_utils import litellm_logging, logging_utils

    class RaisingModule(type):
        @property
        def __module__(cls):
            raise RuntimeError("module metadata unavailable")

    class UninspectableLogger(metaclass=RaisingModule):
        pass

    registered_logger = _make_datadog_logger()
    service_logger = _make_datadog_logger()
    service_logging = type("ServiceLogging", (), {"dd_logger": service_logger})()

    monkeypatch.setattr(litellm, "service_callback", ["datadog"])
    monkeypatch.setattr(litellm_logging, "_in_memory_loggers", [UninspectableLogger(), registered_logger])
    monkeypatch.setattr(logging_utils, "_service_logger", service_logging)

    await drain_litellm_callbacks(timeout=5)

    assert registered_logger.flush_count == 1
    assert service_logger.flush_count == 1


def test_named_batch_callback_types_match_litellm_classes():
    from litellm.integrations.custom_batch_logger import CustomBatchLogger

    task_methods = {"async_send_batch", "async_send_message", "async_upload_data_to_s3"}
    assert callable(getattr(CustomBatchLogger, "flush_queue", None))
    assert inspect.iscoroutinefunction(CustomBatchLogger.flush_queue)
    assert set(_LITELLM_BATCH_CALLBACK_TYPES) <= set(litellm._known_custom_logger_compatible_callbacks)
    for module_name, class_name in _LITELLM_BATCH_CALLBACK_TYPES.values():
        callback_type = getattr(importlib.import_module(module_name), class_name)
        assert callback_type.__module__ == module_name
        assert callback_type.__name__ == class_name
        assert issubclass(callback_type, CustomBatchLogger)
        assert inspect.iscoroutinefunction(callback_type.flush_queue)
        implemented_tasks = {
            (parent_type.__module__, method)
            for parent_type in callback_type.__mro__
            if parent_type is not CustomBatchLogger
            for method in vars(parent_type)
        }
        assert any(
            owner_module.startswith("litellm.integrations.") and method in task_methods
            for owner_module, method in implemented_tasks
        ), class_name


@pytest.mark.parametrize(
    ("callback_name", "module_name", "class_name"),
    (
        ("gcs_bucket", "litellm.integrations.gcs_bucket.gcs_bucket", "GCSBucketLogger"),
        ("aws_sqs", "litellm.integrations.sqs", "SQSLogger"),
    ),
)
def test_named_batch_callbacks_resolve_initialized_loggers(
    clean_litellm_callbacks, monkeypatch, callback_name, module_name, class_name
):
    async def flush_queue(self):
        self.flushed = True

    callback_type = type(class_name, (), {"__module__": module_name, "flush_queue": flush_queue})
    callback_subclass = type(f"Wrapped{class_name}", (callback_type,), {})
    callbacks = [callback_type(), callback_subclass()]
    for callback in callbacks:
        callback.flushed = False
    monkeypatch.setattr(litellm, "callbacks", [callback_name])
    monkeypatch.setattr(
        "litellm.litellm_core_utils.litellm_logging._in_memory_loggers",
        callbacks,
    )

    asyncio.run(drain_litellm_callbacks(timeout=5))

    assert all(callback.flushed for callback in callbacks)


def test_named_batch_callbacks_do_not_initialize_loggers(clean_litellm_callbacks, monkeypatch):
    class UnrelatedBatchCallback:
        def __init__(self):
            self.flushed = False

        async def flush_queue(self):
            self.flushed = True

    unrelated_callback = UnrelatedBatchCallback()
    registry = [unrelated_callback]
    monkeypatch.setattr(litellm, "callbacks", ["aws_sqs"])
    monkeypatch.setattr(
        "litellm.litellm_core_utils.litellm_logging._in_memory_loggers",
        registry,
    )

    asyncio.run(drain_litellm_callbacks(timeout=5))

    assert registry == [unrelated_callback]
    assert unrelated_callback.flushed is False


def test_synchronous_batch_callback_is_skipped_without_blocking_async_callbacks(
    clean_litellm_callbacks, monkeypatch
):
    events = []
    warnings = []

    class SyncBatchCallback:
        def flush_queue(self):
            events.append("sync")

    class AsyncBatchCallback:
        async def flush_queue(self):
            events.append("async")

    class AsyncFlushCallable:
        async def __call__(self):
            events.append("callable")

    class CallableBatchCallback:
        flush_queue = AsyncFlushCallable()

    class RecordingLogger:
        @staticmethod
        def warning(message):
            warnings.append(message)

        @staticmethod
        def debug(_message):
            pass

    sync_callback = SyncBatchCallback()
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
    monkeypatch.setattr(litellm, "callbacks", [sync_callback])
    monkeypatch.setattr(litellm, "_async_success_callback", [sync_callback])
    assert litellm_helpers._litellm_batch_callbacks_registered() is False

    asyncio.run(litellm_helpers._flush_litellm_batch_callbacks(timeout=0))

    assert len(warnings) == 1
    assert "SyncBatchCallback" in warnings[0]

    warnings.clear()
    monkeypatch.setattr(
        litellm,
        "callbacks",
        [sync_callback, AsyncBatchCallback(), CallableBatchCallback()],
    )
    assert litellm_helpers._litellm_batch_callbacks_registered() is True

    asyncio.run(litellm_helpers._flush_litellm_batch_callbacks(timeout=1))

    assert sorted(events) == ["async", "callable"]
    assert len(warnings) == 1
    assert "SyncBatchCallback" in warnings[0]


def test_named_batch_callbacks_do_not_collide_with_related_integrations(clean_litellm_callbacks, monkeypatch):
    async def flush_queue(self):
        self.flushed = True

    datadog_type = type(
        "DataDogLogger",
        (),
        {"__module__": "litellm.integrations.datadog.datadog", "flush_queue": flush_queue},
    )
    llm_obs_type = type(
        "DataDogLLMObsLogger",
        (),
        {"__module__": "litellm.integrations.datadog.datadog_llm_obs", "flush_queue": flush_queue},
    )
    datadog_callback = datadog_type()
    datadog_callback.flushed = False
    llm_obs_callback = llm_obs_type()
    llm_obs_callback.flushed = False
    monkeypatch.setattr(litellm, "callbacks", ["datadog"])
    monkeypatch.setattr(
        "litellm.litellm_core_utils.litellm_logging._in_memory_loggers",
        [llm_obs_callback, datadog_callback],
    )

    asyncio.run(drain_litellm_callbacks(timeout=5))

    assert datadog_callback.flushed
    assert llm_obs_callback.flushed is False


def test_drain_flushes_batch_callbacks_and_follow_up_tasks(clean_litellm_callbacks, monkeypatch):
    events = []

    async def run():
        async def upload_callback():
            events.append("uploaded")

        callback = _async_function_with_metadata(
            upload_callback,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        class BatchCallback:
            async def flush_queue(self):
                events.append("flushed")
                self._follow_up = asyncio.create_task(callback())

        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])
        await drain_litellm_callbacks(timeout=5)

    asyncio.run(run())

    assert events == ["flushed", "uploaded"]


def test_batch_callback_flushes_share_drain_timeout(clean_litellm_callbacks, monkeypatch):
    started = []

    class SlowCallback:
        def __init__(self, name):
            self.name = name

        async def flush_queue(self):
            started.append(self.name)
            await asyncio.sleep(2)

    async def run():
        monkeypatch.setattr(litellm, "callbacks", [SlowCallback("first"), SlowCallback("second")])
        start = asyncio.get_running_loop().time()
        await drain_litellm_callbacks(timeout=1.0)
        return asyncio.get_running_loop().time() - start

    elapsed = asyncio.run(run())

    assert sorted(started) == ["first", "second"]
    assert elapsed < 1.5


def test_batch_flush_delivers_callbacks_enqueued_by_last_worker_flush_once(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    batch_pending = []
    delivered = []
    batch_flushes = 0
    batch_timeouts = []

    async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
        nonlocal batch_flushes
        if timeout <= 0:
            return
        batch_flushes += 1
        batch_timeouts.append(timeout)
        if batch_pending:
            delivered.extend(batch_pending)
            batch_pending.clear()

    async def late_worker_callback():
        await asyncio.sleep(0)
        worker._queue.put_nowait("late-task")

    class LateCallbackWorker:
        _worker_task = None
        _running_tasks = set()

        def __init__(self):
            self.flushes = 0
            self._running_tasks = set()
            self._queue = asyncio.Queue()

        async def flush(self):
            self.flushes += 1
            if self.flushes == 1:
                self._running_tasks = {asyncio.create_task(late_worker_callback())}
            while not self._queue.empty():
                batch_pending.append(self._queue.get_nowait())
                self._queue.task_done()

    worker = LateCallbackWorker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)

    asyncio.run(litellm_helpers.drain_litellm_callbacks(timeout=1))

    assert worker.flushes >= 2
    assert batch_flushes == 1
    assert all(timeout > 0 for timeout in batch_timeouts)
    assert delivered == ["late-task"]


def test_slow_worker_preserves_time_for_batch_callback(clean_litellm_callbacks, monkeypatch):
    class SlowWorker:
        _worker_task = None
        _running_tasks = set()

        async def flush(self):
            await asyncio.sleep(5)

    class BatchCallback:
        def __init__(self):
            self.flushed = False

        async def flush_queue(self):
            self.flushed = True

    callback = BatchCallback()

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
        lambda: SlowWorker(),
    )
    monkeypatch.setattr(litellm, "callbacks", [callback])

    async def run():
        await drain_litellm_callbacks(timeout=1.0)

    asyncio.run(run())

    assert callback.flushed


def test_non_batch_callback_keeps_full_timeout_for_worker_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    timeout = 1.2
    flush_delay = 0.85
    monkeypatch.setattr(litellm_helpers, "MAX_DRAIN_ROUNDS", 2)

    class SlowWorker:
        _worker_task = None
        _running_tasks = set()
        _queue = None

        def __init__(self):
            self.completed = False
            self.cancelled = False

        async def flush(self):
            try:
                await asyncio.sleep(flush_delay)
                self.completed = True
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    worker = SlowWorker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

    start = time.monotonic()
    asyncio.run(drain_litellm_callbacks(timeout=timeout))
    elapsed = time.monotonic() - start

    # With the former unconditional reserve, the producer deadline was 0.6s.
    assert worker.completed
    assert not worker.cancelled
    assert flush_delay <= elapsed < timeout + 0.3


def test_cancelling_drain_cancels_worker_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class SlowWorker:
        _worker_task = None
        _running_tasks = set()
        _queue = None

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.flush_task = None

        async def flush(self):
            self.flush_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def run():
        worker = SlowWorker()
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert worker.cancelled.is_set()
        assert worker.flush_task is not None
        assert worker.flush_task.done()
        assert worker.flush_task.cancelled()
        assert worker.flush_task not in asyncio.all_tasks()

    asyncio.run(run())


def test_cancelling_drain_cancels_batch_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class SlowBatchCallback:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.flush_task = None

        async def flush_queue(self):
            self.flush_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def run():
        callback = SlowBatchCallback()
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm, "callbacks", [callback])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(callback.started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert callback.cancelled.is_set()
        assert callback.flush_task is not None
        assert callback.flush_task.done()
        assert callback.flush_task.cancelled()
        assert callback.flush_task not in asyncio.all_tasks()
        assert not any(
            getattr(getattr(task.get_coro(), "cr_code", None), "co_name", "") == "flush_callback"
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    asyncio.run(run())


def test_cancelling_final_drain_cancels_spawned_upload(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    upload_started = asyncio.Event()
    upload_cancelled = asyncio.Event()
    upload_tasks = []

    async def upload_event():
        upload_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            upload_cancelled.set()
            raise

    upload_callback = _async_function_with_metadata(
        upload_event,
        "litellm.integrations.s3_v2",
        "async_upload_data_to_s3",
    )

    async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
        if timeout > 0:
            upload_tasks.append(asyncio.create_task(upload_callback()))

    async def run():
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(upload_started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert upload_cancelled.is_set()
        assert len(upload_tasks) == 1
        assert upload_tasks[0].done()
        assert upload_tasks[0].cancelled()
        assert upload_tasks[0] not in asyncio.all_tasks()

    asyncio.run(run())


def test_cancelling_final_drain_is_bounded_for_resistant_upload(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        upload_started = asyncio.Event()
        upload_cancelled = asyncio.Event()
        released = asyncio.Event()
        upload_tasks = []
        warnings = []

        class RecordingLogger:
            @staticmethod
            def warning(message):
                warnings.append(str(message))

        async def upload_event():
            upload_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upload_cancelled.set()
                await released.wait()
                raise RuntimeError("resistant upload failed") from None

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
            if timeout > 0:
                upload_tasks.append(asyncio.create_task(upload_callback()))

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(upload_started.wait(), timeout=1)
        cleanup_records.clear()
        start = asyncio.get_running_loop().time()
        drain_task.cancel()
        await asyncio.wait_for(upload_cancelled.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed <= litellm_helpers.CANCELLATION_CLEANUP_SECONDS + CANCELLATION_TIMING_TOLERANCE_SECONDS
        assert len(cleanup_records) == 1
        _assert_cleanup_timeouts_are_bounded(cleanup_records)
        assert len(upload_tasks) == 1
        assert not upload_tasks[0].done()

        released.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if upload_tasks[0].done() and any("resistant upload failed" in warning for warning in warnings):
                break
        assert upload_tasks[0].done()
        assert any("litellm callback task raised: resistant upload failed" in warning for warning in warnings)

    asyncio.run(run())


@pytest.mark.parametrize("flush_kind", ("worker", "batch"))
def test_cancelling_drain_is_bounded_for_cancellation_resistant_flush(
    clean_litellm_callbacks, monkeypatch, flush_kind
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        released = asyncio.Event()
        reaped = []
        original_log_task_exceptions = litellm_helpers._log_task_exceptions

        def record_reaped_tasks(tasks):
            reaped.extend(tasks)
            original_log_task_exceptions(tasks)

        monkeypatch.setattr(litellm_helpers, "_log_task_exceptions", record_reaped_tasks)

        class ResistantFlush:
            _worker_task = None
            _running_tasks = set()
            _queue = None

            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()
                self.flush_task = None

            async def flush(self):
                await self._run()

            async def flush_queue(self):
                await self._run()

            async def _run(self):
                self.flush_task = asyncio.current_task()
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    await released.wait()

        resistant = ResistantFlush()
        if flush_kind == "worker":
            monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: resistant)
            monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])
        else:
            monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
            monkeypatch.setattr(litellm, "callbacks", [resistant])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(resistant.started.wait(), timeout=1)
        start = asyncio.get_running_loop().time()
        drain_task.cancel()
        await asyncio.wait_for(resistant.cancelled.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed < 0.5
        assert resistant.flush_task is not None
        assert not resistant.flush_task.done()

        released.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if resistant.flush_task.done() and resistant.flush_task in reaped:
                break
        assert resistant.flush_task.done()
        assert resistant.flush_task in reaped

    asyncio.run(run())


def test_cancelling_batch_flush_cancels_detached_upload(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        upload_started = asyncio.Event()
        upload_cancelled = asyncio.Event()
        upload_tasks = []

        async def upload_event():
            upload_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upload_cancelled.set()
                raise

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        class BatchCallback:
            async def flush_queue(self):
                upload_tasks.append(asyncio.create_task(upload_callback()))
                await upload_started.wait()
                await asyncio.Event().wait()

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(upload_started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert upload_cancelled.is_set()
        assert len(upload_tasks) == 1
        assert upload_tasks[0].done()
        assert upload_tasks[0].cancelled()

    asyncio.run(run())


def test_cancelling_batch_flush_is_bounded_for_resistant_detached_upload(
    clean_litellm_callbacks, monkeypatch
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        upload_started = asyncio.Event()
        upload_cancelled = asyncio.Event()
        released = asyncio.Event()
        upload_tasks = []

        async def upload_event():
            upload_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upload_cancelled.set()
                await released.wait()

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        class BatchCallback:
            async def flush_queue(self):
                upload_tasks.append(asyncio.create_task(upload_callback()))
                await upload_started.wait()
                await asyncio.Event().wait()

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(upload_started.wait(), timeout=1)
        cleanup_records.clear()
        start = asyncio.get_running_loop().time()
        drain_task.cancel()
        await asyncio.wait_for(upload_cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await drain_task
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed <= litellm_helpers.CANCELLATION_CLEANUP_SECONDS + CANCELLATION_TIMING_TOLERANCE_SECONDS
        _assert_cleanup_deadline_is_shared(cleanup_records)
        assert cleanup_records[1][1] > 0
        assert len(upload_tasks) == 1
        assert not upload_tasks[0].done()

        released.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if upload_tasks[0].done():
                break
        assert upload_tasks[0].done()

    asyncio.run(run())


def test_terminal_producer_drain_precedes_final_worker_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        real_wait = asyncio.wait
        producer_waits = 0
        delivered = []
        release_producer = asyncio.Event()

        class Worker:
            _worker_task = None
            _running_tasks = set()

            def __init__(self):
                self._queue = asyncio.Queue()

            async def flush(self):
                while not self._queue.empty():
                    delivered.append(self._queue.get_nowait())
                    self._queue.task_done()

        worker = Worker()

        async def late_producer():
            await release_producer.wait()
            worker._queue.put_nowait("terminal callback")

        producer = _async_function_with_metadata(
            late_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        producer_task = asyncio.create_task(producer())

        async def controlled_wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            nonlocal producer_waits
            tasks = set(tasks)
            if producer_task in tasks:
                producer_waits += 1
                if producer_waits < 3:
                    return set(), tasks
                release_producer.set()
            return await real_wait(tasks, timeout=timeout, return_when=return_when)

        monkeypatch.setattr(litellm_helpers, "MAX_DRAIN_ROUNDS", 1)
        monkeypatch.setattr(litellm_helpers.asyncio, "wait", controlled_wait)
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        await drain_litellm_callbacks(timeout=1)

        assert producer_waits == 3
        assert producer_task.done()
        assert delivered == ["terminal callback"]

    asyncio.run(run())


def test_terminal_worker_flush_stabilizes_new_producer_before_batch_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        buffered = []
        delivered = []
        producer_tasks = []

        async def late_producer():
            await asyncio.sleep(0)
            worker.pending.append("terminal callback")

        producer = _async_function_with_metadata(
            late_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )

        class PendingQueue:
            @staticmethod
            def qsize():
                return int(
                    worker.flushes <= litellm_helpers.MAX_DRAIN_ROUNDS
                    or bool(worker.pending)
                )

        class Worker:
            _worker_task = None
            _running_tasks = set()

            def __init__(self):
                self.flushes = 0
                self.pending = []
                self._queue = PendingQueue()

            async def flush(self):
                self.flushes += 1
                if self.flushes == litellm_helpers.MAX_DRAIN_ROUNDS + 1:
                    producer_tasks.append(asyncio.create_task(producer()))
                elif self.pending:
                    buffered.extend(self.pending)
                    self.pending.clear()

        class BatchCallback:
            async def flush_queue(self):
                delivered.extend(buffered)
                buffered.clear()

        worker = Worker()
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

        await drain_litellm_callbacks(timeout=1)
        await asyncio.gather(*producer_tasks)

        assert worker.flushes == litellm_helpers.MAX_DRAIN_ROUNDS + 2
        assert delivered == ["terminal callback"]
        assert buffered == []

    asyncio.run(run())


def test_stuck_final_upload_is_cancelled_and_logs_single_timeout_warning(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        warnings = []
        upload_tasks = []
        upload_cancelled = asyncio.Event()

        class RecordingLogger:
            @staticmethod
            def warning(message):
                warnings.append(str(message))

        async def upload_event():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upload_cancelled.set()
                raise

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
            if timeout > 0:
                upload_tasks.append(asyncio.create_task(upload_callback()))

        monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        try:
            await drain_litellm_callbacks(timeout=0.1)
        finally:
            for task in upload_tasks:
                task.cancel()
            await asyncio.gather(*upload_tasks, return_exceptions=True)

        timeout_warnings = [
            warning
            for warning in warnings
            if "callback tasks(" in warning and "did not complete within timeout" in warning
        ]
        assert len(timeout_warnings) == 1
        assert upload_cancelled.is_set()

    asyncio.run(run())


def test_external_cancellation_during_incomplete_cleanup_cancels_spawned_uploads(
    clean_litellm_callbacks,
    monkeypatch,
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_started = asyncio.Event()
        parent_cancelled = asyncio.Event()
        allow_child = asyncio.Event()
        child_started = asyncio.Event()
        child_cancelled = asyncio.Event()
        release_parent = asyncio.Event()
        upload_tasks = []

        async def child_upload():
            child_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        child_callback = _async_function_with_metadata(
            child_upload,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def spawning_upload():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                parent_cancelled.set()
                await allow_child.wait()
                upload_tasks.append(asyncio.create_task(child_callback()))
                await child_started.wait()
                await release_parent.wait()
                raise

        upload_callback = _async_function_with_metadata(
            spawning_upload,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def flush_batch_callbacks(timeout, *_args):
            if timeout > 0:
                upload_tasks.append(asyncio.create_task(upload_callback()))

        original_cancel_and_reap = litellm_helpers._cancel_and_reap_tasks

        async def interruptible_cancel_and_reap(tasks, timeout, *, protect_from_cancellation=False):
            if not protect_from_cancellation:
                for task in tasks:
                    task.cancel()
                cleanup_started.set()
                await asyncio.Event().wait()
            await original_cancel_and_reap(
                tasks,
                timeout,
                protect_from_cancellation=protect_from_cancellation,
            )

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm_helpers, "_cancel_and_reap_tasks", interruptible_cancel_and_reap)
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=0.1))
        try:
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.wait_for(parent_cancelled.wait(), timeout=1)
            drain_task.cancel()
            allow_child.set()
            with pytest.raises(asyncio.CancelledError):
                await drain_task
            await asyncio.wait_for(child_cancelled.wait(), timeout=1)
        finally:
            drain_task.cancel()
            release_parent.set()
            await asyncio.gather(drain_task, *upload_tasks, return_exceptions=True)

    asyncio.run(run())


def test_external_cancellation_during_worker_cleanup_cancels_pending_callbacks(
    clean_litellm_callbacks,
    monkeypatch,
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        worker_cleanup_started = asyncio.Event()
        producer_cancelled = asyncio.Event()

        async def pending_producer():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise

        producer = _async_function_with_metadata(
            pending_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        producer_task = asyncio.create_task(producer())

        class Worker:
            _worker_task = None
            _running_tasks = set()
            _queue = None

            @staticmethod
            async def flush():
                await asyncio.Event().wait()

        original_cancel_and_reap = litellm_helpers._cancel_and_reap_tasks

        async def interruptible_cancel_and_reap(tasks, timeout, *, protect_from_cancellation=False):
            if not protect_from_cancellation and any(
                task.get_coro().__name__ == "run_worker_flush"
                for task in tasks
            ):
                for task in tasks:
                    task.cancel()
                worker_cleanup_started.set()
                await asyncio.Event().wait()
            await original_cancel_and_reap(
                tasks,
                timeout,
                protect_from_cancellation=protect_from_cancellation,
            )

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: Worker())
        monkeypatch.setattr(litellm_helpers, "_cancel_and_reap_tasks", interruptible_cancel_and_reap)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=0.1))
        try:
            await asyncio.wait_for(worker_cleanup_started.wait(), timeout=1)
            drain_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain_task
            await asyncio.wait_for(producer_cancelled.wait(), timeout=1)
        finally:
            drain_task.cancel()
            producer_task.cancel()
            await asyncio.gather(drain_task, producer_task, return_exceptions=True)

    asyncio.run(run())


def test_chained_final_uploads_log_round_exhaustion_and_cancel_pending_task(
    clean_litellm_callbacks,
    monkeypatch,
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        warnings = []
        upload_tasks = []
        spawned_uploads = 0

        class RecordingLogger:
            @staticmethod
            def warning(message):
                warnings.append(str(message))

        async def upload_event():
            nonlocal spawned_uploads
            await asyncio.sleep(0.001)
            if spawned_uploads < litellm_helpers.MAX_DRAIN_ROUNDS:
                spawned_uploads += 1
                upload_tasks.append(asyncio.create_task(upload_callback()))

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def flush_batch_callbacks(timeout, *_args):
            if timeout > 0:
                upload_tasks.append(asyncio.create_task(upload_callback()))

        monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        try:
            await drain_litellm_callbacks(timeout=1)
        finally:
            for task in upload_tasks:
                task.cancel()
            await asyncio.gather(*upload_tasks, return_exceptions=True)

        assert spawned_uploads == litellm_helpers.MAX_DRAIN_ROUNDS
        assert any("did not quiesce after bounded drain rounds" in warning for warning in warnings)

    asyncio.run(run())


def test_failing_worker_flush_is_not_retried(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    warnings = []

    class RecordingLogger:
        @staticmethod
        def warning(message):
            warnings.append(str(message))

    class PendingQueue:
        @staticmethod
        def qsize():
            return 1

    class FailingWorker:
        _worker_task = None
        _running_tasks = set()
        _queue = PendingQueue()

        def __init__(self):
            self.calls = 0

        async def flush(self):
            self.calls += 1
            raise RuntimeError("worker flush failed")

    worker = FailingWorker()
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

    async def run():
        async def stuck_callback():
            await asyncio.Event().wait()

        callback = _async_function_with_metadata(
            stuck_callback,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        callback_task = asyncio.create_task(callback())
        try:
            await drain_litellm_callbacks(timeout=0.2)
        finally:
            callback_task.cancel()
            await asyncio.gather(callback_task, return_exceptions=True)

    asyncio.run(run())

    failure_warnings = [warning for warning in warnings if "Failed to flush litellm callback queue" in warning]
    assert worker.calls == 1
    assert len(failure_warnings) == 1
    assert any("callback tasks(" in warning and "did not stabilize" in warning for warning in warnings)
    assert not any("callback queue or worker flush did not stabilize" in warning for warning in warnings)


def test_long_producer_chain_does_not_misreport_round_exhaustion_as_timeout(
    clean_litellm_callbacks, monkeypatch
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    warnings = []

    class RecordingLogger:
        @staticmethod
        def warning(message):
            warnings.append(str(message))

    class ChainedQueue:
        def __init__(self, worker):
            self.worker = worker

        def qsize(self):
            return int(self.worker.remaining > 0)

    class ChainedWorker:
        _worker_task = None
        _running_tasks = set()

        def __init__(self):
            self.calls = 0
            self.remaining = litellm_helpers.MAX_DRAIN_ROUNDS * 2
            self._queue = ChainedQueue(self)

        async def flush(self):
            self.calls += 1
            self.remaining -= 1

    worker = ChainedWorker()
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

    asyncio.run(drain_litellm_callbacks(timeout=5))

    assert worker.remaining > 0
    assert any("did not quiesce after bounded drain rounds" in warning for warning in warnings)
    assert not any("did not complete within timeout" in warning for warning in warnings)


def test_stuck_task_logs_batch_flush_deadline(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    warnings = []

    class RecordingLogger:
        @staticmethod
        def warning(message):
            warnings.append(str(message))

    async def run():
        async def stuck_callback():
            await asyncio.sleep(10)

        callback = _async_function_with_metadata(
            stuck_callback,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        callback_task = asyncio.create_task(callback())
        monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        try:
            await drain_litellm_callbacks(timeout=0.2)
        finally:
            callback_task.cancel()
            await asyncio.gather(callback_task, return_exceptions=True)

    asyncio.run(run())

    task_deadline_warnings = [
        warning
        for warning in warnings
        if "1 callback tasks(" in warning and "did not stabilize before the batch flush deadline" in warning
    ]
    assert len(task_deadline_warnings) == 1
    assert not any("did not complete within timeout" in warning for warning in warnings)


def test_worker_flush_can_span_producer_rounds(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    batch_pending = []
    delivered = []

    class DelayedWorker:
        _worker_task = None

        def __init__(self):
            self._running_tasks = set()
            self._queue = asyncio.Queue()
            self.flushes = 0

        async def flush(self):
            self.flushes += 1
            if self.flushes == 1:
                await asyncio.sleep(0.11)
                self._queue.put_nowait("late-task")
                return
            while not self._queue.empty():
                batch_pending.append(self._queue.get_nowait())
                self._queue.task_done()

    async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
        if timeout > 0:
            delivered.extend(batch_pending)
            batch_pending.clear()

    worker = DelayedWorker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)

    asyncio.run(drain_litellm_callbacks(timeout=1.0))

    assert worker.flushes >= 2
    assert delivered == ["late-task"]


@pytest.mark.parametrize("worker_result", ("call_error", "sync", "async_error"))
def test_worker_flush_result_does_not_block_terminal_drains(clean_litellm_callbacks, monkeypatch, worker_result):
    from pr_agent.algo.ai_handlers import litellm_helpers

    batch_flushes = 0
    uploaded = []
    upload_tasks = []

    async def upload_event():
        uploaded.append("sent")

    upload_callback = _async_function_with_metadata(
        upload_event,
        "litellm.integrations.s3_v2",
        "async_upload_data_to_s3",
    )

    class Worker:
        _worker_task = None
        _running_tasks = set()
        _queue = None

        def __init__(self):
            self.calls = 0

        def flush(self):
            self.calls += 1
            if worker_result == "call_error":
                raise RuntimeError("worker flush failed")
            if worker_result == "sync":
                return None

            async def fail():
                raise RuntimeError("worker flush failed asynchronously")

            return fail()

    class BatchCallback:
        async def flush_queue(self):
            nonlocal batch_flushes
            batch_flushes += 1
            upload_tasks.append(asyncio.create_task(upload_callback()))

    worker = Worker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

    async def run():
        await drain_litellm_callbacks(timeout=0.5)
        uploads_drained = all(task.done() for task in upload_tasks)
        await asyncio.gather(*upload_tasks, return_exceptions=True)
        return uploads_drained

    uploads_drained = asyncio.run(run())

    assert uploads_drained
    assert worker.calls == 1
    assert batch_flushes == 1
    assert uploaded == ["sent"]


def test_expired_rounds_do_not_restart_sync_blocking_worker(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    timeout = 0.4
    monkeypatch.setattr(litellm_helpers, "MAX_DRAIN_ROUNDS", 2)
    batch_flushes = 0
    uploaded = []
    upload_tasks = []

    async def upload_event():
        uploaded.append("sent")

    upload_callback = _async_function_with_metadata(
        upload_event,
        "litellm.integrations.s3_v2",
        "async_upload_data_to_s3",
    )

    class PendingQueue:
        @staticmethod
        def qsize():
            return 1

    class Worker:
        _worker_task = None
        _running_tasks = set()
        _queue = PendingQueue()

        def __init__(self):
            self.calls = 0

        def flush(self):
            self.calls += 1
            # Block past the producer deadline without depending on sleep wake-up accuracy.
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                pass

    class BatchCallback:
        async def flush_queue(self):
            nonlocal batch_flushes
            batch_flushes += 1
            upload_tasks.append(asyncio.create_task(upload_callback()))

    async def run():
        start = asyncio.get_running_loop().time()
        await drain_litellm_callbacks(timeout=timeout)
        elapsed = asyncio.get_running_loop().time() - start
        uploads_drained = all(task.done() for task in upload_tasks)
        await asyncio.gather(*upload_tasks, return_exceptions=True)
        return elapsed, uploads_drained

    worker = Worker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

    elapsed, uploads_drained = asyncio.run(run())

    assert elapsed <= timeout + CANCELLATION_TIMING_TOLERANCE_SECONDS
    assert uploads_drained
    assert worker.calls == 1
    assert batch_flushes == 1
    assert uploaded == ["sent"]


def test_round_exhaustion_keeps_terminal_worker_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class PendingQueue:
        @staticmethod
        def qsize():
            return 1

    class Worker:
        _worker_task = None
        _running_tasks = set()
        _queue = PendingQueue()

        def __init__(self):
            self.calls = 0

        def flush(self):
            self.calls += 1

    class BatchCallback:
        def __init__(self):
            self.calls = 0

        async def flush_queue(self):
            self.calls += 1

    worker = Worker()
    callback = BatchCallback()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm, "callbacks", [callback])

    asyncio.run(drain_litellm_callbacks(timeout=0.5))

    assert worker.calls == litellm_helpers.MAX_DRAIN_ROUNDS + 2
    assert callback.calls == 1


def test_batch_flush_delivers_callbacks_enqueued_by_task_spawned_by_worker_flush(
    clean_litellm_callbacks, monkeypatch
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    batch_pending = []
    delivered = []
    batch_flushes = {"count": 0}

    async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
        if timeout <= 0:
            return
        batch_flushes["count"] += 1
        delivered.extend(batch_pending)
        batch_pending.clear()

    async def enqueue_batch_event():
        # Keep the enqueue behind the worker flush that creates this task.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        worker._queue.put_nowait("late-task")

    class SpawningWorker:
        _worker_task = None
        _running_tasks = set()

        def __init__(self):
            self.flushes = 0
            self._running_tasks = set()
            self._queue = asyncio.Queue()

        async def flush(self):
            self.flushes += 1
            if self.flushes == 1:
                self._running_tasks = {asyncio.create_task(enqueue_batch_event())}
            while not self._queue.empty():
                batch_pending.append(self._queue.get_nowait())
                self._queue.task_done()

    worker = SpawningWorker()
    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)
    monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)

    asyncio.run(litellm_helpers.drain_litellm_callbacks(timeout=1))

    assert worker.flushes == 2
    assert batch_flushes["count"] == 1
    assert delivered == ["late-task"]
    assert batch_pending == []


def test_drain_returns_within_deadline_before_flush_cancellation_cleanup(clean_litellm_callbacks, monkeypatch):
    async def run():
        released = asyncio.Event()
        batch_started = False

        class ResistantWorker:
            _worker_task = None
            _running_tasks = set()

            async def flush(self):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await released.wait()

        class ResistantBatchCallback:
            async def flush_queue(self):
                nonlocal batch_started
                batch_started = True
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await released.wait()

        monkeypatch.setattr(
            "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
            lambda: ResistantWorker(),
        )
        monkeypatch.setattr(litellm, "callbacks", [ResistantBatchCallback()])
        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=1.0))
        start = asyncio.get_running_loop().time()
        done, _ = await asyncio.wait({drain_task}, timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - start
        released.set()
        _ = await drain_task
        return drain_task in done, elapsed, batch_started

    completed, elapsed, batch_started = asyncio.run(run())

    assert completed
    assert elapsed < 1.5
    assert batch_started


def test_litellm_tasks_are_recognised(clean_litellm_callbacks, real_litellm_acompletion):
    """The module filter must actually match litellm's deferred logging helper."""
    logger = _CountingLogger()
    litellm.callbacks = [logger]
    seen = {}

    async def inner():
        await _one_completion()
        seen["litellm"] = [t for t in asyncio.all_tasks()
                           if t is not asyncio.current_task() and _is_litellm_task(t)]
        seen["matched"] = []
        for task in seen["litellm"]:
            frame = getattr(task.get_coro(), "cr_frame", None)
            if frame is not None:
                seen["matched"].append((frame.f_globals.get("__name__", ""), frame.f_code.co_name))
        seen["mine"] = [t for t in asyncio.all_tasks() if t is asyncio.current_task()]
        await drain_litellm_callbacks()

    asyncio.run(inner())

    assert seen["litellm"], "litellm's logging helper task was not recognised"
    assert ("litellm.utils", "_client_async_logging_helper") in seen["matched"]
    assert all(not _is_litellm_task(t) for t in seen["mine"])
    assert logger.calls == 1


def test_litellm_core_task_names_match_installed_version():
    import litellm.utils as litellm_utils
    from litellm._service_logger import ServiceLogging
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.logging_worker import LoggingWorker

    def assert_task_function(function, module_name, function_name):
        assert callable(function)
        assert function.__globals__.get("__name__") == module_name
        assert function.__code__.co_name == function_name

    assert_task_function(
        getattr(litellm_utils, "_client_async_logging_helper", None),
        "litellm.utils",
        "_client_async_logging_helper",
    )
    for method in ("_process_log_task", "_aggressively_clear_queue_async", "_retry_enqueue_task"):
        assert_task_function(
            getattr(LoggingWorker, method, None),
            "litellm.litellm_core_utils.logging_worker",
            method,
        )
    logging_methods = (
        "async_success_handler",
        "async_failure_handler",
        "dispatch_success_handlers",
        "dispatch_failure_handlers",
    )
    for method in logging_methods:
        assert_task_function(
            getattr(Logging, method, None),
            "litellm.litellm_core_utils.litellm_logging",
            method,
        )
    for method in ("async_service_success_hook", "async_service_failure_hook"):
        assert_task_function(getattr(ServiceLogging, method, None), "litellm._service_logger", method)

    integration_task_methods = {
        "aws_sqs": ("async_send_batch", "async_send_message"),
        "s3_v2": ("async_send_batch", "async_upload_data_to_s3"),
    }
    for callback_name, methods in integration_task_methods.items():
        module_name, class_name = _LITELLM_BATCH_CALLBACK_TYPES[callback_name]
        callback_class = getattr(importlib.import_module(module_name), class_name)
        for method in methods:
            assert_task_function(getattr(callback_class, method, None), module_name, method)

    worker = _get_global_logging_worker()
    assert worker is not None
    assert hasattr(worker, "_queue")
    assert hasattr(worker, "_worker_task")
    assert hasattr(worker, "_running_tasks")
    assert callable(getattr(worker, "flush", None))


def test_late_litellm_task_reaches_batch_flush(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class BatchCallback:
        def __init__(self):
            self.log_queue = []
            self.sent = []

        async def flush_queue(self):
            self.sent.extend(self.log_queue)
            self.log_queue.clear()

    async def run():
        callback = BatchCallback()

        class Worker:
            def __init__(self):
                self._worker_task = None
                self._running_tasks = set()
                self._queue = asyncio.Queue()

            async def flush(self):
                while not self._queue.empty():
                    callback.log_queue.append(self._queue.get_nowait())
                    self._queue.task_done()

        worker = Worker()

        async def enqueue_late_event():
            await asyncio.sleep(0.26)
            worker._queue.put_nowait("late")

        helper = _async_function_with_metadata(
            enqueue_late_event,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        helper_task = asyncio.create_task(helper())
        monkeypatch.setattr(litellm, "callbacks", [callback])
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: worker)

        await drain_litellm_callbacks(timeout=0.5)
        await helper_task
        return callback.sent

    assert asyncio.run(run()) == ["late"]


def test_batch_flush_is_best_effort_without_retry_after_producer_timeout(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    class BatchCallback:
        def __init__(self):
            self.calls = 0
            self.log_queue = ["event"]

        async def flush_queue(self):
            self.calls += 1

    callback = BatchCallback()

    async def run():
        async def slow_helper():
            await asyncio.sleep(1)

        helper = _async_function_with_metadata(
            slow_helper,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        helper_task = asyncio.create_task(helper())
        monkeypatch.setattr(litellm, "callbacks", [callback])
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)

        await drain_litellm_callbacks(timeout=0.2)
        helper_task.cancel()
        await asyncio.gather(helper_task, return_exceptions=True)

    asyncio.run(run())

    assert callback.calls == 1
    assert callback.log_queue == ["event"]


def test_batch_flush_reserves_time_for_spawned_upload_tasks(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    uploaded = []
    upload_tasks = []

    async def upload_event():
        await asyncio.sleep(0.18)
        uploaded.append("sent")

    upload_callback = _async_function_with_metadata(
        upload_event,
        "litellm.integrations.s3_v2",
        "async_upload_data_to_s3",
    )

    async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
        await asyncio.sleep(max(0.0, timeout - 0.12))
        upload_tasks.append(asyncio.create_task(upload_callback()))

    monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
    monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
    monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

    async def run():
        await drain_litellm_callbacks(timeout=1.2)
        uploads_drained = all(task.done() for task in upload_tasks)
        await asyncio.gather(*upload_tasks, return_exceptions=True)
        return uploads_drained

    uploads_drained = asyncio.run(run())

    assert uploads_drained
    assert uploaded == ["sent"]


def test_malformed_batch_callback_does_not_block_other_flushes(clean_litellm_callbacks, monkeypatch):
    uploaded = []
    upload_tasks = []

    async def upload_event():
        uploaded.append("sent")

    upload_callback = _async_function_with_metadata(
        upload_event,
        "litellm.integrations.s3_v2",
        "async_upload_data_to_s3",
    )

    class MalformedCallback:
        @property
        def flush_queue(self):
            raise RuntimeError("flush_queue unavailable")

    class BatchCallback:
        def __init__(self):
            self.calls = 0

        async def flush_queue(self):
            self.calls += 1
            upload_tasks.append(asyncio.create_task(upload_callback()))

    async def run():
        await drain_litellm_callbacks(timeout=0.5)
        uploads_drained = all(task.done() for task in upload_tasks)
        await asyncio.gather(*upload_tasks, return_exceptions=True)
        return uploads_drained

    callback = BatchCallback()
    monkeypatch.setattr(litellm, "callbacks", [MalformedCallback(), callback])

    uploads_drained = asyncio.run(run())

    assert uploads_drained
    assert callback.calls == 1
    assert uploaded == ["sent"]


def test_litellm_task_matching_uses_callback_modules():
    async def run():
        async def callback_template():
            await asyncio.sleep(0)

        tasks = []
        cases = (
            ("litellm.utils", "_client_async_logging_helper", True, "<synthetic>/litellm/utils.py"),
            (
                "litellm.litellm_core_utils.logging_worker",
                "_worker_loop",
                False,
                "<synthetic>/litellm/logging_worker.py",
            ),
            (
                "litellm.litellm_core_utils.logging_worker",
                "_process_log_task",
                True,
                "<synthetic>/litellm/logging_worker.py",
            ),
            (
                "litellm.litellm_core_utils.logging_worker",
                "_aggressively_clear_queue_async",
                True,
                "<synthetic>/litellm/logging_worker.py",
            ),
            (
                "litellm.litellm_core_utils.logging_worker",
                "_retry_enqueue_task",
                True,
                "<synthetic>/litellm/logging_worker.py",
            ),
            (
                "litellm.litellm_core_utils.litellm_logging",
                "dispatch_success_handlers",
                True,
                "<synthetic>/litellm/logging.py",
            ),
            (
                "litellm.litellm_core_utils.litellm_logging",
                "async_success_handler",
                True,
                "<synthetic>/litellm/logging.py",
            ),
            (
                "litellm.litellm_core_utils.litellm_logging",
                "async_failure_handler",
                True,
                "<synthetic>/litellm/logging.py",
            ),
            (
                "litellm.litellm_core_utils.litellm_logging",
                "dispatch_failure_handlers",
                True,
                "<synthetic>/litellm/logging.py",
            ),
            ("litellm._service_logger", "async_service_success_hook", True, "<synthetic>/litellm/service.py"),
            ("litellm.integrations.s3_v2", "async_upload_data_to_s3", True, "<synthetic>/litellm/s3_v2.py"),
            ("litellm.integrations.sqs", "async_send_message", True, "<synthetic>/litellm/sqs.py"),
            ("litellm.integrations.langsmith", "async_send_batch", True, "<synthetic>/litellm/langsmith.py"),
            (
                "litellm.integrations.s3_v2",
                "async_send_scheduled_batch",
                False,
                "<synthetic>/litellm/s3_v2.py",
            ),
            (
                "litellm.integrations.custom_batch_logger",
                "periodic_flush",
                False,
                "<synthetic>/litellm/custom_batch_logger.py",
            ),
            (
                "litellm.integrations.SlackAlerting.slack_alerting",
                "_run_scheduled_daily_report",
                False,
                "<synthetic>/litellm/slack.py",
            ),
            (
                "litellm.integrations.SlackAlerting.hanging_request_check",
                "check_for_hanging_requests",
                False,
                "<synthetic>/litellm/hanging.py",
            ),
            ("litellm.proxy.utils", "_run_scheduled_daily_report", False, "<synthetic>/litellm/proxy.py"),
            ("my_litellm_helpers", "callback", False, "<synthetic>/litellm/helpers.py"),
        )
        for module, function, _, filename in cases:
            callback = _async_function_with_metadata(callback_template, module, function, filename)
            tasks.append(asyncio.create_task(callback()))

        try:
            actual = [_is_litellm_task(task) for task in tasks]
            expected = [expected for _, _, expected, _ in cases]
            filenames = [filename for _, _, _, filename in cases]
            assert actual == expected, filenames
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_drain_ignores_worker_task_in_running_tasks(clean_litellm_callbacks, monkeypatch):
    stale_loop = asyncio.new_event_loop()
    stale_task = stale_loop.create_task(asyncio.sleep(3600))

    async def run():
        worker_task = asyncio.create_task(asyncio.sleep(3600))
        callback_task = asyncio.create_task(asyncio.sleep(0))
        seen_tasks = set()

        class FakeWorker:
            _worker_task = worker_task
            _running_tasks = {worker_task, callback_task, stale_task}

            async def flush(self):
                return None

        original_wait = asyncio.wait

        async def recording_wait(tasks, *args, **kwargs):
            seen_tasks.update(tasks)
            return await original_wait(tasks, *args, **kwargs)

        monkeypatch.setattr(
            "pr_agent.algo.ai_handlers.litellm_helpers._get_global_logging_worker",
            lambda: FakeWorker(),
        )
        monkeypatch.setattr(asyncio, "wait", recording_wait)

        try:
            await drain_litellm_callbacks()
            assert callback_task in seen_tasks
            assert worker_task not in seen_tasks
            assert stale_task not in seen_tasks
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        async def cleanup_stale_task():
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)

        stale_loop.run_until_complete(cleanup_stale_task())
        stale_loop.close()


def test_batch_cancellation_uses_one_cleanup_deadline_for_wrapper_and_upload(
    clean_litellm_callbacks, monkeypatch
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        wrapper_cancelled = asyncio.Event()
        upload_cancelled = asyncio.Event()
        release_children = asyncio.Event()
        reaped = []
        tasks = {}
        original_log_task_exceptions = litellm_helpers._log_task_exceptions

        def record_reaped_tasks(completed):
            reaped.extend(completed)
            original_log_task_exceptions(completed)

        async def upload_event():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upload_cancelled.set()
                await release_children.wait()

        upload_callback = _async_function_with_metadata(
            upload_event,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        class BatchCallback:
            async def flush_queue(self):
                tasks["wrapper"] = asyncio.current_task()
                tasks["upload"] = asyncio.create_task(upload_callback())
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    wrapper_cancelled.set()
                    await release_children.wait()

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_log_task_exceptions", record_reaped_tasks)
        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        while "upload" not in tasks:
            await asyncio.sleep(0)
        cleanup_records.clear()
        start = asyncio.get_running_loop().time()
        drain_task.cancel()
        await asyncio.wait_for(wrapper_cancelled.wait(), timeout=1)
        await asyncio.wait_for(upload_cancelled.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed <= litellm_helpers.CANCELLATION_CLEANUP_SECONDS + CANCELLATION_TIMING_TOLERANCE_SECONDS
        _assert_cleanup_deadline_is_exhausted(cleanup_records)
        assert not tasks["wrapper"].done()
        assert not tasks["upload"].done()

        release_children.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if all(task.done() and task in reaped for task in tasks.values()):
                break
        assert all(task.done() and task in reaped for task in tasks.values())

    asyncio.run(run())


@pytest.mark.parametrize("failing_attribute", ("_worker_task", "_running_tasks"))
def test_worker_introspection_failure_preserves_cancellation(
    clean_litellm_callbacks, monkeypatch, failing_attribute
):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        started = asyncio.Event()

        class Worker:
            _queue = None

            @property
            def _worker_task(self):
                if failing_attribute == "_worker_task":
                    raise RuntimeError("worker task failed")
                return None

            @property
            def _running_tasks(self):
                if failing_attribute == "_running_tasks":
                    raise RuntimeError("running tasks failed")
                return set()

            async def flush(self):
                started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: Worker())
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

    asyncio.run(run())


def test_producer_cancellation_cancels_pending_callback_task(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        producer_started = asyncio.Event()
        producer_cancelled = asyncio.Event()

        async def pending_producer():
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise

        producer = _async_function_with_metadata(
            pending_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        producer_task = asyncio.create_task(producer())
        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        await asyncio.sleep(0)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert producer_cancelled.is_set()
        assert producer_task.done()
        assert producer_task.cancelled()

    asyncio.run(run())


def test_producer_and_worker_cancellation_share_one_cleanup_deadline(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        cleanup_records = _record_cancellation_cleanup_deadlines(monkeypatch)
        producer_started = asyncio.Event()
        producer_cancelled = asyncio.Event()
        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()
        release_children = asyncio.Event()

        async def resistant_producer():
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                await release_children.wait()

        producer = _async_function_with_metadata(
            resistant_producer,
            "litellm.utils",
            "_client_async_logging_helper",
        )
        producer_task = asyncio.create_task(producer())

        class Worker:
            _worker_task = None
            _running_tasks = set()
            _queue = None

            async def flush(self):
                worker_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    worker_cancelled.set()
                    await release_children.wait()

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: Worker())
        monkeypatch.setattr(litellm, "callbacks", [_CountingLogger()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=1))
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        cleanup_records.clear()
        start = asyncio.get_running_loop().time()
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task
        elapsed = asyncio.get_running_loop().time() - start

        assert producer_cancelled.is_set()
        assert worker_cancelled.is_set()
        assert elapsed <= litellm_helpers.CANCELLATION_CLEANUP_SECONDS + CANCELLATION_TIMING_TOLERANCE_SECONDS
        _assert_cleanup_deadline_is_exhausted(cleanup_records)
        assert not producer_task.done()

        release_children.set()
        await asyncio.gather(producer_task, return_exceptions=True)

    asyncio.run(run())


def test_final_drain_cancellation_resnapshots_spawned_uploads(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        parent_started = asyncio.Event()
        child_started = asyncio.Event()
        child_cancelled = asyncio.Event()
        release_children = asyncio.Event()
        child_cancel_count = 0
        tasks = []

        async def child_upload():
            nonlocal child_cancel_count
            child_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancel_count += 1
                child_cancelled.set()
                try:
                    await release_children.wait()
                except asyncio.CancelledError:
                    child_cancel_count += 1
                    raise

        child_callback = _async_function_with_metadata(
            child_upload,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def spawning_upload():
            parent_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tasks.append(asyncio.create_task(child_callback()))
                await child_started.wait()
                await release_children.wait()
                raise

        spawning_callback = _async_function_with_metadata(
            spawning_upload,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def blocking_upload():
            await asyncio.Event().wait()

        blocking_callback = _async_function_with_metadata(
            blocking_upload,
            "litellm.integrations.s3_v2",
            "async_upload_data_to_s3",
        )

        async def flush_batch_callbacks(timeout, cleanup_deadline_state=None):
            if timeout > 0:
                tasks.extend(
                    (
                        asyncio.create_task(spawning_callback()),
                        asyncio.create_task(blocking_callback()),
                    )
                )

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: None)
        monkeypatch.setattr(litellm_helpers, "_flush_litellm_batch_callbacks", flush_batch_callbacks)
        monkeypatch.setattr(litellm, "callbacks", ["s3_v2"])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(parent_started.wait(), timeout=1)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert child_cancelled.is_set()
        assert child_cancel_count == 1
        assert not tasks[-1].done()

        release_children.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_qsize_failure_does_not_replace_batch_cancellation(clean_litellm_callbacks, monkeypatch):
    from pr_agent.algo.ai_handlers import litellm_helpers

    async def run():
        qsize_called = asyncio.Event()
        batch_started = asyncio.Event()
        batch_cancelled = asyncio.Event()

        class BrokenQueue:
            @staticmethod
            def qsize():
                qsize_called.set()
                raise RuntimeError("qsize failed")

        class Worker:
            _worker_task = None
            _running_tasks = set()
            _queue = BrokenQueue()

            @staticmethod
            async def flush():
                return None

        class BatchCallback:
            async def flush_queue(self):
                batch_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    batch_cancelled.set()
                    raise

        monkeypatch.setattr(litellm_helpers, "_get_global_logging_worker", lambda: Worker())
        monkeypatch.setattr(litellm, "callbacks", [BatchCallback()])

        drain_task = asyncio.create_task(drain_litellm_callbacks(timeout=30))
        await asyncio.wait_for(batch_started.wait(), timeout=1)
        assert qsize_called.is_set()
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert batch_cancelled.is_set()

    asyncio.run(run())


def test_falsy_registered_batch_callback_is_flushed(clean_litellm_callbacks, monkeypatch):
    class FalsyBatchCallback:
        def __init__(self):
            self.flushed = False

        def __bool__(self):
            return False

        async def flush_queue(self):
            self.flushed = True

    callback = FalsyBatchCallback()
    monkeypatch.setattr(litellm, "callbacks", callback)

    assert litellm_callbacks_registered() is True
    asyncio.run(drain_litellm_callbacks(timeout=5))

    assert callback.flushed


def test_unexpected_logging_worker_lookup_error_is_warned(monkeypatch):
    from litellm import litellm_core_utils

    warnings = []

    class BrokenLoggingWorkerModule:
        def __getattr__(self, name):
            raise RuntimeError("broken worker lookup")

    class RecordingLogger:
        @staticmethod
        def warning(message):
            warnings.append(str(message))

    monkeypatch.setattr(litellm_core_utils, "logging_worker", BrokenLoggingWorkerModule())
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())

    assert _get_global_logging_worker() is None
    assert warnings == [
        "Failed to resolve litellm LoggingWorker; draining pending tasks only: broken worker lookup"
    ]


def test_logging_worker_import_error_is_debugged(monkeypatch):
    from litellm import litellm_core_utils

    debug_messages = []
    warnings = []

    class MissingLoggingWorkerModule:
        def __getattr__(self, name):
            raise ImportError("missing logging worker")

    class RecordingLogger:
        @staticmethod
        def debug(message):
            debug_messages.append(str(message))

        @staticmethod
        def warning(message):
            warnings.append(str(message))

    monkeypatch.setattr(litellm_core_utils, "logging_worker", MissingLoggingWorkerModule())
    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: RecordingLogger())

    assert _get_global_logging_worker() is None
    assert debug_messages == [
        "litellm LoggingWorker unavailable; draining pending tasks only: missing logging worker"
    ]
    assert warnings == []
