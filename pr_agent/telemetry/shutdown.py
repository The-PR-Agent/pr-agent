import atexit

from opentelemetry import trace

from pr_agent.log import get_logger

_shutdown_registered = False

def register_shutdown_handler():
    """Register atexit handler to flush spans"""
    global _shutdown_registered
    if not _shutdown_registered:
        atexit.register(shutdown_telemetry)
        _shutdown_registered = True

def shutdown_telemetry():
    """Flush and shutdown telemetry provider"""
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, 'shutdown'):
            get_logger().debug("Shutting down telemetry provider")
            provider.shutdown()
    except Exception as e:
        get_logger().warning(f"Error shutting down telemetry: {e}")
