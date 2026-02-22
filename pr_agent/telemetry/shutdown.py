import atexit
import functools

from opentelemetry import trace

from pr_agent.log import get_logger


@functools.lru_cache(maxsize=1)
def register_shutdown_handler():
    """Register atexit handler to flush spans (called once via lru_cache)."""
    atexit.register(shutdown_telemetry)


def shutdown_telemetry():
    """Flush and shutdown telemetry provider"""
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, 'shutdown'):
            get_logger().debug("Shutting down telemetry provider")
            provider.shutdown()
    except Exception as e:
        get_logger().warning(f"Error shutting down telemetry: {e}")
