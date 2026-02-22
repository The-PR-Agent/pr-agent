import threading

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource

from pr_agent.log import get_logger
from pr_agent.telemetry.config import get_otel_config

_meter = None
_provider = None
_lock = threading.Lock()


def get_meter():
    """Get or initialize the meter (lazy initialization, thread-safe)."""
    global _meter
    if _meter is None:
        with _lock:
            if _meter is None:  # Double-check locking
                _init_metrics()
    return _meter


def _init_metrics():
    """Initialize MeterProvider based on shared OTel config. Mirrors tracer._init_telemetry()."""
    global _meter, _provider

    try:
        config = get_otel_config()

        if not config.is_enabled:
            _meter = metrics.get_meter(__name__)  # no-op meter
            return

        resource = Resource.create({
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
        })

        exporter = _create_metric_exporter(config)
        reader = PeriodicExportingMetricReader(exporter)  # default export interval: 60 000 ms
        _provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(_provider)
        _meter = metrics.get_meter("pr_agent")

        import atexit
        atexit.register(lambda: _provider.shutdown())

    except Exception as e:
        get_logger().warning(f"Failed to initialize metrics: {e}")
        _meter = metrics.get_meter(__name__)  # no-op fallback


def _create_metric_exporter(config):
    """Create metric exporter matching tracer._create_exporter() logic."""
    if config.exporter_type == "otlp":
        kwargs = {}
        if config.otlp_endpoint:
            kwargs["endpoint"] = config.otlp_endpoint
        if config.otlp_headers:
            kwargs["headers"] = config.otlp_headers
        return OTLPMetricExporter(**kwargs)
    return ConsoleMetricExporter()


class _LazyMeterProxy:
    """Proxy that initializes meter on first use. Mirrors _LazyTracerProxy."""

    def __getattr__(self, name):
        return getattr(get_meter(), name)


meter = _LazyMeterProxy()
