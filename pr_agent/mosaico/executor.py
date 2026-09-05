"""MOSAICO A2A executor + the /health LLM probe (A2A 1.0).

PRAgentExecutor.execute runs a single non-streaming path: it FIRST installs a
request-scoped deepcopy of global_settings into starlette_context (so the tool run
mutates only the per-request copy — load-bearing isolation under concurrency), routes
the inbound text to a pr-agent command, and completes the Task with the rendered
markdown.

On success the review text is published as an artifact (RISK 2: the reference agent's
pollTask reads task.artifacts, not the completion message), then complete().

On any failure — including ok=False from the router (Fix C) — an artifact containing
the error text is published first (required to initialise the task before sending a
TaskStatusUpdateEvent), then failed().  The first event to the ActiveTask MUST be a
TaskArtifactUpdateEvent (not a TaskStatusUpdateEvent), otherwise the SDK raises
"Agent should enqueue Task before TaskStatusUpdateEvent event".

health_check issues a NON-retry-wrapped litellm probe with at most one static AWS credential fallback."""
import copy

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from starlette_context import context as sctx

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger
from pr_agent.mosaico.dispatch import route_and_run_result
from pr_agent.mosaico.observability import langfuse_span, mosaico_log_context, parse_observability_metadata


class PRAgentExecutor(AgentExecutor):
    """Turns a MOSAICO message/send into a pr-agent run and returns a Task."""

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

            # Request-scoped settings: the tool run mutates ONLY this deepcopy, never the
            # shared global. get_settings() resolves to sctx["settings"] when present.
            sctx["settings"] = copy.deepcopy(global_settings)

            user_text = context.get_user_input() or ""
            meta = parse_observability_metadata(context.metadata)
            with mosaico_log_context(meta, context.context_id), \
                    langfuse_span(meta, context.context_id):
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
        raise NotImplementedError("cancel is not supported by the PR-Agent solution agent")


async def health_check() -> str:
    """LLM-connectivity probe for /health. Bypasses pr-agent's retry-wrapped
    LiteLLMAIHandler.chat_completion and allows only one static AWS credential retry,
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
        await handler.probe_completion(model, _completion=litellm.acompletion)
        return "OK"
    except Exception as e:
        get_logger().warning(f"MOSAICO health_check unhealthy: {type(e).__name__}")
        return "Unhealthy: LLM check failed"
