"""Native Prometheus scrape endpoint for the OpenTelemetry command telemetry.

When ``OTEL.EXPORTER_TYPE = "prometheus"`` the ``pr_agent.commands`` counter
stays an OpenTelemetry instrument; this module translates the SDK's aggregated
data points into ``prometheus_client`` metric objects instead of shipping them
to a collector.

Gunicorn runs with ``preload_app = True`` (fork model), so the translation
happens on the worker side: each worker's values land in its own
multiprocess state file under ``PROMETHEUS_MULTIPROC_DIR``, and ``/metrics``
serves a ``MultiProcessCollector`` that merges every worker's file.

prometheus_client decides at import time whether metrics are multiprocess-aware
(from ``PROMETHEUS_MULTIPROC_DIR``), and gunicorn's master imports the app
before ``when_ready`` provisions that directory. This module therefore never
imports prometheus_client at module level: everything touching it is deferred
to the worker or to a scrape request, so a master process merely importing the
app can neither create metrics in the wrong mode nor see a stale directory.

``opentelemetry-exporter-prometheus`` is deliberately not a dependency: its
``PrometheusMetricReader`` documents "No multiprocessing support", which is the
requirement this module exists for, and it ships as a 0.x pre-release.
"""

import re
from typing import Any, Dict, Tuple

from opentelemetry.sdk.metrics.export import (
    Gauge as OTelGauge,
)
from opentelemetry.sdk.metrics.export import (
    Histogram as OTelHistogram,
)
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    Sum,
)
from starlette.responses import Response

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.telemetry.types import ExporterType

_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_:]")

_prometheus_client = None
_prometheus_registry = None


def _client() -> Any:
    """Import prometheus_client on first use (never during app import in the master)."""
    global _prometheus_client
    if _prometheus_client is None:
        import prometheus_client

        # The multiprocess dir/env is provisioned by config/`when_ready` before
        # any worker reaches this point, so the client picks the right mode here.
        _prometheus_client = prometheus_client
    return _prometheus_client


def prometheus_metrics_enabled() -> bool:
    """True when the [otel] prometheus exporter is selected and telemetry is on."""
    settings = get_settings()
    return bool(settings.get("OTEL.IS_ENABLED", False)) and (
        settings.get("OTEL.EXPORTER_TYPE") == ExporterType.PROMETHEUS
    )


def prometheus_registry():
    """The registry metric objects are created on (single-process view)."""
    return _registry()


def _registry():
    global _prometheus_registry
    if _prometheus_registry is None:
        _prometheus_registry = _client().CollectorRegistry(auto_describe=False)
    return _prometheus_registry


def prometheus_response() -> Response:
    """Render ``/metrics`` for this process.

    Uses a ``MultiProcessCollector`` when prometheus_client imported in
    multiprocess mode (workers under gunicorn with PROMETHEUS_MULTIPROC_DIR
    set); otherwise serves this process's own registry (single-process
    deployments such as plain `uvicorn`).
    """
    client = _client()
    # prometheus_client decides its storage mode when `values` is first imported
    # (from PROMETHEUS_MULTIPROC_DIR); MultiProcessValue is chosen per worker.
    import prometheus_client.multiprocess
    import prometheus_client.values as prometheus_values

    if prometheus_values.ValueClass._multiprocess:
        registry = client.CollectorRegistry()
        prometheus_client.multiprocess.MultiProcessCollector(registry)
    else:
        registry = _registry()
    return Response(
        content=client.generate_latest(registry), media_type=client.CONTENT_TYPE_LATEST
    )


def attach_metrics_endpoint(router) -> None:
    """Attach a lazy ``GET /metrics`` route to a FastAPI router.

    The route raises no prometheus_client import at registration time — the
    gunicorn master (which imports the app under ``preload_app``) must never
    import it before ``when_ready`` provisions the multiprocess directory. The
    heavy import happens on the first request, inside the serving worker.
    """

    def _metrics_endpoint():
        return prometheus_response()

    router.add_api_route(
        "/metrics", _metrics_endpoint, methods=["GET"], include_in_schema=False
    )


class PrometheusMetricExporter(MetricExporter):
    """Translate SDK `MetricsData` into prometheus_client metrics.

    Designed for use with ``PeriodicExportingMetricReader`` (DELTA temporality
    for counters, accumulating per worker). Increments land in the worker's
    multiprocess state file when prometheus_client imported in multiprocess mode.
    """

    def __init__(self):
        super().__init__()
        # metric name -> (label_names, prometheus_client metric object)
        self._counters: Dict[str, Tuple] = {}
        self._gauges: Dict[str, Tuple] = {}

    def export(self, metrics_data, timeout_millis=10_000, **kwargs) -> MetricExportResult:
        try:
            for resource_metric in metrics_data.resource_metrics:
                for scope_metric in resource_metric.scope_metrics:
                    for metric in scope_metric.metrics:
                        self._translate(metric)
        except Exception as e:
            get_logger().warning(f"Failed to export metrics to Prometheus: {e}")
            return MetricExportResult.FAILURE
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000) -> bool:
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs) -> None:
        pass

    def _translate(self, metric) -> None:
        name = _sanitize_metric_name(metric.name)
        help_text = metric.description or metric.name
        if isinstance(metric.data, Sum):
            if metric.data.is_monotonic:
                for point in metric.data.data_points:
                    obj, labels = self._metric(self._counters, "Counter", name, help_text, point)
                    obj.labels(**labels).inc(point.value)
            else:
                for point in metric.data.data_points:
                    obj, labels = self._metric(self._gauges, "Gauge", name, help_text, point)
                    obj.labels(**labels).set(point.value)
        elif isinstance(metric.data, OTelGauge):
            for point in metric.data.data_points:
                obj, labels = self._metric(self._gauges, "Gauge", name, help_text, point)
                obj.labels(**labels).set(point.value)
        elif isinstance(metric.data, OTelHistogram):
            get_logger().warning(
                f"Skipping histogram metric '{metric.name}': Prometheus histograms are "
                f"not exported yet (no command telemetry uses one today)."
            )
        else:
            get_logger().warning(
                f"Skipping unsupported metric '{metric.name}' of type {type(metric.data)}."
            )

    def _metric(self, cache, metric_class_name, name, help_text, point):
        labels = _point_labels(point)
        entry = cache.get(name)
        if entry is None:
            client = _client()
            metric_class = getattr(client, metric_class_name)
            label_names = tuple(sorted(labels))
            metric_object = metric_class(
                name, help_text, labelnames=label_names, registry=_registry()
            )
            entry = cache[name] = (label_names, metric_object)
        label_names, metric_object = entry
        # A family's label set is fixed on the first data point; drop later
        # attributes we cannot attach and back-fill missing ones with "".
        normalized = {label: labels.get(label, "") for label in label_names}
        return metric_object, normalized


def _sanitize_metric_name(name: str) -> str:
    return _NAME_INVALID_CHARS.sub("_", name)


def _sanitize_label_key(key: str) -> str:
    key = _NAME_INVALID_CHARS.sub("_", key)
    return "_" + key if key[0].isdigit() else key


def _point_labels(point) -> dict:
    return {_sanitize_label_key(key): str(value) for key, value in point.attributes.items()}
