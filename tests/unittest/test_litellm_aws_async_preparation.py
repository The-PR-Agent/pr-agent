"""Exercise deferred AWS preparation and ownership after caller cancellation."""

import asyncio
import threading
from contextvars import ContextVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as handler_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from tests.unittest.test_litellm_imds import _base_settings, _frozen_creds, _static_aws_settings
from tests.unittest.test_litellm_imds import isolate_aws as isolate_aws


async def _prepare(handler):
    async with handler._snapshot_aws_request_credentials(True) as snapshot:
        return snapshot


@pytest.mark.asyncio
async def test_non_aws_probe_never_discovers_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(handler_module, "get_settings", lambda: _base_settings({"openai.key": "request-key"}))
    with patch("boto3.Session") as session:
        handler = LiteLLMAIHandler()
        session.assert_not_called()
        await handler.probe_completion("openai/gpt-4o", _completion=AsyncMock())
        session.assert_not_called()
    assert handler._aws_preparation_future is None


@pytest.mark.asyncio
async def test_discovery_and_refresh_run_off_loop_with_request_context(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    context = ContextVar("aws-test-context", default=None)
    context.set("request-a")
    loop_thread = threading.get_ident()
    calls = []

    def record(stage):
        calls.append((stage, threading.get_ident(), context.get()))

    def freeze():
        record("freeze")
        return _frozen_creds()

    credentials = MagicMock()
    credentials.get_frozen_credentials.side_effect = freeze
    session = MagicMock(region_name="us-east-1")

    def get_credentials():
        record("get")
        return credentials

    session.get_credentials.side_effect = get_credentials

    def create_session():
        record("session")
        return session

    with patch("boto3.Session", side_effect=create_session):
        handler = LiteLLMAIHandler()
        assert calls == []
        first, _ = await _prepare(handler)
        second, _ = await _prepare(handler)

    assert first == second
    assert [stage for stage, _, _ in calls] == ["session", "get", "freeze", "freeze"]
    assert all(thread != loop_thread and value == "request-a" for _, thread, value in calls)
    assert handler._aws_discovery_complete
    assert handler._aws_preparation_future is None
    assert not handler._aws_bedrock_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("static", [False, True])
async def test_empty_discovery_is_not_repeated(monkeypatch, static):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    if static:
        monkeypatch.setattr(handler_module, "get_settings", _static_aws_settings)
    session = MagicMock(region_name="us-east-1")
    session.get_credentials.return_value = None
    with patch("boto3.Session", return_value=session) as factory:
        handler = LiteLLMAIHandler()
        first, _ = await _prepare(handler)
        second, _ = await _prepare(handler)
    assert first == second
    assert bool(first) == static
    factory.assert_called_once_with()
    assert handler._aws_discovery_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovery", "refresh"])
@pytest.mark.parametrize("worker_error", [False, True])
async def test_cancelled_preparation_retains_lock_until_worker_finishes(monkeypatch, phase, worker_error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls = []
    block_next = False
    credentials = MagicMock()
    session = MagicMock(region_name="us-east-1")
    session.get_credentials.return_value = credentials

    def freeze():
        nonlocal block_next
        calls.append(threading.get_ident())
        if block_next:
            block_next = False
            loop.call_soon_threadsafe(started.set)
            if not release.wait(10):
                raise AssertionError("Test did not release the AWS worker")
            if worker_error:
                raise ValueError("delayed-worker-error")
        return _frozen_creds()

    credentials.get_frozen_credentials.side_effect = freeze
    tasks = []
    pending = None
    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        try:
            if phase == "refresh":
                await _prepare(handler)
            block_next = True
            completion = AsyncMock()
            first = asyncio.create_task(handler.probe_completion("bedrock/model", _completion=completion))
            tasks.append(first)
            await asyncio.wait_for(started.wait(), 5)
            pending = handler._aws_preparation_future
            first.cancel()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 5)
            assert not pending.done()
            assert handler._aws_bedrock_lock.locked()
            completion.assert_not_called()

            entered = asyncio.Event()

            async def queued():
                entered.set()
                return await _prepare(handler)

            second = asyncio.create_task(queued())
            tasks.append(second)
            await asyncio.wait_for(entered.wait(), 5)
            assert not second.done()
            assert len(calls) == (2 if phase == "refresh" else 1)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert handler._aws_preparation_future is pending
            assert not pending.cancelled()

            third = asyncio.create_task(_prepare(handler))
            tasks.append(third)
            release.set()
            snapshot, _ = await asyncio.wait_for(third, 5)
            assert snapshot["aws_access_key_id"] == "IMDS-KEY"
            assert handler._aws_preparation_future is None
            assert not handler._aws_bedrock_lock.locked()
            assert len(calls) == (3 if phase == "refresh" else 2)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_preparation_releases_ownership_and_can_retry(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    session = MagicMock(region_name="us-east-1")
    session.get_credentials.return_value.get_frozen_credentials.return_value = _frozen_creds()
    with patch("boto3.Session", side_effect=[ValueError("discovery rejected"), session]) as factory:
        handler = LiteLLMAIHandler()
        with pytest.raises(ValueError, match="discovery rejected"):
            await _prepare(handler)
        assert not handler._aws_bedrock_lock.locked()
        assert handler._aws_preparation_future is None
        assert not handler._aws_discovery_complete
        snapshot, _ = await asyncio.wait_for(_prepare(handler), 5)
    assert snapshot["aws_access_key_id"] == "IMDS-KEY"
    assert handler._aws_discovery_complete
    assert factory.call_count == 2


@pytest.mark.asyncio
async def test_cancellation_at_worker_completion_releases_exactly_once(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    handler = LiteLLMAIHandler()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    submitted = asyncio.Event()

    def submit(*args):
        submitted.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", submit)
    task = asyncio.create_task(_prepare(handler))
    await submitted.wait()
    future.set_result(None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Wait for the registered release callback by acquiring after completion.
    await asyncio.wait_for(handler._aws_bedrock_lock.acquire(), 5)
    assert handler._aws_preparation_future is None
    handler._aws_bedrock_lock.release()
