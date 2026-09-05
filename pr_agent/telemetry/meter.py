import functools

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config, otlp_signal_endpoint
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.shutdown import register_shutdown_handler
from pr_agent.telemetry.types import ExporterType, OtlpProtocol


@functools.lru_cache(maxsize=1)
def get_meter():
    try:
        config = get_otel_config()
        if not config.is_enabled:
            return metrics.NoOpMeter("pr_agent")

        exporter = _create_metric_exporter(config)
        if exporter is None:
            return metrics.NoOpMeter("pr_agent")

        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

        # Local provider, never the process-global one (see tracer.py)
        reader = PeriodicExportingMetricReader(exporter)
        provider = MeterProvider(resource=resource, metric_readers=[reader])

        provider_registry.register(provider)
        register_shutdown_handler()
        return provider.get_meter("pr_agent")

    except Exception as e:
        get_logger().warning(f"Failed to initialize metrics: {e}")
        return metrics.NoOpMeter("pr_agent")


def _create_metric_exporter(config):
    if config.exporter_type == ExporterType.CONSOLE:
        return ConsoleMetricExporter()
    elif config.exporter_type == ExporterType.OTLP:
        # Imported lazily so a missing package cannot break `import pr_agent`
        if config.otlp_protocol == OtlpProtocol.GRPC:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            except ImportError:
                get_logger().warning(
                    "OTEL.OTLP_PROTOCOL is 'grpc' but opentelemetry-exporter-otlp-proto-grpc "
                    "is not installed (pip install pr-agent[otel-grpc]); metrics will not be exported."
                )
                return None
            endpoint = config.otlp_endpoint
        else:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            except ImportError:
                get_logger().warning(
                    "OTEL.EXPORTER_TYPE is 'otlp' but opentelemetry-exporter-otlp-proto-http "
                    "is not installed; metrics will not be exported."
                )
                return None
            endpoint = otlp_signal_endpoint(config.otlp_endpoint, "metrics") if config.otlp_endpoint else None
        kwargs = {"timeout": config.otlp_timeout}
        if endpoint:
            kwargs["endpoint"] = endpoint
        if config.otlp_headers:
            kwargs["headers"] = config.otlp_headers
        return OTLPMetricExporter(**kwargs)
    return None


@functools.lru_cache(maxsize=1)
def get_commands_counter():
    return get_meter().create_counter(
        "pr_agent.commands", unit="{command}", description="PR-Agent commands executed"
    )
