import functools

from opentelemetry import trace
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config, otlp_signal_endpoint
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.shutdown import register_shutdown_handler
from pr_agent.telemetry.types import ExporterType, OtlpProtocol


@functools.lru_cache(maxsize=1)
def get_tracer():
    try:
        config = get_otel_config()
        if not config.is_enabled:
            return trace.NoOpTracer()

        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

        # Local provider, never the process-global one: a host application
        # embedding pr-agent keeps control of its own telemetry.
        provider = TracerProvider(resource=resource)
        exporter = _create_exporter(config)
        if exporter:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        provider_registry.register(provider)
        register_shutdown_handler()
        return provider.get_tracer("pr_agent")

    except Exception as e:
        get_logger().warning(f"Failed to initialize telemetry: {e}")
        return trace.NoOpTracer()


def _create_exporter(config):
    if config.exporter_type == ExporterType.CONSOLE:
        return ConsoleSpanExporter()
    elif config.exporter_type == ExporterType.OTLP:
        # Imported lazily so a missing package cannot break `import pr_agent`
        if config.otlp_protocol == OtlpProtocol.GRPC:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            except ImportError:
                get_logger().warning(
                    "OTEL.OTLP_PROTOCOL is 'grpc' but opentelemetry-exporter-otlp-proto-grpc "
                    "is not installed (pip install pr-agent[otel-grpc]); telemetry will not be exported."
                )
                return None
            endpoint = config.otlp_endpoint
        else:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            except ImportError:
                get_logger().warning(
                    "OTEL.EXPORTER_TYPE is 'otlp' but opentelemetry-exporter-otlp-proto-http "
                    "is not installed; telemetry will not be exported."
                )
                return None
            endpoint = otlp_signal_endpoint(config.otlp_endpoint, 'traces') if config.otlp_endpoint else None
        kwargs = {'timeout': config.otlp_timeout}
        if endpoint:
            kwargs['endpoint'] = endpoint
        if config.otlp_headers:
            kwargs['headers'] = config.otlp_headers
        return OTLPSpanExporter(**kwargs)
    return None
