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

import litellm
import pytest

from pr_agent.algo.ai_handlers.litellm_helpers import drain_litellm_callbacks, litellm_callbacks_registered
from pr_agent.config_loader import global_settings

_CALLBACK_ATTRS = ("callbacks", "success_callback", "failure_callback", "service_callback",
                   "_async_success_callback", "_async_failure_callback")


@pytest.fixture
def clean_litellm_callbacks():
    """Snapshot litellm's module-level callback lists and restore them afterwards."""
    snapshot = {attr: getattr(litellm, attr, None) for attr in _CALLBACK_ATTRS}
    enable_callbacks = global_settings.get("LITELLM.ENABLE_CALLBACKS", False)
    for attr in _CALLBACK_ATTRS:
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


# --- litellm_callbacks_registered ------------------------------------------------

def test_callbacks_not_registered_on_clean_state(clean_litellm_callbacks):
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

def test_callbacks_are_dropped_without_the_drain(clean_litellm_callbacks):
    """Pins the regression: this is exactly what issue #2378 reports."""
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    asyncio.run(_one_completion())

    assert logger.calls == 0


def test_drain_delivers_callbacks_before_the_loop_closes(clean_litellm_callbacks):
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    async def inner():
        await _one_completion()
        await drain_litellm_callbacks()

    asyncio.run(inner())

    assert logger.calls == 1


def test_drain_delivers_every_concurrent_callback(clean_litellm_callbacks):
    logger = _CountingLogger()
    litellm.callbacks = [logger]

    async def inner():
        await asyncio.gather(*(_one_completion() for _ in range(3)))
        await drain_litellm_callbacks()

    asyncio.run(inner())

    assert logger.calls == 3


def test_drain_does_not_wait_on_the_logging_worker(clean_litellm_callbacks):
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


def test_drain_gives_up_at_the_timeout(clean_litellm_callbacks):
    """A task that never finishes must not hold the process past the deadline."""
    elapsed = {}

    async def inner():
        stuck = asyncio.create_task(asyncio.sleep(30))
        loop = asyncio.get_running_loop()
        start = loop.time()
        await drain_litellm_callbacks(timeout=0.2)
        elapsed["seconds"] = loop.time() - start
        stuck.cancel()

    asyncio.run(inner())

    assert elapsed["seconds"] < 5


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
    ran = {"value": False}

    async def side_task():
        await asyncio.sleep(0)
        ran["value"] = True

    async def inner():
        asyncio.create_task(side_task())
        await drain_litellm_callbacks(timeout=5)

    asyncio.run(inner())

    assert ran["value"] is True
