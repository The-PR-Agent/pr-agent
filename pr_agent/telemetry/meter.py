import functools

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config


@functools.lru_cache(maxsize=1)
def get_meter():
    """Get or initialize the meter (lazy, cached, thread-safe via lru_cache)."""
    return _init_metrics()


def _init_metrics():
    try:
        if not get_otel_config().is_enabled:
            return metrics.get_meter(__name__)  # no-op

        exporter = _create_metric_exporter(get_otel_config())
        if exporter is None:  # exporter_type == "none"
            return metrics.get_meter(__name__)  # no-op

        resource = Resource.create({
            SERVICE_NAME: get_otel_config().service_name,
            SERVICE_VERSION: get_otel_config().service_version,
            DEPLOYMENT_ENVIRONMENT: get_otel_config().environment,
        })

        reader = PeriodicExportingMetricReader(exporter)  # default: 60 000 ms
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)

        import atexit
        atexit.register(lambda: metrics.get_meter_provider().shutdown())

        return metrics.get_meter("pr_agent")

    except Exception as e:
        get_logger().warning(f"Failed to initialize metrics: {e}")
        return metrics.get_meter(__name__)  # no-op fallback


def _create_metric_exporter(config):
    if config.exporter_type == "otlp":
        kwargs = {}
        if config.otlp_endpoint:
            kwargs["endpoint"] = config.otlp_endpoint
        if config.otlp_headers:
            kwargs["headers"] = config.otlp_headers
        return OTLPMetricExporter(**kwargs)
    elif config.exporter_type == "none":
        return None
    return ConsoleMetricExporter()


@functools.lru_cache(maxsize=1)
def get_commands_counter():
    """Return the commands counter instrument (created once, cached)."""
    return get_meter().create_counter(
        "pr_agent.commands.total", unit="1", description="Total PR-Agent commands executed"
    )


@functools.lru_cache(maxsize=1)
def get_tokens_histogram():
    """Return the token usage histogram instrument (created once, cached)."""
    return get_meter().create_histogram(
        "pr_agent.llm.tokens", unit="token", description="LLM token usage per completion"
    )
