"""Raise a real openai.APIError when a streaming response arrives empty."""
import asyncio
import copy
from contextvars import ContextVar
from types import SimpleNamespace

import openai
import pytest
from dynaconf.utils.parse_conf import DynaconfFormatError
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from starlette_context import request_cycle_context

from pr_agent.algo.ai_handlers import litellm_helpers
from pr_agent.algo.ai_handlers.litellm_helpers import _handle_streaming_response
from pr_agent.config_loader import get_settings, global_settings


class Chunk:
    def __init__(self, content, finish_reason):
        delta = type("Delta", (), {"content": content})()
        self.choices = [type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()]
        self.usage = None
        self._hidden_params = {}


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def generate():
            for chunk in self.chunks:
                yield chunk
        return generate()


async def test_stalled_cleanup_does_not_delay_a_completed_result(monkeypatch):
    monkeypatch.setitem(get_settings().config, "stream_cleanup_timeout", 0.02)
    release, closed = asyncio.Event(), asyncio.Event()
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class StalledStream(Stream):
        async def aclose(self):
            try:
                await release.wait()
            finally:
                closed.set()

    try:
        result = await asyncio.wait_for(
            _handle_streaming_response(StalledStream([Chunk("ping", "stop")])), timeout=0.5,
        )
        assert result[0] == "ping"
        assert not closed.is_set()
        tasks = litellm_helpers._stream_close_tasks - previous_tasks
        assert len(tasks) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5)
        assert closed.is_set()
        await asyncio.sleep(0)
        assert not (litellm_helpers._stream_close_tasks - previous_tasks)
    finally:
        tasks = litellm_helpers._stream_close_tasks - previous_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_consumer_cleanup_continues_after_its_wait_deadline(monkeypatch):
    monkeypatch.setitem(get_settings().config, "stream_cleanup_timeout", 0.02)
    started, release, closed, expired = (asyncio.Event() for _ in range(4))
    warnings = []

    def record_warning(message):
        warnings.append(message)
        expired.set()

    monkeypatch.setattr(litellm_helpers, "_warn_stream_cleanup", record_warning)
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class CancelledStream(Stream):
        def __aiter__(self):
            async def generate():
                raise asyncio.CancelledError
                yield
            return generate()

        async def aclose(self):
            started.set()
            try:
                await release.wait()
            finally:
                closed.set()

    try:
        with pytest.raises(asyncio.CancelledError):
            await _handle_streaming_response(CancelledStream([]))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await asyncio.wait_for(expired.wait(), timeout=0.5)
        assert warnings == [
            "Stream cleanup remains pending after 0.02 seconds; cleanup continues in the background"
        ]
        assert not closed.is_set()
        tasks = litellm_helpers._stream_close_tasks - previous_tasks
        assert len(tasks) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5)
        assert closed.is_set()
        await asyncio.sleep(0)
        assert not (litellm_helpers._stream_close_tasks - previous_tasks)
    finally:
        tasks = litellm_helpers._stream_close_tasks - previous_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("count", [65, 100])
async def test_concurrent_fast_closers_are_all_called(count):
    attempts = []
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class FastStream:
        async def aclose(self):
            attempts.append(self)

    streams = [FastStream() for _ in range(count)]
    await asyncio.gather(*(litellm_helpers._close_stream(stream) for stream in streams))
    await asyncio.sleep(0)
    assert set(attempts) == set(streams)
    assert len(attempts) == count
    assert not (litellm_helpers._stream_close_tasks - previous_tasks)


@pytest.mark.parametrize("value", [True, False, 0, -1, None, "invalid", float("inf"), float("nan"), 10**400])
def test_invalid_cleanup_timeout_uses_a_finite_default(monkeypatch, value):
    warnings = []
    monkeypatch.setattr(litellm_helpers, "_warn_stream_cleanup", warnings.append)
    monkeypatch.setitem(get_settings().config, "stream_cleanup_timeout", value)
    assert litellm_helpers._stream_cleanup_timeout() == litellm_helpers.DEFAULT_STREAM_CLEANUP_TIMEOUT_SECONDS
    assert warnings == ["Ignoring invalid config.stream_cleanup_timeout; using the default"]


def test_cleanup_timeout_cannot_exceed_its_ceiling(monkeypatch):
    monkeypatch.setitem(get_settings().config, "stream_cleanup_timeout", 600)
    assert litellm_helpers._stream_cleanup_timeout() == litellm_helpers.MAX_STREAM_CLEANUP_TIMEOUT_SECONDS


async def test_unresolved_host_setting_cannot_replace_a_completed_result(monkeypatch):
    warnings = []
    monkeypatch.setattr(litellm_helpers, "_warn_stream_cleanup", warnings.append)
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "true")
    monkeypatch.delenv("PR_AGENT_MISSING_CLEANUP_TEST", raising=False)
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("AUTO_CAST_FOR_DYNACONF", True)
        get_settings().set("CONFIG", {
            "stream_cleanup_timeout": "@format {env[PR_AGENT_MISSING_CLEANUP_TEST]}",
        }, merge=False)
        with pytest.raises(DynaconfFormatError):
            get_settings().get("config.stream_cleanup_timeout")
        result = await _handle_streaming_response(Stream([Chunk("ping", "stop")]))
    assert result[0] == "ping"
    assert warnings == [
        "Unable to resolve config.stream_cleanup_timeout: DynaconfFormatError; using the default"
    ]


def collect(chunks):
    return asyncio.run(_handle_streaming_response(Stream(chunks), model="some-model"))


def test_collect_a_normal_streaming_response():
    """Keep assembling a streamed answer exactly as before."""
    content, finish_reason, _ = collect([Chunk("hel", None), Chunk("lo", None),
                                         Chunk(None, "stop")])

    assert content == "hello"
    assert finish_reason == "stop"


@pytest.mark.parametrize("chunks, reason", [
    ([Chunk(None, "stop")], "completed with a finish reason but no content"),
    ([Chunk(None, None)], "ended without content or a finish reason"),
])
def test_raise_an_api_error_the_retry_can_catch(chunks, reason):
    """openai.APIError is what @retry(retry_if_exception_type(openai.APIError)) waits for."""
    with pytest.raises(openai.APIError):
        collect(chunks)


def test_the_raised_error_carries_its_message():
    """Keep the diagnostic message that names the finish reason."""
    with pytest.raises(openai.APIError) as excinfo:
        collect([Chunk(None, "content_filter")])

    assert "content_filter" in str(excinfo.value)


@pytest.mark.parametrize("outcome", ["success", "empty", "failure", "cancel"])
async def test_stream_is_closed_on_every_collection_exit(outcome):
    closed = []
    failure = RuntimeError("stream failed")
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class ClosingStream(Stream):
        def __aiter__(self):
            async def generate():
                if outcome == "failure":
                    raise failure
                if outcome == "cancel":
                    raise asyncio.CancelledError
                if outcome == "success":
                    yield Chunk("ping", "stop")
            return generate()

        async def aclose(self):
            closed.append(True)
            raise ValueError("cleanup must not replace the result")

    stream = ClosingStream([])
    try:
        if outcome == "success":
            assert (await _handle_streaming_response(stream))[0] == "ping"
        else:
            expected = {"empty": openai.APIError, "failure": RuntimeError, "cancel": asyncio.CancelledError}[outcome]
            with pytest.raises(expected) as caught:
                await _handle_streaming_response(stream)
            if outcome == "failure":
                assert caught.value is failure
    finally:
        close_tasks = litellm_helpers._stream_close_tasks - previous_tasks
        await asyncio.wait_for(asyncio.gather(*close_tasks, return_exceptions=True), timeout=5)
    assert closed == [True]


@pytest.mark.parametrize("cancel_mode", ["direct", "self"])
@pytest.mark.parametrize("failure", [False, True])
async def test_closer_cancellation_preserves_collection_outcome_and_context(monkeypatch, cancel_mode, failure):
    warnings = []
    closers = []
    correlation_id = ContextVar("closer_cancel_correlation", default="stream")
    consumer = asyncio.current_task()
    restored_in = []
    error = RuntimeError("original iteration failure")
    private_detail = "private-closer-cancellation-detail"

    class CancellingStream(Stream):
        async def __aiter__(self):
            if failure:
                raise error
            yield Chunk("ping", "stop")

        async def aclose(self):
            closer = asyncio.current_task()
            closers.append(closer)
            if cancel_mode == "direct":
                raise asyncio.CancelledError(private_detail)
            closer.cancel(private_detail)
            await asyncio.sleep(0)

        def _restore_consumer_correlation_context(self):
            restored_in.append(asyncio.current_task())
            correlation_id.set("outer")

    monkeypatch.setattr(
        litellm_helpers, "get_logger", lambda: SimpleNamespace(warning=warnings.append, error=lambda *args: None),
    )
    try:
        async with asyncio.timeout(5):
            if failure:
                with pytest.raises(RuntimeError) as caught:
                    await _handle_streaming_response(CancellingStream([]))
                assert caught.value is error
            else:
                content, finish_reason, _ = await _handle_streaming_response(CancellingStream([]))
                assert (content, finish_reason) == ("ping", "stop")
    finally:
        await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), timeout=5)

    closer, = closers
    assert closer.cancelled()
    assert closer not in litellm_helpers._stream_close_tasks
    assert consumer.cancelling() == 0
    assert restored_in == [consumer]
    assert correlation_id.get() == "outer"
    assert warnings == ["Failed to close streaming response: CancelledError"]
    assert private_detail not in str(warnings)


async def test_iteration_cancellation_propagates_before_hanging_close_and_restores_context():
    started, release, finished = (asyncio.Event() for _ in range(3))
    previous_tasks = set(litellm_helpers._stream_close_tasks)
    correlation_id = ContextVar("iteration_cancel_correlation", default="stream")
    restored_in = []
    cancellation = asyncio.CancelledError("original iteration cancellation")

    class CancelledStream:
        async def __aiter__(self):
            raise cancellation
            yield

        async def aclose(self):
            started.set()
            await release.wait()

        def _restore_consumer_correlation_context(self):
            restored_in.append(asyncio.current_task())
            correlation_id.set("outer")

    async def consume():
        try:
            await _handle_streaming_response(CancelledStream())
        except asyncio.CancelledError as error:
            assert error is cancellation
            assert asyncio.current_task().cancelling() == 0
            assert correlation_id.get() == "outer"
            raise

    consumer = asyncio.create_task(consume())
    consumer.add_done_callback(lambda _: finished.set())
    closers = set()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        closers = litellm_helpers._stream_close_tasks - previous_tasks
        closer, = closers
        await asyncio.wait_for(finished.wait(), timeout=5)
        with pytest.raises(asyncio.CancelledError) as caught:
            await consumer
        assert caught.value is cancellation
        assert restored_in == [consumer]
        assert consumer.cancelling() == 0
        assert not release.is_set()
        assert not closer.done()
        assert closer in litellm_helpers._stream_close_tasks
    finally:
        release.set()
        consumer.cancel()
        await asyncio.wait_for(
            asyncio.gather(
                consumer, *closers, *(litellm_helpers._stream_close_tasks - previous_tasks), return_exceptions=True,
            ),
            timeout=5,
        )
    assert closer not in litellm_helpers._stream_close_tasks


async def test_stream_cleanup_queue_retains_response_until_owner_closes_it():
    closed = []
    cleanup = []

    class DeferredStream(Stream):
        async def aclose(self):
            closed.append(True)

    stream = DeferredStream([Chunk("ping", "stop")])
    try:
        assert (await _handle_streaming_response(stream, stream_cleanup=cleanup))[0] == "ping"
        assert cleanup == [stream]
        assert closed == []
    finally:
        await litellm_helpers._close_stream(stream)
    assert closed == [True]


async def test_stream_close_restores_correlation_context_in_the_consuming_task():
    correlation_id = ContextVar("correlation_id", default="outer")
    correlation_id.set("stream")
    consuming_task = asyncio.current_task()
    restored_in = []

    class CorrelatedStream:
        def _restore_consumer_correlation_context(self):
            restored_in.append(asyncio.current_task())
            correlation_id.set("outer")

        async def aclose(self):
            self._restore_consumer_correlation_context()

    await litellm_helpers._close_stream(CorrelatedStream())

    assert len(restored_in) == 2
    assert restored_in[0] is not consuming_task
    assert restored_in[1] is consuming_task
    assert correlation_id.get() == "outer"


async def test_stream_close_restoration_failure_preserves_result_and_redacts_warning(monkeypatch):
    warnings = []

    class BrokenRestoration(Stream):
        def _restore_consumer_correlation_context(self):
            raise RuntimeError("private-correlation-detail")

    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: SimpleNamespace(warning=warnings.append))

    assert (await _handle_streaming_response(BrokenRestoration([Chunk("ping", "stop")])))[0] == "ping"
    assert warnings == ["Unable to restore stream correlation context"]


async def test_real_litellm_stream_close_contract():
    consuming_task = asyncio.current_task()
    events = []

    class UnderlyingStream:
        async def aclose(self):
            events.append(("close", asyncio.current_task()))

    class Logging:
        def _restore_correlation_context(self):
            events.append(("restore", asyncio.current_task()))

        def _restore_correlation_context_if_unclaimed(self):
            pass

    class RealLiteLLMStream(CustomStreamWrapper):
        def __aiter__(self):
            async def generate():
                yield Chunk("ping", "stop")

            return generate()

    stream = object.__new__(RealLiteLLMStream)
    stream.completion_stream = UnderlyingStream()
    stream.logging_obj = Logging()

    assert (await _handle_streaming_response(stream))[0] == "ping"
    assert stream.completion_stream is None
    assert events[0][0] == "close"
    assert events[0][1] is not consuming_task
    assert events[1] == ("restore", events[0][1])
    assert events[2] == ("restore", consuming_task)


def test_litellm_stream_exposes_consumer_correlation_restore_hook():
    assert callable(getattr(CustomStreamWrapper, "_restore_consumer_correlation_context", None))


@pytest.mark.parametrize("failure_type", [ValueError, asyncio.CancelledError, BaseException])
async def test_stream_close_waits_until_completion_and_observes_late_failure(monkeypatch, failure_type):
    started, release, finished = (asyncio.Event() for _ in range(3))
    warnings = []
    previous_tasks = set(litellm_helpers._stream_close_tasks)
    correlation_id = ContextVar("late_close_correlation", default="stream")
    restored_in = []
    consuming_task = None

    class HangingStream:
        async def aclose(self):
            started.set()
            await release.wait()
            raise failure_type("private-late-provider-error")

        def _restore_consumer_correlation_context(self):
            restored_in.append(asyncio.current_task())
            correlation_id.set("outer")

    async def consume():
        with pytest.raises(asyncio.CancelledError):
            await litellm_helpers._close_stream(HangingStream())
        assert correlation_id.get() == "outer"

    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: SimpleNamespace(warning=warnings.append))
    consuming_task = asyncio.create_task(consume())
    close_tasks = set()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not consuming_task.done()
        assert warnings == []
        close_tasks = litellm_helpers._stream_close_tasks - previous_tasks
        assert len(close_tasks) == 1
        closer, = close_tasks
        consuming_task.cancel()
        await consuming_task
        assert restored_in == [consuming_task]
        assert closer in litellm_helpers._stream_close_tasks
        assert not closer.done()
        closer.add_done_callback(lambda _: finished.set())
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        if failure_type is asyncio.CancelledError:
            assert closer.cancelled()
        elif failure_type is BaseException:
            assert isinstance(closer.exception(), BaseException)
        else:
            assert closer.result() is None
        assert closer not in litellm_helpers._stream_close_tasks
        assert len(warnings) == 1
        assert failure_type.__name__ in warnings[0]
        assert "private-late-provider-error" not in warnings[0]
    finally:
        release.set()
        consuming_task.cancel()
        await asyncio.gather(
            consuming_task, *(litellm_helpers._stream_close_tasks - previous_tasks), *close_tasks,
            return_exceptions=True,
        )


async def test_repeated_cancellation_during_close_propagates_without_cancelling_cleanup():
    closing, closed, release = (asyncio.Event() for _ in range(3))
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class CancelledStream:
        def __aiter__(self):
            async def generate():
                yield Chunk("ping", "stop")
            return generate()

        async def aclose(self):
            closing.set()
            try:
                await release.wait()
            finally:
                closed.set()

    task = asyncio.create_task(_handle_streaming_response(CancelledStream()))
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not closed.is_set()
            release.set()
            await closed.wait()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(
            task, *(litellm_helpers._stream_close_tasks - previous_tasks), return_exceptions=True,
        )


async def test_real_litellm_deadline_keeps_underlying_cleanup_alive(monkeypatch):
    monkeypatch.setitem(get_settings().config, "stream_cleanup_timeout", 0.02)
    started, release, closed, cancelled = (asyncio.Event() for _ in range(4))
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class UnderlyingStream:
        async def aclose(self):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            closed.set()

    stream = object.__new__(CustomStreamWrapper)
    stream.completion_stream = UnderlyingStream()
    stream.logging_obj = SimpleNamespace(
        _restore_correlation_context=lambda: None,
        _restore_correlation_context_if_unclaimed=lambda: None,
    )
    try:
        await asyncio.wait_for(litellm_helpers._close_stream(stream), timeout=0.5)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert stream.completion_stream is None
        assert not cancelled.is_set()
        assert not closed.is_set()
        tasks = litellm_helpers._stream_close_tasks - previous_tasks
        assert len(tasks) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.5)
        await asyncio.sleep(0)
        assert closed.is_set()
        assert not cancelled.is_set()
        assert not (litellm_helpers._stream_close_tasks - previous_tasks)
    finally:
        release.set()
        await asyncio.gather(*(litellm_helpers._stream_close_tasks - previous_tasks), return_exceptions=True)


async def test_real_litellm_single_cancellation_does_not_reach_close():
    started, release, closed, cancelled, finished = (asyncio.Event() for _ in range(5))
    previous_tasks = set(litellm_helpers._stream_close_tasks)

    class UnderlyingStream:
        async def aclose(self):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Reproduce a provider closer swallowing one cancellation.
                cancelled.set()
            finally:
                closed.set()

    class Logging:
        def _restore_correlation_context(self):
            pass

        def _restore_correlation_context_if_unclaimed(self):
            pass

    stream = object.__new__(CustomStreamWrapper)
    stream.completion_stream = UnderlyingStream()
    stream.logging_obj = Logging()
    task = asyncio.create_task(litellm_helpers._close_stream(stream))
    close_tasks = set()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not task.done()
        close_tasks = litellm_helpers._stream_close_tasks - previous_tasks
        assert len(close_tasks) == 1
        close_task, = close_tasks
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
        assert not cancelled.is_set()
        assert close_task in litellm_helpers._stream_close_tasks
        close_task.add_done_callback(lambda _: finished.set())
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert closed.is_set()
        assert close_task.result() is None
        assert stream.completion_stream is None
        assert not cancelled.is_set()
        assert close_task not in litellm_helpers._stream_close_tasks
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(
            task, *(litellm_helpers._stream_close_tasks - previous_tasks), *close_tasks,
            return_exceptions=True,
        )


@pytest.mark.parametrize("closer", [None, "not-callable", lambda: None])
async def test_optional_or_synchronous_closer_preserves_success(closer):
    stream = Stream([Chunk("ping", "stop")])
    stream.aclose = closer
    assert (await _handle_streaming_response(stream))[0] == "ping"


async def test_cleanup_warning_failure_preserves_success(monkeypatch):
    def fail(*args):
        raise RuntimeError("sensitive-cleanup-detail")

    class FailingStream(Stream):
        async def aclose(self):
            raise ValueError("private-provider-detail")

    monkeypatch.setattr(litellm_helpers, "get_logger", lambda: SimpleNamespace(warning=fail))

    assert (await _handle_streaming_response(FailingStream([Chunk("ping", "stop")])))[0] == "ping"
