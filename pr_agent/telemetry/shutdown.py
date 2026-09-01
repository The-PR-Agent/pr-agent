import atexit
import functools

from pr_agent.telemetry.config import get_otel_config
from pr_agent.telemetry.registry import provider_registry


@functools.lru_cache(maxsize=1)
def register_shutdown_handler():
    atexit.register(shutdown_telemetry)


def shutdown_telemetry():
    provider_registry.shutdown_all()


def flush_telemetry(timeout_millis: int | None = None):
    """Force-export buffered telemetry at the request boundary.

    Serverless platforms freeze the moment the handler returns and are reaped
    without running atexit, so buffered spans/metrics would otherwise be lost.
    """
    if timeout_millis is None:
        try:
            timeout_millis = get_otel_config().otlp_timeout * 1000
        except Exception:
            # Runs in handle_request's finally — a config error must not
            # mask the real request outcome.
            timeout_millis = 3000
    provider_registry.flush_all(timeout_millis)
