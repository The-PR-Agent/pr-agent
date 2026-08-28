"""MOSAICO A2A server (A2A 1.0).

Builds a plain Starlette app with A2A routes (agent-card + JSONRPC) plus a GET /health
route, with RawContextMiddleware on the SAME app so every request runs inside a
starlette_context scope (MANDATORY: the executor installs a request-scoped deepcopy of
the settings there; without the scope it raises ContextDoesNotExistError).

Module import side effects (so build_app works in tests): JSON logger, apply_mosaico_env(),
provider registration, and a one-time Langfuse client construction when creds are present.
uvicorn.run is only invoked under __main__."""
import os

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette_context.middleware import RawContextMiddleware

# Fully load pr-agent's config first. pr_agent.log imports config_loader, whose
# module-level load_file() lazily triggers Dynaconf's custom_merge_loader, which in turn
# imports pr_agent.log. Importing config_loader as the first pr_agent import here ensures
# that chain completes before any get_settings() call below, avoiding a partial-init
# circular import when server.py is the first module loaded (e.g. at test collection).
import pr_agent.config_loader  # noqa: F401  (import-order load; do not remove)

from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.mosaico.card import build_agent_card
from pr_agent.mosaico.env_bridge import apply_mosaico_env, langfuse_env_present
from pr_agent.mosaico.executor import PRAgentExecutor, health_check

HEALTH_PATH = "/health"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9000


async def _health(request: Request) -> JSONResponse:
    health = await health_check()
    is_healthy = health == "OK"
    status_code = 200 if is_healthy else 503
    content = {"is_healthy": is_healthy, "status": health}
    if not is_healthy:
        content["detail"] = health
    return JSONResponse(content, status_code=status_code)


def _configure_runtime() -> None:
    """Idempotent import-time configuration. Safe to call from tests (no server start).

    NOTE: setup_logger(JSON) is deliberately NOT called here. Reconfiguring the loguru
    sink as an import-time side effect runs *before* pr_agent's lazy Dynaconf config has
    finished loading, which trips a circular import (pr_agent.log <-> custom_merge_loader)
    when server.py is the first module to trigger the config load (e.g. at test collection).
    The JSON logger is configured in start() instead (real runtime), where the config is
    already loaded. build_app() does not depend on the JSON sink."""
    apply_mosaico_env()
    # Idempotent registry insert (_GIT_PROVIDERS.setdefault("mosaico_diff", ...)).
    import pr_agent.mosaico.provider_registration  # noqa: F401
    _configure_langfuse()


def _configure_langfuse() -> None:
    """Construct the Langfuse client ONCE with a2a transport spans suppressed.
    No-op when creds are absent; degrades on any failure."""
    if not langfuse_env_present():
        return
    try:
        from langfuse import Langfuse
        Langfuse(blocked_instrumentation_scopes=["a2a-python-sdk"])
        get_logger().info("MOSAICO: Langfuse client initialised (a2a-python-sdk spans blocked).")
    except Exception as e:
        get_logger().warning(f"MOSAICO: Langfuse client init failed (continuing untraced): {e}")


def build_app():
    """Build the Starlette app: A2A card + JSONRPC routes + /health, with
    RawContextMiddleware on the same app (A2A 1.0)."""
    card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=PRAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),          # GET /.well-known/agent-card.json
        *create_jsonrpc_routes(handler, rpc_url="/"),
        Route(HEALTH_PATH, _health, methods=["GET"]),
    ]
    return Starlette(routes=routes, middleware=[Middleware(RawContextMiddleware)])


def start() -> None:
    import uvicorn
    setup_logger(fmt=LoggingFormat.JSON)
    host = os.getenv("HOST", DEFAULT_HOST)
    port = int(os.getenv("PORT", DEFAULT_PORT))
    app = build_app()
    get_logger().info(f"Starting PR-Agent MOSAICO solution agent on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


# Import-time configuration (so build_app works in tests). uvicorn only starts in __main__.
_configure_runtime()


if __name__ == "__main__":
    start()
