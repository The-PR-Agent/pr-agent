import functools

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config, otlp_signal_endpoint
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.shutdown import register_shutdown_handler
from pr_agent.telemetry.types import ExporterType

# See the note in tracer.py: pr-agent never touches OpenTelemetry's
# process-global providers; created providers go to the lifecycle registry.


@functools.lru_cache(maxsize=1)
def get_meter():
    """Get or initialize the meter (lazy, cached, thread-safe via lru_cache)."""
    return _init_metrics()


def _init_metrics():
    try:
        config = get_otel_config()
        if not config.is_enabled:
            return metrics.NoOpMeter("pr_agent")

        exporter = _create_metric_exporter(config)
        if exporter is None:  # ExporterType.NONE or unknown
            return metrics.NoOpMeter("pr_agent")

        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

        reader = PeriodicExportingMetricReader(exporter)  # default: 60 000 ms
        provider = MeterProvider(resource=resource, metric_readers=[reader])

        provider_registry.register(provider)
        register_shutdown_handler()
        return provider.get_meter("pr_agent")

    except Exception as e:
        get_logger().warning(f"Failed to initialize metrics: {e}")
        return metrics.NoOpMeter("pr_agent")  # no-op fallback


def _create_metric_exporter(config):
    if config.exporter_type == ExporterType.CONSOLE:
        return ConsoleMetricExporter()
    elif config.exporter_type == ExporterType.OTLP:
        # Imported lazily so a missing exporter package degrades telemetry
        # instead of breaking every `import pr_agent`.
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        except ImportError:
            get_logger().warning(
                "OTEL.EXPORTER_TYPE is 'otlp' but opentelemetry-exporter-otlp-proto-http "
                "is not installed; metrics will not be exported."
            )
            return None
        kwargs = {"timeout": config.otlp_timeout}
        if config.otlp_endpoint:
            kwargs["endpoint"] = otlp_signal_endpoint(config.otlp_endpoint, "metrics")
        if config.otlp_headers:
            kwargs["headers"] = config.otlp_headers
        return OTLPMetricExporter(**kwargs)
    return None  # ExporterType.NONE or unknown type — no exporter, metrics dropped


@functools.lru_cache(maxsize=1)
def get_commands_counter():
    """Return the commands counter instrument (created once, cached)."""
    return get_meter().create_counter(
        "pr_agent.commands", unit="{command}", description="PR-Agent commands executed"
    )
