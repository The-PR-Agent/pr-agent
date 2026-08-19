import atexit
import functools

from pr_agent.telemetry.registry import provider_registry


@functools.lru_cache(maxsize=1)
def register_shutdown_handler():
    """Register atexit handler to flush telemetry (called once via lru_cache)."""
    atexit.register(shutdown_telemetry)


def shutdown_telemetry():
    """Flush and shut down the providers pr-agent created."""
    provider_registry.shutdown_all()


def flush_telemetry(timeout_millis: int = 3000):
    """Force-export buffered spans and metrics without shutting anything down.

    Called at the end of every handled request. Serverless platforms freeze
    the execution environment the moment the handler returns — background
    export threads stop ticking, and the environment is later reaped without
    running atexit — so telemetry buffered by the batch span processor or the
    periodic metric reader would otherwise never leave the machine.
    """
    provider_registry.flush_all(timeout_millis)
