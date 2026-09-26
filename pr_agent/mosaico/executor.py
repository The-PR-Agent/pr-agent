"""MOSAICO A2A executor + the /health LLM probe (A2A 1.0).

PRAgentExecutor.execute runs a single non-streaming path: it FIRST installs a
request-scoped deepcopy of global_settings into starlette_context (so the tool run
mutates only the per-request copy — load-bearing isolation under concurrency), routes
the inbound text to a pr-agent command, and completes the Task with the rendered
markdown.

For a new request, a working Task is published before the long-running route starts so
that A2A cancellation can resolve the task from the store. On success the review text
is published as an artifact (RISK 2: the reference agent's pollTask reads
task.artifacts, not the completion message), then complete().

On any failure — including ok=False from the router (Fix C) — an artifact containing
the error text is published first (required to initialise the task before sending a
TaskStatusUpdateEvent), then failed().  The first event to the ActiveTask MUST be a
TaskArtifactUpdateEvent (not a TaskStatusUpdateEvent), otherwise the SDK raises
"Agent should enqueue Task before TaskStatusUpdateEvent event".

health_check issues a single, NON-retry-wrapped litellm probe."""
import asyncio
import copy
from collections import deque
from math import isfinite

from a2a.helpers.proto_helpers import get_message_text
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.inmemory_task_store import resolve_user_scope
from a2a.types import Part, Role, Task, TaskState, TaskStatus
from starlette_context import context as sctx

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger
from pr_agent.mosaico.dispatch import _find_pr_url, _looks_like_diff, route_and_run_result
from pr_agent.mosaico.observability import langfuse_span, mosaico_log_context, parse_observability_metadata

_MAX_CONTEXT_HISTORY_TASKS = 1000


class PRAgentExecutor(AgentExecutor):
    """Turns a MOSAICO message/send into a pr-agent run and returns a Task."""

    def __init__(self, task_store=None):
        self.task_store = task_store
        self._recent_task_ids: dict[tuple[str, str], deque[str]] = {}

    async def _history_for_context(self, context: RequestContext) -> list[str]:
        current = context.get_user_input() or ""
        if self.task_store is None or _find_pr_url(current) or _looks_like_diff(current):
            return []

        # Recover prior user turns in input order from the owner-scoped index.
        # Keep late task status updates from changing the review target.
        limit = get_settings().get("MOSAICO.CONTEXT_HISTORY_MAX_TASKS", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CONTEXT_HISTORY_TASKS:
            raise ValueError("MOSAICO context history max tasks must be an integer from 1 to 1000")
        key = (resolve_user_scope(context.call_context), context.context_id)
        recent_ids = list(self._recent_task_ids.get(key, ()))[::-1]
        turns = []
        found_context = False
        prior_count = 0
        for task_id in recent_ids:
            if task_id == context.task_id:
                continue
            if prior_count >= limit:
                break
            prior_count += 1
            task = await self.task_store.get(task_id, context.call_context)
            if task is None:
                continue
            for message in reversed(task.history):
                if message.role == Role.ROLE_USER:
                    text = get_message_text(message)
                    if text:
                        turns.append(text)
                        if _find_pr_url(text) or _looks_like_diff(text):
                            found_context = True
                            break
            if found_context:
                break
        return list(reversed(turns))

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # In A2A 1.0 DefaultRequestHandler sets task_id/context_id on the context before
        # execute() is reached (the SDK also rejects non-1.0 requests upstream).  We still
        # build the updater INSIDE the try and validate the fields explicitly: a context
        # missing them surfaces as a controlled, logged failure here instead of an uncaught
        # crash before any task event is emitted.
        updater = None
        try:
            if not context.task_id or not context.context_id:
                raise ValueError("A2A 1.0 RequestContext missing task_id/context_id")
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)

            # A2A cancellation looks up the persisted Task before invoking this
            # executor's cancel() callback. Establish the task before starting the
            # long-running route, but do not replace an existing task on follow-up work.
            if context.current_task is None:
                if self.task_store is not None:
                    key = (resolve_user_scope(context.call_context), context.context_id)
                    self._recent_task_ids.setdefault(
                        key, deque(maxlen=_MAX_CONTEXT_HISTORY_TASKS + 1)
                    ).append(context.task_id)
                await event_queue.enqueue_event(
                    Task(
                        id=context.task_id,
                        context_id=context.context_id,
                        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                        history=[context.message] if context.message else [],
                    )
                )

            # Request-scoped settings: the tool run mutates ONLY this deepcopy, never the
            # shared global. get_settings() resolves to sctx["settings"] when present.
            sctx["settings"] = copy.deepcopy(global_settings)

            user_text = context.get_user_input() or ""
            history = await self._history_for_context(context)
            meta = parse_observability_metadata(context.metadata)
            with mosaico_log_context(meta, context.context_id), \
                    langfuse_span(meta, context.context_id):
                if history:
                    result = await route_and_run_result(user_text, context_history=history)
                else:
                    result = await route_and_run_result(user_text)

            output_text = result.text or "(no output produced)"
            # ALWAYS add_artifact first: the SDK requires a TaskArtifactUpdateEvent
            # before any TaskStatusUpdateEvent (otherwise it raises InvalidAgentResponseError
            # "Agent should enqueue Task before TaskStatusUpdateEvent").  The artifact also
            # delivers the review text to the reference agent's pollTask (RISK 2).
            await updater.add_artifact([Part(text=output_text)])
            if result.ok:
                await updater.complete()
            else:
                # ok=False means a recoverable routing/fetch failure (Fix C).
                msg = updater.new_agent_message([Part(text=output_text)])
                await updater.failed(msg)
        except Exception as e:
            get_logger().exception("MOSAICO task failed")
            if updater is None:
                # task_id/context_id were missing, so we cannot create a Task to fail.
                # Re-raise so the handler returns a controlled JSON-RPC error.
                raise
            error_text = f"Error: {e}"
            # Must add_artifact first to initialise the task before failed().
            await updater.add_artifact([Part(text=error_text)])
            msg = updater.new_agent_message([Part(text=error_text)])
            await updater.failed(msg)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A 1.0 RequestContext missing task_id/context_id")

        # ActiveTask cancels the producer before invoking this callback. The initial
        # Task event from execute() makes the task/context identifiers resolvable here.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


async def health_check() -> str:
    """LLM-connectivity probe for /health. Bypasses pr-agent's retry-wrapped
    LiteLLMAIHandler.chat_completion and issues a single isolated completion,
    after applying the MOSAICO LLM settings."""
    try:
        import litellm

        # Construct the handler to snapshot request-local provider credentials and
        # routing without using its retry-wrapped chat_completion.
        from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

        handler = LiteLLMAIHandler()

        model = get_settings().get("CONFIG.MODEL", None)
        if not model:
            return "Unhealthy: no model configured"
        timeout = get_settings().get("MOSAICO.HEALTH_TIMEOUT_SECONDS", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not isfinite(timeout) or timeout <= 0:
            raise ValueError("MOSAICO health timeout must be a finite positive number")
        # Bound cooperative waits, including request preparation, not just HTTP I/O.
        # Synchronous handler construction and blocking SDK work cannot be interrupted.
        async with asyncio.timeout(timeout):
            await handler.probe_completion(
                model, timeout=timeout, _completion=litellm.acompletion,
            )
        return "OK"
    except Exception as e:
        get_logger().warning(f"MOSAICO health_check unhealthy: {type(e).__name__}")
        return "Unhealthy: LLM probe failed"
