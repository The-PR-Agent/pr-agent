import asyncio
import inspect
import json
import sys

import litellm
import openai

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30
MAX_DRAIN_ROUNDS = 5
FLUSH_RESERVE_SECONDS = 1.0  # cap for each terminal phase reservation
CANCELLATION_CLEANUP_SECONDS = 0.1
_LITELLM_CALLBACK_ATTRS = (
    "callbacks",
    "success_callback",
    "failure_callback",
    "service_callback",
    "_async_success_callback",
    "_async_failure_callback",
)
_LITELLM_BATCH_CALLBACK_TYPES = {
    "argilla": ("litellm.integrations.argilla", "ArgillaLogger"),
    "aws_sqs": ("litellm.integrations.sqs", "SQSLogger"),
    "azure_sentinel": ("litellm.integrations.azure_sentinel.azure_sentinel", "AzureSentinelLogger"),
    "azure_storage": ("litellm.integrations.azure_storage.azure_storage", "AzureBlobStorageLogger"),
    "datadog": ("litellm.integrations.datadog.datadog", "DataDogLogger"),
    "datadog_llm_observability": (
        "litellm.integrations.datadog.datadog_llm_obs",
        "DataDogLLMObsLogger",
    ),
    "datadog_metrics": ("litellm.integrations.datadog.datadog_metrics", "DatadogMetricsLogger"),
    "gcs_bucket": ("litellm.integrations.gcs_bucket.gcs_bucket", "GCSBucketLogger"),
    "gcs_pubsub": ("litellm.integrations.gcs_pubsub.pub_sub", "GcsPubSubLogger"),
    "generic_api": ("litellm.integrations.generic_api.generic_api_callback", "GenericAPILogger"),
    "langsmith": ("litellm.integrations.langsmith", "LangsmithLogger"),
    "literalai": ("litellm.integrations.literal_ai", "LiteralAILogger"),
    "opik": ("litellm.integrations.opik.opik", "OpikLogger"),
    "posthog": ("litellm.integrations.posthog", "PostHogLogger"),
    "s3_v2": ("litellm.integrations.s3_v2", "S3Logger"),
}


def _response_field(response, name):
    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _stream_usage(chunk):
    """Read finalized usage from a regular or metadata-only LiteLLM chunk."""
    usage = _response_field(chunk, "usage")
    if usage is not None:
        return usage
    hidden_params = _response_field(chunk, "_hidden_params")
    if isinstance(hidden_params, dict):
        return hidden_params.get("usage")
    return None


async def _handle_streaming_response(response, model=None):
    """
    Handle streaming response from acompletion and collect the full response.

    Args:
        response: The streaming response object from acompletion

    Returns:
        tuple: (full_response_content, finish_reason, completed_response)
    """
    full_response = ""
    finish_reason = None
    finalized_usage = None

    try:
        async for chunk in response:
            usage = _stream_usage(chunk)
            if usage is not None:
                finalized_usage = usage
            if chunk.choices and len(chunk.choices) > 0:
                choice = chunk.choices[0]
                delta = choice.delta
                content = getattr(delta, 'content', None)
                if content:
                    full_response += content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
    except Exception as e:
        get_logger().error(f"Error handling streaming response: {e}")
        raise

    if not full_response and finish_reason is None:
        get_logger().warning("Streaming response resulted in empty content with no finish reason")
        raise openai.APIError("Empty streaming response received without proper completion")
    elif not full_response and finish_reason:
        get_logger().debug(f"Streaming response resulted in empty content but completed with finish_reason: {finish_reason}")
        raise openai.APIError(f"Streaming response completed with finish_reason '{finish_reason}' but no content received")
    return full_response, finish_reason, MockResponse(full_response, finish_reason, finalized_usage, model)


class MockResponse:
    """Represent a completed streaming response while retaining LiteLLM's finalized usage object."""

    def __init__(self, resp, finish_reason, usage=None, model=None):
        self.usage = usage
        self._data = {
            "choices": [
                {
                    "message": {"content": resp},
                    "finish_reason": finish_reason
                }
            ]
        }
        if model is not None:
            self._data["model"] = model

    def dict(self):
        data = self._data.copy()
        if self.usage is not None:
            if hasattr(self.usage, "model_dump"):
                data["usage"] = self.usage.model_dump()
            elif isinstance(self.usage, dict):
                data["usage"] = self.usage.copy()
            else:
                data["usage"] = vars(self.usage).copy()
        return data


def _get_azure_ad_token():
    """
    Generates an access token using Azure AD credentials from settings.
    Returns:
        str: The access token
    """
    from azure.identity import ClientSecretCredential
    try:
        credential = ClientSecretCredential(
            tenant_id=get_settings().azure_ad.tenant_id,
            client_id=get_settings().azure_ad.client_id,
            client_secret=get_settings().azure_ad.client_secret
        )
        # Get token for Azure OpenAI service
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token
    except Exception as e:
        get_logger().error(f"Failed to get Azure AD token: {e}")
        raise


def _process_litellm_extra_body(kwargs: dict) -> dict:
    """
    Process LITELLM.EXTRA_BODY configuration and update kwargs accordingly.

    Args:
        kwargs: The current kwargs dictionary to update

    Returns:
        Updated kwargs dictionary

    Raises:
        ValueError: If extra_body contains invalid JSON, unsupported keys, or colliding keys
    """
    allowed_extra_body_keys = {"processing_mode", "service_tier"}
    extra_body = getattr(getattr(get_settings(), "litellm", None), "extra_body", None)
    if extra_body:
        try:
            litellm_extra_body = json.loads(extra_body)
            if not isinstance(litellm_extra_body, dict):
                raise ValueError("LITELLM.EXTRA_BODY must be a JSON object")
            unsupported_keys = set(litellm_extra_body.keys()) - allowed_extra_body_keys
            if unsupported_keys:
                raise ValueError(f"LITELLM.EXTRA_BODY contains unsupported keys: {', '.join(unsupported_keys)}. Allowed keys: {', '.join(allowed_extra_body_keys)}")
            colliding_keys = kwargs.keys() & litellm_extra_body.keys()
            if colliding_keys:
                raise ValueError(f"LITELLM.EXTRA_BODY cannot override existing parameters: {', '.join(colliding_keys)}")
            kwargs.update(litellm_extra_body)
        except json.JSONDecodeError as e:
            raise ValueError(f"LITELLM.EXTRA_BODY contains invalid JSON: {str(e)}")
    return kwargs


def _get_global_logging_worker():
    """
    Return litellm's module-global LoggingWorker, or None if it is unavailable.

    Lives under litellm's internal litellm_core_utils, so it may move; losing it
    costs the queue flush, not the task drain.
    """
    try:
        from litellm.litellm_core_utils import logging_worker

        worker = getattr(logging_worker, "GLOBAL_LOGGING_WORKER", None)
    except ImportError as e:
        get_logger().debug(f"litellm LoggingWorker unavailable; draining pending tasks only: {e}")
        return None
    except Exception as e:
        get_logger().warning(f"Failed to resolve litellm LoggingWorker; draining pending tasks only: {e}")
        return None
    if worker is None:
        get_logger().debug("litellm LoggingWorker unavailable; draining pending tasks only")
    return worker


def _is_litellm_task(task) -> bool:
    """
    True when a pending task belongs to litellm's deferred-logging machinery.

    Coroutines carry no __module__, so the defining module is read off the frame
    globals. Anything we cannot introspect counts as unrelated.
    """
    try:
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        if frame is None:
            return False
        module = frame.f_globals.get("__name__", "")
        code = getattr(frame, "f_code", None)
        if code is None:
            return False
        function = code.co_name
        if module == "litellm.utils":
            return function == "_client_async_logging_helper"
        if module == "litellm.litellm_core_utils.logging_worker":
            return function in {"_process_log_task", "_aggressively_clear_queue_async", "_retry_enqueue_task"}
        if module == "litellm.litellm_core_utils.litellm_logging":
            return function in {
                "async_success_handler",
                "async_failure_handler",
                "dispatch_success_handlers",
                "dispatch_failure_handlers",
            }
        if module == "litellm._service_logger":
            return function.startswith("async_service_")
        if module.startswith("litellm.integrations."):
            return function in {"async_send_batch", "async_send_message", "async_upload_data_to_s3"}
        return False
    except Exception:
        return False


def _get_litellm_service_datadog_logger():
    """Return LiteLLM's existing service Datadog logger without importing or initializing it."""
    try:
        logging_utils = sys.modules.get("litellm.litellm_core_utils.logging_utils")
        service_logger = getattr(logging_utils, "_service_logger", None)
        return getattr(service_logger, "dd_logger", None)
    except Exception as e:
        get_logger().debug(f"Unable to resolve litellm service Datadog logger: {e}")
        return None


def _matches_litellm_callback_type(logger, expected_type) -> bool:
    try:
        return any(
            (logger_type.__module__, logger_type.__name__) == expected_type
            for logger_type in type(logger).__mro__
        )
    except Exception as e:
        get_logger().debug(f"Unable to inspect litellm callback integration: {e}")
        return False


def _resolve_litellm_callbacks(callback):
    """Return the initialized loggers behind a callback registration."""
    if not isinstance(callback, str):
        loggers = [callback]
        if _matches_litellm_callback_type(callback, _LITELLM_BATCH_CALLBACK_TYPES["datadog"]):
            service_datadog_logger = _get_litellm_service_datadog_logger()
            if service_datadog_logger is not None and _matches_litellm_callback_type(
                service_datadog_logger,
                _LITELLM_BATCH_CALLBACK_TYPES["datadog"],
            ):
                loggers.append(service_datadog_logger)
        return loggers
    try:
        import litellm.litellm_core_utils.litellm_logging as litellm_logging
    except ImportError:
        return []
    try:
        loggers = list(getattr(litellm_logging, "_in_memory_loggers", ()))
        expected_type = _LITELLM_BATCH_CALLBACK_TYPES.get(callback)
        if expected_type is None:
            return []
        if callback == "datadog":
            service_datadog_logger = _get_litellm_service_datadog_logger()
            if service_datadog_logger is not None:
                loggers.append(service_datadog_logger)
        return [
            logger
            for logger in loggers
            if _matches_litellm_callback_type(logger, expected_type)
        ]
    except Exception as e:
        get_logger().debug(f"Unable to resolve litellm callback integration {callback}: {e}")
        return []


def _is_async_callable(callable_obj) -> bool:
    """Return whether calling an object produces coroutine work without running synchronously."""
    try:
        return inspect.iscoroutinefunction(callable_obj) or (
            callable(callable_obj) and inspect.iscoroutinefunction(callable_obj.__call__)
        )
    except Exception:
        return False


def _litellm_batch_callbacks_registered() -> bool:
    """Return whether an existing callback registration may need a batch flush."""
    for attribute in _LITELLM_CALLBACK_ATTRS:
        try:
            registered = getattr(litellm, attribute, None)
            if registered is None:
                continue
            callbacks = registered if isinstance(registered, (list, tuple, set)) else (registered,)
        except Exception as e:
            get_logger().debug(f"Unable to inspect litellm callback registration {attribute}: {e}")
            continue

        for callback in callbacks:
            try:
                if isinstance(callback, str):
                    if callback in _LITELLM_BATCH_CALLBACK_TYPES:
                        return True
                else:
                    flush_queue = getattr(callback, "flush_queue", None)
                    if _is_async_callable(flush_queue):
                        return True
            except Exception as e:
                get_logger().debug(f"Unable to inspect litellm callback registration {attribute}: {e}")
    return False


async def _flush_litellm_batch_callbacks(timeout: float, cleanup_deadline_state=None) -> None:
    """Flush async callback integrations that buffer events outside LoggingWorker."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)

    callbacks = []
    for attribute in _LITELLM_CALLBACK_ATTRS:
        try:
            registered = getattr(litellm, attribute, None)
        except Exception as e:
            get_logger().debug(f"Unable to inspect litellm callback registration {attribute}: {e}")
            continue
        if registered is None:
            continue
        if isinstance(registered, (list, tuple, set)):
            callbacks.extend(registered)
        else:
            callbacks.append(registered)

    async def flush_callback(flush_queue) -> None:
        try:
            result = flush_queue()
            if hasattr(result, "__await__"):
                await result
        except Exception as e:
            get_logger().warning(f"Failed to flush litellm callback integration: {e}")

    resolved_callbacks = []
    for callback in callbacks:
        resolved_callbacks.extend(_resolve_litellm_callbacks(callback))

    seen = set()
    flush_queues = []
    for callback in resolved_callbacks:
        if id(callback) in seen:
            continue
        seen.add(id(callback))
        try:
            flush_queue = getattr(callback, "flush_queue", None)
        except Exception as e:
            get_logger().warning(f"Failed to inspect litellm callback integration: {e}")
            continue
        if callable(flush_queue):
            if not _is_async_callable(flush_queue):
                callback_type = type(callback)
                get_logger().warning(
                    "Skipping synchronous litellm callback integration flush for "
                    f"{callback_type.__module__}.{callback_type.__qualname__} because the drain deadline "
                    "cannot bound it"
                )
                continue
            flush_queues.append(flush_queue)

    if timeout <= 0 or not flush_queues:
        return
    flush_tasks = [asyncio.create_task(flush_callback(flush_queue)) for flush_queue in flush_queues]
    remaining = deadline - loop.time()
    try:
        done, pending = await asyncio.wait(flush_tasks, timeout=max(0.0, remaining))
    except asyncio.CancelledError:
        if cleanup_deadline_state is None:
            cleanup_deadline_state = [None]
        if cleanup_deadline_state[0] is None:
            cleanup_deadline = loop.time() + CANCELLATION_CLEANUP_SECONDS
            if len(cleanup_deadline_state) > 2:
                cleanup_deadline = min(cleanup_deadline, cleanup_deadline_state[2])
            else:
                cleanup_deadline = min(cleanup_deadline, deadline)
            cleanup_deadline_state[0] = cleanup_deadline
        try:
            await _cancel_and_reap_tasks(
                flush_tasks,
                max(0.0, cleanup_deadline_state[0] - loop.time()),
                protect_from_cancellation=True,
            )
        except Exception as e:
            _log_cleanup_debug("batch callback cancellation cleanup", e)
        raise
    _log_task_exceptions(done)
    if pending:
        await _cancel_and_reap_tasks(pending, 0)
        get_logger().warning("litellm callback integration did not flush within timeout")


async def _cancel_and_reap_tasks(
    tasks,
    cleanup_timeout: float,
    *,
    protect_from_cancellation: bool = False,
) -> None:
    """Cancel tasks and briefly reap cooperative cancellation when allowed."""
    tasks = set(tasks)
    done = {task for task in tasks if task.done()}
    pending = tasks - done
    _log_task_exceptions(done)

    def reap_task(completed) -> None:
        _log_task_exceptions([completed])

    for task in pending:
        task.add_done_callback(reap_task)
        task.cancel()
    if not pending:
        return

    async def wait_for_cancellation() -> None:
        await asyncio.wait(pending, timeout=max(0.0, cleanup_timeout))

    if not protect_from_cancellation:
        await wait_for_cancellation()
        return

    cleanup_waiter = asyncio.create_task(wait_for_cancellation())
    while not cleanup_waiter.done():
        try:
            await asyncio.shield(cleanup_waiter)
        except asyncio.CancelledError:
            # The original cancellation is already being handled. Keep the fixed-time
            # cleanup waiter alive so repeated cancellation cannot detach child tasks.
            continue
    cleanup_waiter.result()


async def _reap_cancelled_tasks(tasks, cleanup_timeout: float) -> None:
    """Wait briefly for already-cancelled tasks without cancelling them again."""
    pending = {task for task in tasks if not task.done()}
    if not pending:
        return

    async def wait_for_cancellation() -> None:
        await asyncio.wait(pending, timeout=max(0.0, cleanup_timeout))

    cleanup_waiter = asyncio.create_task(wait_for_cancellation())
    while not cleanup_waiter.done():
        try:
            await asyncio.shield(cleanup_waiter)
        except asyncio.CancelledError:
            # The original cancellation is already being handled. Repeated
            # cancellation must not detach the bounded cleanup waiter.
            continue
    cleanup_waiter.result()


def _log_cleanup_debug(context: str, error: Exception) -> None:
    """Log best-effort cleanup diagnostics without disturbing cancellation."""
    try:
        get_logger().debug(f"Failed to inspect {context} while draining litellm callbacks: {error}")
    except Exception:
        # Best-effort diagnostics must not interrupt callback draining.
        pass


def _log_task_exceptions(tasks) -> None:
    """Consume exceptions from finished tasks, so they don't resurface at shutdown."""
    for task in tasks:
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            continue
        if exception is not None:
            get_logger().warning(f"litellm callback task raised: {exception}")


def litellm_callbacks_registered() -> bool:
    """
    True when anything is listening for litellm callbacks.

    Covers litellm's module-level lists as well as the config flag, since callers
    embedding pr-agent can register callbacks without touching configuration.toml.
    """
    litellm_settings = get_settings().get("litellm", {})
    if litellm_settings and litellm_settings.get("enable_callbacks", False):
        return True
    for name in _LITELLM_CALLBACK_ATTRS:
        registered = getattr(litellm, name, None)
        if isinstance(registered, (list, tuple, set)):
            if registered:
                return True
        elif registered is not None:
            return True
    return False


async def drain_litellm_callbacks(timeout: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS) -> None:
    """
    Let litellm's deferred callbacks run before the event loop closes.

    litellm defers logging twice - a create_task when the completion resolves,
    which then enqueues onto a global LoggingWorker - and asyncio.run() cancels
    both when the command returns. Draining lets the callbacks reach the queue;
    the flush waits for them to finish. Best-effort: errors are logged, not raised.
    """
    try:
        if timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Try to stabilize callback producers before the terminal batch flush. If the producer
        # budget expires, the batch flush remains a bounded best-effort shutdown step.
        phase_reserve = (
            min(FLUSH_RESERVE_SECONDS, timeout / (MAX_DRAIN_ROUNDS + 2))
            if _litellm_batch_callbacks_registered()
            else 0.0
        )
        producer_deadline = deadline - 2 * phase_reserve
        batch_deadline = deadline - phase_reserve
        worker = _get_global_logging_worker()
        current_task = asyncio.current_task()

        def is_active_worker_task(task, worker_task) -> bool:
            return (
                isinstance(task, asyncio.Task)
                and task is not current_task
                and task is not worker_task
                and task.get_loop() is loop
                and not task.done()
            )

        def safe_worker_attr(name, default=None):
            if worker is None:
                return default
            try:
                return getattr(worker, name, default)
            except Exception as e:
                _log_cleanup_debug(f"litellm worker {name}", e)
                return default

        def pending_litellm_tasks():
            worker_task = safe_worker_attr("_worker_task")
            try:
                running_tasks = safe_worker_attr("_running_tasks") or ()
                worker_running_tasks = [
                    task for task in running_tasks
                    if is_active_worker_task(task, worker_task)
                ]
            except Exception as e:
                _log_cleanup_debug("litellm worker running tasks", e)
                worker_running_tasks = []
            try:
                all_litellm_tasks = [task for task in asyncio.all_tasks()
                                     if task is not current_task and task is not worker_task
                                     and _is_litellm_task(task)]
            except Exception as e:
                _log_cleanup_debug("pending litellm callback tasks", e)
                all_litellm_tasks = []
            # Combine both sources to avoid missing tasks that are in the
            # intermediary create_task hop while worker internals are also active.
            return list(dict.fromkeys([*worker_running_tasks, *all_litellm_tasks]))

        def worker_has_pending_work() -> bool:
            if worker is None:
                return False
            worker_task = safe_worker_attr("_worker_task")
            try:
                running_tasks = safe_worker_attr("_running_tasks") or ()
                if any(is_active_worker_task(task, worker_task) for task in running_tasks):
                    return True
            except Exception as e:
                _log_cleanup_debug("litellm worker running tasks", e)
            queue = safe_worker_attr("_queue")
            if queue is None:
                return False
            try:
                qsize = getattr(queue, "qsize", None)
                return bool(qsize()) if callable(qsize) else False
            except Exception as e:
                _log_cleanup_debug("litellm worker queue size", e)
                return False

        async def cancel_pending_callbacks(
            cleanup_deadline_state,
            *,
            protect_from_cancellation: bool = True,
        ) -> None:
            if cleanup_deadline_state[0] is None:
                cleanup_deadline_state[0] = min(
                    loop.time() + CANCELLATION_CLEANUP_SECONDS,
                    deadline,
                )
            if len(cleanup_deadline_state) == 1:
                cleanup_deadline_state.append(set())
            cancelled_tasks = cleanup_deadline_state[1]
            while True:
                pending = [task for task in pending_litellm_tasks() if task not in cancelled_tasks]
                if not pending:
                    return
                remaining = max(0.0, cleanup_deadline_state[0] - loop.time())
                cancelled_tasks.update(pending)
                await _cancel_and_reap_tasks(
                    pending,
                    remaining,
                    protect_from_cancellation=protect_from_cancellation,
                )
                if remaining <= 0 or loop.time() >= cleanup_deadline_state[0]:
                    # A callback may create another callback task while handling
                    # cancellation. Deliver cancellation to that final snapshot
                    # even when there is no cleanup time left to await it.
                    final_pending = [
                        task for task in pending_litellm_tasks()
                        if task not in cancelled_tasks
                    ]
                    if final_pending:
                        cancelled_tasks.update(final_pending)
                        await _cancel_and_reap_tasks(
                            final_pending,
                            0,
                            protect_from_cancellation=protect_from_cancellation,
                        )
                    return

        warned_callback_tasks = set()

        async def drain_pending_tasks(
            end_time: float,
            *,
            warn_on_timeout: bool = True,
            cancel_pending_on_cancel: bool = False,
            cancel_pending_on_incomplete: bool = False,
            cleanup_deadline_state=None,
        ):
            async def handle_incomplete(pending, reason: str) -> None:
                tasks_to_warn = [task for task in pending if task not in warned_callback_tasks]
                if warn_on_timeout and tasks_to_warn:
                    warned_callback_tasks.update(tasks_to_warn)
                    get_logger().warning(
                        f"{len(tasks_to_warn)} callback tasks({[task.get_coro() for task in tasks_to_warn]}) {reason}"
                    )
                if cancel_pending_on_incomplete:
                    # The normal drain deadline has already expired. Deliver
                    # cancellation without extending it, and install exception
                    # reapers for tasks that need another loop turn to finish.
                    incomplete_cleanup_state = [loop.time(), set()]
                    try:
                        await cancel_pending_callbacks(
                            incomplete_cleanup_state,
                            protect_from_cancellation=False,
                        )
                    except asyncio.CancelledError:
                        # Preserve the external cancellation, but first rescan
                        # for callbacks spawned while handling task cancellation.
                        incomplete_cleanup_state[0] = min(
                            loop.time() + CANCELLATION_CLEANUP_SECONDS,
                            deadline,
                        )
                        try:
                            await _reap_cancelled_tasks(
                                incomplete_cleanup_state[1],
                                max(0.0, incomplete_cleanup_state[0] - loop.time()),
                            )
                            await cancel_pending_callbacks(incomplete_cleanup_state)
                        except Exception as e:
                            _log_cleanup_debug("interrupted callback cancellation cleanup", e)
                        raise
                    except Exception as e:
                        _log_cleanup_debug("incomplete callback cancellation cleanup", e)

            for _ in range(MAX_DRAIN_ROUNDS):
                remaining = end_time - loop.time()
                if remaining <= 0:
                    pending = pending_litellm_tasks()
                    if pending:
                        await handle_incomplete(pending, "did not complete within timeout")
                    return
                pending = pending_litellm_tasks()
                if not pending:
                    return

                try:
                    done, still_pending = await asyncio.wait(pending, timeout=remaining)
                except asyncio.CancelledError:
                    if cancel_pending_on_cancel:
                        if cleanup_deadline_state is None:
                            cleanup_deadline_state = [None]
                        try:
                            await cancel_pending_callbacks(cleanup_deadline_state)
                        except Exception as e:
                            _log_cleanup_debug("pending callback cancellation cleanup", e)
                    raise
                _log_task_exceptions(done)
                if still_pending:
                    await handle_incomplete(still_pending, "did not complete within timeout")
                    return

            pending = pending_litellm_tasks()
            if pending:
                await handle_incomplete(pending, "did not quiesce after bounded drain rounds")

        worker_flush_task = None
        worker_flush_failed = False

        async def run_worker_flush() -> None:
            nonlocal worker_flush_failed
            try:
                result = worker.flush()
                if hasattr(result, "__await__"):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                worker_flush_failed = True
                get_logger().warning(f"Failed to flush litellm callback queue: {e}")

        def reap_worker_flush() -> bool:
            nonlocal worker_flush_task
            if worker_flush_task is not None and worker_flush_task.done():
                _log_task_exceptions([worker_flush_task])
                worker_flush_task = None
            return worker_flush_task is not None

        async def flush_worker(end_time: float) -> bool:
            nonlocal worker_flush_task
            if worker is None:
                return True
            reap_worker_flush()
            if worker_flush_failed:
                return False
            if worker_flush_task is None:
                if end_time - loop.time() <= 0:
                    return False
                worker_flush_task = asyncio.create_task(run_worker_flush())
            done, _pending = await asyncio.wait(
                {worker_flush_task},
                timeout=max(0.0, end_time - loop.time()),
            )
            if done:
                reap_worker_flush()
                return True
            return False

        async def cancel_worker_flush(
            cleanup_timeout: float,
            *,
            protect_from_cancellation: bool = False,
        ) -> None:
            nonlocal worker_flush_task
            if worker_flush_task is None:
                return
            if not worker_flush_task.done():
                await _cancel_and_reap_tasks(
                    {worker_flush_task},
                    cleanup_timeout,
                    protect_from_cancellation=protect_from_cancellation,
                )
            else:
                _log_task_exceptions([worker_flush_task])
            worker_flush_task = None

        cancellation_cleanup_deadline = [None, set(), deadline]
        producer_quiescent = False
        drain_cancelled = False
        try:
            for round_index in range(MAX_DRAIN_ROUNDS):
                round_start = loop.time()
                remaining_rounds = MAX_DRAIN_ROUNDS - round_index
                round_window = max(0.0, producer_deadline - round_start) / remaining_rounds
                task_deadline = round_start + round_window / 3
                worker_deadline = round_start + 2 * round_window / 3
                round_deadline = min(producer_deadline, round_start + round_window)

                await drain_pending_tasks(
                    task_deadline,
                    warn_on_timeout=False,
                    cancel_pending_on_cancel=True,
                    cleanup_deadline_state=cancellation_cleanup_deadline,
                )
                if worker is not None:
                    await flush_worker(worker_deadline)
                await drain_pending_tasks(
                    round_deadline,
                    warn_on_timeout=False,
                    cancel_pending_on_cancel=True,
                    cleanup_deadline_state=cancellation_cleanup_deadline,
                )

                if not pending_litellm_tasks() and not worker_has_pending_work() and not reap_worker_flush():
                    producer_quiescent = True
                    break

            for _ in range(2):
                if producer_quiescent or worker is None or loop.time() >= producer_deadline:
                    break
                await drain_pending_tasks(
                    producer_deadline,
                    warn_on_timeout=False,
                    cancel_pending_on_cancel=True,
                    cleanup_deadline_state=cancellation_cleanup_deadline,
                )
                await flush_worker(producer_deadline)
                await drain_pending_tasks(
                    producer_deadline,
                    warn_on_timeout=False,
                    cancel_pending_on_cancel=True,
                    cleanup_deadline_state=cancellation_cleanup_deadline,
                )
                if not pending_litellm_tasks() and not worker_has_pending_work() and not reap_worker_flush():
                    producer_quiescent = True
        except asyncio.CancelledError:
            drain_cancelled = True
            try:
                await cancel_pending_callbacks(cancellation_cleanup_deadline)
            except Exception as e:
                _log_cleanup_debug("producer cancellation cleanup", e)
            raise
        finally:
            try:
                await cancel_worker_flush(
                    max(0.0, cancellation_cleanup_deadline[0] - loop.time())
                    if drain_cancelled
                    else 0,
                    protect_from_cancellation=drain_cancelled,
                )
            except asyncio.CancelledError:
                if not drain_cancelled:
                    if cancellation_cleanup_deadline[0] is None:
                        cancellation_cleanup_deadline[0] = min(
                            loop.time() + CANCELLATION_CLEANUP_SECONDS,
                            deadline,
                        )
                    try:
                        if worker_flush_task is not None:
                            await _reap_cancelled_tasks(
                                {worker_flush_task},
                                max(0.0, cancellation_cleanup_deadline[0] - loop.time()),
                            )
                        await cancel_pending_callbacks(cancellation_cleanup_deadline)
                    except Exception as e:
                        _log_cleanup_debug("interrupted worker cancellation cleanup", e)
                raise
            except Exception as e:
                if not drain_cancelled:
                    raise
                _log_cleanup_debug("worker cancellation cleanup", e)

        if not producer_quiescent:
            timeout_expired = loop.time() >= producer_deadline
            incomplete_reason = (
                "did not stabilize before the batch flush deadline"
                if timeout_expired
                else "did not quiesce after bounded drain rounds"
            )
            try:
                pending = pending_litellm_tasks()
                if pending:
                    warned_callback_tasks.update(pending)
                    get_logger().warning(
                        f"{len(pending)} callback tasks({[task.get_coro() for task in pending]}) "
                        f"{incomplete_reason}"
                    )
                elif not worker_flush_failed:
                    get_logger().warning(f"litellm callback queue or worker flush {incomplete_reason}")
            except Exception as e:
                if not worker_flush_failed:
                    get_logger().warning(
                        f"litellm callback queue or worker flush {incomplete_reason}; "
                        f"unable to inspect pending tasks: {e}"
                    )
        try:
            await _flush_litellm_batch_callbacks(
                max(0.0, batch_deadline - loop.time()),
                cancellation_cleanup_deadline,
            )
        except asyncio.CancelledError:
            if cancellation_cleanup_deadline[0] is None:
                cancellation_cleanup_deadline[0] = min(
                    loop.time() + CANCELLATION_CLEANUP_SECONDS,
                    deadline,
                )
            try:
                await cancel_pending_callbacks(cancellation_cleanup_deadline)
            except Exception as e:
                _log_cleanup_debug("detached upload cancellation cleanup", e)
            raise

        # Batch integrations such as S3 may create upload tasks while flushing.
        await drain_pending_tasks(
            deadline,
            warn_on_timeout=True,
            cancel_pending_on_cancel=True,
            cancel_pending_on_incomplete=True,
            cleanup_deadline_state=cancellation_cleanup_deadline,
        )
    except Exception as e:
        get_logger().warning(f"Failed to drain litellm callbacks: {e}")
