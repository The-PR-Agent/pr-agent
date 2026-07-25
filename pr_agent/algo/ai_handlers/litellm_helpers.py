import asyncio
import json

import litellm
import openai

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Max seconds to wait for pending litellm callbacks before giving up and exiting.
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30
# Bound on how many times we re-snapshot asyncio.all_tasks() while draining. Each
# pass can surface tasks spawned by the previous pass; a handful of rounds is
# plenty and keeps a pathological callback from looping us forever.
MAX_DRAIN_ROUNDS = 5
# Grace given to the final queue flush when the task drain already used the budget,
# so callbacks that reached the queue are not dropped right at the deadline.
FLUSH_GRACE_SECONDS = 1.0


async def _handle_streaming_response(response):
    """
    Handle streaming response from acompletion and collect the full response.

    Args:
        response: The streaming response object from acompletion

    Returns:
        tuple: (full_response_content, finish_reason)
    """
    full_response = ""
    finish_reason = None

    try:
        async for chunk in response:
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
    return full_response, finish_reason


class MockResponse:
    """Mock response object for streaming models to enable consistent logging."""

    def __init__(self, resp, finish_reason):
        self._data = {
            "choices": [
                {
                    "message": {"content": resp},
                    "finish_reason": finish_reason
                }
            ]
        }

    def dict(self):
        return self._data


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
    Return litellm's module-global LoggingWorker, or None if unavailable.

    Imported lazily and defensively: the worker lives under litellm's internal
    `litellm_core_utils` package, so a future litellm may move it. Losing it only
    costs us the queue flush, not the task drain.
    """
    try:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
        return GLOBAL_LOGGING_WORKER
    except ImportError:
        get_logger().debug("litellm LoggingWorker unavailable; draining pending tasks only")
        return None


def _is_litellm_task(task) -> bool:
    """
    True when a pending task belongs to litellm's deferred-logging machinery.

    Keeps the drain from waiting on unrelated background work, which would otherwise
    let any long-running task hold up process exit for the whole timeout. Coroutine
    objects carry no ``__module__``, so read the defining module off the frame's
    globals; anything we cannot introspect is treated as unrelated.
    """
    try:
        frame = getattr(task.get_coro(), "cr_frame", None)
        module = frame.f_globals.get("__name__", "") if frame is not None else ""
        return module.split(".")[0] == "litellm"
    except Exception:
        return False


def _log_task_exceptions(tasks) -> None:
    """
    Retrieve exceptions from finished tasks so they are observed, not just dropped.

    Without this, a callback that raises surfaces later as an unhelpful
    "Task exception was never retrieved" during interpreter shutdown.
    """
    for task in tasks:
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            continue
        if exception is not None:
            get_logger().warning(f"litellm callback task raised: {exception}")


def litellm_callbacks_registered() -> bool:
    """
    True when anything is listening for litellm callbacks, so a drain is worthwhile.

    Checks the config flag *and* litellm's module-level lists, because callbacks
    can also be registered programmatically (`litellm.callbacks = [MyLogger()]`)
    by code that embeds pr-agent and never touches configuration.toml.
    """
    litellm_settings = get_settings().get("litellm", {})
    if litellm_settings and litellm_settings.get("enable_callbacks", False):
        return True
    return any(
        getattr(litellm, name, None)
        for name in ("callbacks", "success_callback", "failure_callback", "service_callback",
                     "_async_success_callback", "_async_failure_callback")
    )


async def drain_litellm_callbacks(timeout: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS) -> None:
    """
    Let litellm's deferred success/failure callbacks run before the event loop closes.

    litellm defers async logging twice: once via asyncio.create_task() when the
    completion resolves, and again when that task enqueues the actual callback onto
    a module-global LoggingWorker queue. Entry points that wrap a whole command in
    asyncio.run() (the CLI, the GitHub Action runner) cancel both as soon as the
    command returns, so callbacks silently never fire. Draining stage one lets the
    callbacks reach the queue; flushing the worker waits for them to finish.

    Best-effort by design: this is telemetry, so anything that goes wrong is logged
    and swallowed rather than allowed to fail a command whose result we already have.
    """
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        worker = _get_global_logging_worker()

        for _ in range(MAX_DRAIN_ROUNDS):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # Exclude the worker's own loop task: it runs forever by design, so
            # waiting on it would burn the full timeout on every single run.
            worker_task = getattr(worker, "_worker_task", None)
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task() and task is not worker_task
                       and _is_litellm_task(task)]
            if not pending:
                break
            # Re-snapshot on the next round: a drained task may itself have spawned
            # the task that actually enqueues the callback.
            done, still_pending = await asyncio.wait(pending, timeout=remaining)
            _log_task_exceptions(done)
            if still_pending:
                get_logger().warning(
                    f"{len(still_pending)} callback tasks({[task.get_coro() for task in still_pending]}) "
                    f"did not complete within timeout"
                )
                break

        if worker is None:
            return
        # Always reach the flush, even after a timeout above: callbacks that already
        # made it onto the queue would otherwise be dropped at the buzzer. The grace
        # keeps that final wait bounded.
        flush_timeout = max(FLUSH_GRACE_SECONDS, deadline - loop.time())
        try:
            await asyncio.wait_for(worker.flush(), timeout=flush_timeout)
        except asyncio.TimeoutError:
            get_logger().warning("litellm callback queue did not flush within timeout")
    except Exception as e:
        get_logger().warning(f"Failed to drain litellm callbacks: {e}")
