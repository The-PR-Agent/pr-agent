"""MOSAICO A2A executor + the /health LLM probe.

PRAgentExecutor.execute runs a single non-streaming path: it FIRST installs a
request-scoped deepcopy of global_settings into starlette_context (so the tool run
mutates only the per-request copy — load-bearing isolation under concurrency), routes
the inbound text to a pr-agent command, and completes the Task with the rendered
markdown. health_check issues a single, NON-retry-wrapped litellm probe."""
import copy

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils import new_agent_text_message, new_task
from starlette_context import context as sctx

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger
from pr_agent.mosaico.dispatch import route_and_run
from pr_agent.mosaico.observability import (langfuse_span,
                                            mosaico_log_context,
                                            parse_observability_metadata)


class PRAgentExecutor(AgentExecutor):
    """Turns a MOSAICO message/send into a pr-agent run and returns a text Message."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            # Request-scoped settings: the tool run mutates ONLY this deepcopy, never the
            # shared global. get_settings() resolves to sctx["settings"] when present.
            sctx["settings"] = copy.deepcopy(global_settings)

            user_text = context.get_user_input() or ""
            meta = parse_observability_metadata(context.metadata)
            with mosaico_log_context(meta, task.context_id), langfuse_span(meta, task.context_id):
                markdown = await route_and_run(user_text)
            await updater.complete(new_agent_text_message(markdown or "(no output produced)"))
        except Exception as e:
            get_logger().exception("MOSAICO task failed")
            await updater.failed(new_agent_text_message(f"Error: {e}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported by the PR-Agent solution agent")


async def health_check() -> str:
    """LLM-connectivity probe for /health. NO-RETRY: bypasses pr-agent's retry-wrapped
    LiteLLMAIHandler.chat_completion (which would retry-hang a down LLM) and issues a
    single litellm.acompletion, after applying the MOSAICO LLM settings."""
    try:
        import litellm

        # Construct the handler purely for its side effect of applying pr-agent's LLM
        # config (api_base/key/callbacks/etc.) onto the litellm module — do NOT call its
        # retry-wrapped chat_completion.
        from pr_agent.algo.ai_handlers.litellm_ai_handler import \
            LiteLLMAIHandler
        handler = LiteLLMAIHandler()

        model = get_settings().get("CONFIG.MODEL", None)
        if not model:
            return "Unhealthy: no model configured"

        kwargs = {
            "model": model,
            "messages": [{"role": "system", "content": "Say ping"}],
            "max_tokens": 10,
            "timeout": 10,
        }
        if getattr(handler, "api_base", None):
            kwargs["api_base"] = handler.api_base

        await litellm.acompletion(**kwargs)
        return "OK"
    except Exception as e:
        get_logger().warning(f"MOSAICO health_check unhealthy: {e}")
        return f"Unhealthy: {e}"
