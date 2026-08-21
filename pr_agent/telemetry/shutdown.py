import atexit
import functools

from pr_agent.telemetry.config import get_otel_config
from pr_agent.telemetry.registry import provider_registry


@functools.lru_cache(maxsize=1)
def register_shutdown_handler():
    """Register atexit handler to flush telemetry (called once via lru_cache)."""
    atexit.register(shutdown_telemetry)


def shutdown_telemetry():
    """Flush and shut down the providers pr-agent created."""
    provider_registry.shutdown_all()


def flush_telemetry(timeout_millis: int | None = None):
    """Force-export buffered spans and metrics without shutting anything down.

    Called at the end of every handled request. Serverless platforms freeze
    the execution environment the moment the handler returns — background
    export threads stop ticking, and the environment is later reaped without
    running atexit — so telemetry buffered by the batch span processor or the
    periodic metric reader would otherwise never leave the machine.

    The default deadline follows OTEL.OTLP_TIMEOUT (seconds) so the flush
    waits exactly as long as the export RPC itself is allowed to run.
    """
    if timeout_millis is None:
        try:
            timeout_millis = get_otel_config().otlp_timeout * 1000
        except Exception:
            # Runs in handle_request's finally — a config error (e.g. invalid
            # OTEL.EXPORTER_TYPE) must not mask the real request outcome.
            timeout_millis = 3000
    provider_registry.flush_all(timeout_millis)
