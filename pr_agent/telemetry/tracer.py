import threading

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config
from pr_agent.telemetry.shutdown import register_shutdown_handler

_tracer = None
_provider = None
_initialized = False
_lock = threading.Lock()

def get_tracer():
    """Get or initialize the tracer (lazy initialization)"""
    global _tracer
    if _tracer is None:
        with _lock:
            if _tracer is None:  # Double-check locking
                _init_telemetry()
    return _tracer

def _init_telemetry():
    """Initialize telemetry based on configuration"""
    global _tracer, _provider, _initialized

    try:
        config = get_otel_config()

        if not (config.is_enabled or config.exporter_type == "none"):
            # Return no-op tracer
            _tracer = trace.get_tracer(__name__)
            _initialized = True
            return

        # Create resource with service identification
        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

        # Create provider
        _provider = TracerProvider(resource=resource)

        # Add appropriate exporter
        exporter = _create_exporter(config)
        if exporter:
            processor = BatchSpanProcessor(exporter)
            _provider.add_span_processor(processor)

        # Set global provider
        trace.set_tracer_provider(_provider)

        # Get tracer
        _tracer = trace.get_tracer("pr_agent")

        # Register shutdown
        register_shutdown_handler()

        _initialized = True
    except Exception as e:
        # Fail-safe: return no-op tracer on initialization error
        get_logger().warning(f"Failed to initialize telemetry: {e}")
        _tracer = trace.get_tracer(__name__)
        _initialized = True

def _create_exporter(config):
    """Create appropriate span exporter"""
    if config.exporter_type == "console":
        return ConsoleSpanExporter()
    elif config.exporter_type == "otlp":
        kwargs = {}
        if config.otlp_endpoint:
            kwargs['endpoint'] = config.otlp_endpoint
        if config.otlp_headers:
            kwargs['headers'] = config.otlp_headers
        return OTLPSpanExporter(**kwargs)
    return None

# Backward-compatible proxy for existing decorator usage
class _LazyTracerProxy:
    """Proxy that initializes tracer on first use"""
    def start_as_current_span(self, *args, **kwargs):
        return get_tracer().start_as_current_span(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(get_tracer(), name)

tracer = _LazyTracerProxy()