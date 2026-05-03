import functools

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config
from pr_agent.telemetry.shutdown import register_shutdown_handler


@functools.lru_cache(maxsize=1)
def get_tracer():
    """Get or initialize the tracer (lazy, cached, thread-safe via lru_cache)."""
    return _init_telemetry()


def _init_telemetry():
    try:
        if not get_otel_config().is_enabled:
            return trace.get_tracer(__name__)  # no-op

        resource = Resource.create({
            SERVICE_NAME: get_otel_config().service_name,
            SERVICE_VERSION: get_otel_config().service_version,
            DEPLOYMENT_ENVIRONMENT: get_otel_config().environment,
        })

        provider = TracerProvider(resource=resource)
        exporter = _create_exporter(get_otel_config())
        if exporter:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        register_shutdown_handler()
        return trace.get_tracer("pr_agent")

    except Exception as e:
        get_logger().warning(f"Failed to initialize telemetry: {e}")
        return trace.get_tracer(__name__)  # no-op fallback


def _create_exporter(config):
    if config.exporter_type == "console":
        return ConsoleSpanExporter()
    elif config.exporter_type == "otlp":
        kwargs = {}
        if config.otlp_endpoint:
            kwargs['endpoint'] = config.otlp_endpoint
        if config.otlp_headers:
            kwargs['headers'] = config.otlp_headers
        return OTLPSpanExporter(**kwargs)
    return None  # "none" or unknown type — no exporter, spans dropped
