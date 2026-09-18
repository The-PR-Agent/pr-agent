"""Exercise AWS refresh and cancelled workers without handler-state writes."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError

import pr_agent.algo.ai_handlers.litellm_ai_handler as handler_module
from tests.unittest import test_litellm_imds as imds_tests

LiteLLMAIHandler = handler_module.LiteLLMAIHandler
_frozen_creds = imds_tests._frozen_creds
# Register shared fixtures without calling their decorated functions.
isolate_aws = imds_tests.isolate_aws
aws_session = imds_tests.aws_session


async def _prepare(handler):
    async with handler._snapshot_aws_request_credentials(True) as snapshot:
        return snapshot


@pytest.fixture
def handler(monkeypatch, aws_session):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(handler_module, "get_settings", imds_tests._static_aws_settings)
    return LiteLLMAIHandler()


@pytest.fixture
async def worker_pool(monkeypatch):
    """Join workers after releasing all test-owned blocking events."""
    loop = asyncio.get_running_loop()
    submit = loop.run_in_executor
    releases = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        monkeypatch.setattr(loop, "run_in_executor", lambda executor, fn, *args: submit(pool, fn, *args))
        try:
            yield releases
        finally:
            for event in releases:
                event.set()


@pytest.mark.asyncio
async def test_constructor_discovery_and_off_loop_refresh_context(handler, aws_session):
    credentials = aws_session.get_credentials.return_value
    aws_session.get_credentials.assert_called_once_with()
    credentials.get_frozen_credentials.assert_called_once_with()
    context = ContextVar("aws-test-context", default=None)
    token = context.set("request-a")
    loop_thread = threading.get_ident()
    seen = []

    def freeze():
        seen.append((threading.get_ident(), context.get()))
        return _frozen_creds(access_key="ROTATED-KEY", token="rotated-token")

    credentials.get_frozen_credentials.side_effect = freeze
    try:
        snapshot, _ = await _prepare(handler)
    finally:
        context.reset(token)
    assert seen[0][0] != loop_thread
    assert seen[0][1] == "request-a"
    assert snapshot["aws_access_key_id"] == "ROTATED-KEY"
    assert snapshot["aws_session_token"] == "rotated-token"
    assert snapshot["aws_region_name"] == "us-east-1"
    snapshot["aws_access_key_id"] = "not-handler-state"
    assert handler._aws_active_creds["aws_access_key_id"] == "ROTATED-KEY"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai/gpt-4o", "bedrock/model", "bedrock_mantle/model"])
async def test_non_aws_and_bearer_requests_do_not_refresh(monkeypatch, aws_session, model):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer")
    monkeypatch.setattr(handler_module, "get_settings", lambda: imds_tests._base_settings({"openai.key": "key"}))
    handler = LiteLLMAIHandler()
    credentials = aws_session.get_credentials.return_value
    credentials.get_frozen_credentials.reset_mock()
    await handler.probe_completion(model, _completion=AsyncMock())
    aws_session.get_credentials.assert_called_once_with()
    credentials.get_frozen_credentials.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_error", [None, ValueError("worker-secret"), OSError("worker-secret")])
@pytest.mark.parametrize("fallback", [False, True])
async def test_late_worker_cannot_overwrite_newer_request(monkeypatch, handler, worker_pool, old_error, fallback):
    loop = asyncio.get_running_loop()
    delayed = asyncio.Event()
    reading = asyncio.Event()
    finished = asyncio.Event()
    enter = threading.Event()
    release = threading.Event()
    worker_pool.extend([enter, release])
    read = handler._read_aws_frozen_credentials
    old_credentials = MagicMock()
    original_credentials = handler._aws_boto3_creds
    invocations = 0

    def old_freeze():
        loop.call_soon_threadsafe(reading.set)
        assert release.wait(10), "Test did not release refresh"
        if old_error:
            raise old_error
        return _frozen_creds(access_key="OLD-KEY")

    old_credentials.get_frozen_credentials.side_effect = old_freeze

    def reordered_read(credentials):
        nonlocal invocations
        invocations += 1
        if invocations != 1:
            return read(credentials)
        loop.call_soon_threadsafe(delayed.set)
        assert enter.wait(10), "Test did not release SDK lock acquisition"
        try:
            return read(old_credentials)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(handler, "_read_aws_frozen_credentials", reordered_read)
    first = asyncio.create_task(_prepare(handler))
    try:
        await asyncio.wait_for(delayed.wait(), 5)
        first.cancel()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not handler._aws_bedrock_lock.locked()
        original_credentials.get_frozen_credentials.return_value = _frozen_creds(access_key="NEW-KEY")
        async with handler._snapshot_aws_request_credentials(True) as (snapshot, _):
            assert snapshot["aws_access_key_id"] == "NEW-KEY"
            enter.set()
            await asyncio.wait_for(reading.wait(), 5)
            if fallback:
                handler._activate_static_aws_fallback()
            expected = dict(handler._aws_active_creds)
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            assert handler._aws_active_creds == expected
            assert handler._aws_imds_fell_back is fallback
            assert snapshot["aws_access_key_id"] == "NEW-KEY"
        if fallback:
            original_credentials.get_frozen_credentials.reset_mock()
            await _prepare(handler)
            original_credentials.get_frozen_credentials.assert_not_called()
    finally:
        enter.set()
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_worker_and_waiter_serialize_sdk_access(monkeypatch, handler, worker_pool):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    waiting = asyncio.Event()
    release = threading.Event()
    worker_pool.append(release)
    sdk_lock = handler._aws_refresh_lock
    freezes = []

    class ObservedLock:
        def __enter__(self):
            if sdk_lock.locked():
                loop.call_soon_threadsafe(waiting.set)
            sdk_lock.acquire()

        def __exit__(self, *args):
            sdk_lock.release()

    def freeze():
        freezes.append(threading.get_ident())
        if len(freezes) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "Test did not release refresh"
        return _frozen_creds()

    monkeypatch.setattr(handler, "_aws_refresh_lock", ObservedLock())
    handler._aws_boto3_creds.get_frozen_credentials.side_effect = freeze
    completion = AsyncMock()
    tasks = [asyncio.create_task(handler.probe_completion("bedrock/model", _completion=completion))]
    try:
        await asyncio.wait_for(started.wait(), 5)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        completion.assert_not_called()
        tasks.append(asyncio.create_task(_prepare(handler)))
        await asyncio.wait_for(waiting.wait(), 5)
        assert len(freezes) == 1
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        release.set()
        snapshot, _ = await asyncio.wait_for(_prepare(handler), 5)
        assert snapshot["aws_access_key_id"] == "IMDS-KEY"
        assert not handler._aws_bedrock_lock.locked()
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CredentialRetrievalError(provider="test", error_msg="secret"),
    ClientError({"Error": {"Code": "AccessDenied", "Message": "secret"}}, "GetCredentials"),
    OSError("secret"), ValueError("secret"), RuntimeError("secret"),
])
@pytest.mark.parametrize("static", [False, True])
async def test_refresh_error_classification(handler, error, static):
    if not static:
        handler._aws_static_creds = None
    original = dict(handler._aws_active_creds)
    handler._aws_boto3_creds.get_frozen_credentials.side_effect = error
    if isinstance(error, (ValueError, RuntimeError)):
        with pytest.raises(type(error)):
            await _prepare(handler)
        assert handler._aws_active_creds == original
        assert not handler._aws_imds_fell_back
    else:
        snapshot, _ = await _prepare(handler)
        assert snapshot == (handler._aws_static_creds if static else original)
        assert handler._aws_imds_fell_back is static
    assert not handler._aws_bedrock_lock.locked()


@pytest.mark.asyncio
async def test_final_trust_guard_rejects_without_fallback(monkeypatch, handler):
    async def return_with_drift(*args):
        monkeypatch.setenv("AWS_PROFILE", "another-request")
        return _frozen_creds(access_key="UNTRUSTED")

    monkeypatch.setattr(asyncio, "to_thread", return_with_drift)
    original = dict(handler._aws_active_creds)
    with pytest.raises(ValueError, match="credential-chain environment"):
        await _prepare(handler)
    assert handler._aws_active_creds == original
    assert not handler._aws_imds_fell_back


@pytest.mark.asyncio
async def test_cancellation_at_refresh_completion_does_not_commit(monkeypatch, handler):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    submitted = asyncio.Event()

    def submit(*args):
        submitted.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", submit)
    original = dict(handler._aws_active_creds)
    task = asyncio.create_task(_prepare(handler))
    try:
        await asyncio.wait_for(submitted.wait(), 5)
        future.set_result(_frozen_creds(access_key="UNACCEPTED"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handler._aws_active_creds == original
        assert not handler._aws_imds_fell_back
        assert not handler._aws_bedrock_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_async_lock_waiter_does_not_submit_worker(monkeypatch, handler):
    submitted = MagicMock()
    monkeypatch.setattr(asyncio, "to_thread", submitted)
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        return await _prepare(handler)

    async with handler._aws_bedrock_lock:
        task = asyncio.create_task(waiting())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            submitted.assert_not_called()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_sdk_object_does_not_submit_worker(monkeypatch, handler):
    submitted = MagicMock()
    monkeypatch.setattr(asyncio, "to_thread", submitted)
    handler._aws_boto3_creds = None
    snapshot, _ = await _prepare(handler)
    assert snapshot == handler._aws_static_creds
    assert handler._aws_imds_fell_back
    submitted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("serialize", [False, True], ids=["console", "json"])
async def test_worker_errors_have_secret_free_diagnostics(handler, serialize):
    handler._aws_boto3_creds.get_frozen_credentials.side_effect = ValueError("aws-worker-secret-sentinel")
    messages = []
    logger = handler_module.get_logger()
    sink = logger.add(messages.append, format="{message}", serialize=serialize, backtrace=True, diagnose=True)
    try:
        with pytest.raises(ValueError):
            await _prepare(handler)
    finally:
        logger.remove(sink)
    assert len(messages) == 1
    assert messages[0].record["message"] == "AWS credential refresh failed: ValueError"
    assert messages[0].record["exception"] is None
    assert "aws-worker-secret-sentinel" not in str(messages[0])


@pytest.mark.asyncio
async def test_logging_failure_does_not_replace_worker_error(monkeypatch, handler):
    handler._aws_boto3_creds.get_frozen_credentials.side_effect = OSError("sdk-secret")
    logger = MagicMock()
    logger.error.side_effect = RuntimeError("logger-secret")
    monkeypatch.setattr(handler_module, "get_logger", lambda: logger)
    snapshot, _ = await _prepare(handler)
    assert snapshot == handler._aws_static_creds
    assert handler._aws_imds_fell_back
    logger.error.assert_called_once_with("AWS credential refresh failed: OSError")
