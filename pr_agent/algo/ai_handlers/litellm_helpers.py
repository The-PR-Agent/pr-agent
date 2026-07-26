import asyncio
import json

import litellm
import openai

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30
MAX_DRAIN_ROUNDS = 5
FLUSH_RESERVE_SECONDS = 1.0  # held back from the timeout so the final flush always gets a turn


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
    Return litellm's module-global LoggingWorker, or None if it is unavailable.

    Lives under litellm's internal litellm_core_utils, so it may move; losing it
    costs the queue flush, not the task drain.
    """
    try:
        from litellm.litellm_core_utils.logging_worker import \
            GLOBAL_LOGGING_WORKER
        return GLOBAL_LOGGING_WORKER
    except ImportError:
        get_logger().debug("litellm LoggingWorker unavailable; draining pending tasks only")
        return None


def _is_litellm_task(task) -> bool:
    """
    True when a pending task belongs to litellm's deferred-logging machinery.

    Coroutines carry no __module__, so the defining module is read off the frame
    globals. Anything we cannot introspect counts as unrelated.
    """
    try:
        frame = getattr(task.get_coro(), "cr_frame", None)
        module = frame.f_globals.get("__name__", "") if frame is not None else ""
        return module.split(".")[0] == "litellm"
    except Exception:
        return False


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
    return any(
        getattr(litellm, name, None)
        for name in ("callbacks", "success_callback", "failure_callback", "service_callback",
                     "_async_success_callback", "_async_failure_callback")
    )


async def drain_litellm_callbacks(timeout: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS) -> None:
    """
    Let litellm's deferred callbacks run before the event loop closes.

    litellm defers logging twice - a create_task when the completion resolves,
    which then enqueues onto a global LoggingWorker - and asyncio.run() cancels
    both when the command returns. Draining lets the callbacks reach the queue;
    the flush waits for them to finish. Best-effort: errors are logged, not raised.
    """
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Split the budget rather than extending it, so timeout bounds the whole call.
        task_deadline = deadline - min(FLUSH_RESERVE_SECONDS, timeout / 2)
        worker = _get_global_logging_worker()

        for _ in range(MAX_DRAIN_ROUNDS):
            remaining = task_deadline - loop.time()
            if remaining <= 0:
                break
            # The worker's own loop task never finishes, so waiting on it would
            # burn the full timeout on every run.
            worker_task = getattr(worker, "_worker_task", None)
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task() and task is not worker_task
                       and _is_litellm_task(task)]
            if not pending:
                break
            # Re-snapshot next round: a drained task may have spawned the one that enqueues.
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
        # Reached even after a timeout above, or already-queued callbacks are dropped.
        try:
            await asyncio.wait_for(worker.flush(), timeout=max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            get_logger().warning("litellm callback queue did not flush within timeout")
    except Exception as e:
        get_logger().warning(f"Failed to drain litellm callbacks: {e}")
