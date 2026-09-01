import functools

from opentelemetry import trace
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config, otlp_signal_endpoint
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.shutdown import register_shutdown_handler
from pr_agent.telemetry.types import ExporterType

# pr-agent never registers itself as OpenTelemetry's process-global provider:
# the provider is created locally and handed to the lifecycle registry, so a
# host application embedding pr-agent keeps full control of its own telemetry,
# and pr-agent's flush/shutdown touch only what pr-agent created. Fleet
# deployments still aggregate normally — each process exports to the
# configured OTLP endpoint and the collector does the fan-in.


@functools.lru_cache(maxsize=1)
def get_tracer():
    """Get or initialize the tracer (lazy, cached, thread-safe via lru_cache)."""
    return _init_telemetry()


def _init_telemetry():
    try:
        config = get_otel_config()
        if not config.is_enabled:
            return trace.NoOpTracer()

        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

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
        # Imported lazily so a missing exporter package degrades telemetry
        # instead of breaking every `import pr_agent`.
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            get_logger().warning(
                "OTEL.EXPORTER_TYPE is 'otlp' but opentelemetry-exporter-otlp-proto-http "
                "is not installed; telemetry will not be exported."
            )
            return None
        kwargs = {'timeout': config.otlp_timeout}
        if config.otlp_endpoint:
            kwargs['endpoint'] = otlp_signal_endpoint(config.otlp_endpoint, 'traces')
        if config.otlp_headers:
            kwargs['headers'] = config.otlp_headers
        return OTLPSpanExporter(**kwargs)
    return None  # ExporterType.NONE or unknown type — no exporter, spans dropped
