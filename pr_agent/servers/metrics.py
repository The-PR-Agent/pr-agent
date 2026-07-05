"""Optional Prometheus metrics for the webhook servers.

Everything here is a no-op unless BOTH hold:
  - config.monitoring.enable_metrics is true, and
  - prometheus-client is installed (pip install pr-agent[monitoring]).

setup_metrics(app) installs an ASGI middleware that counts HTTP requests
(by method/path/status) and observes request latency, plus a /metrics
endpoint exposing the standard Prometheus text format. Metric objects are
created lazily and only once per process (webhook servers build a single
FastAPI app at import time).
"""
import time

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

_metrics = None  # (request_counter, latency_histogram), created once


def _get_metrics():
    global _metrics
    if _metrics is None:
        request_counter = Counter(
            "pr_agent_http_requests_total",
            "HTTP requests processed by the webhook server",
            ["method", "path", "status"],
        )
        latency_histogram = Histogram(
            "pr_agent_http_request_duration_seconds",
            "HTTP request latency of the webhook server",
            ["method", "path"],
        )
        _metrics = (request_counter, latency_histogram)
    return _metrics


def metrics_enabled() -> bool:
    if not get_settings().get("monitoring.enable_metrics", False):
        return False
    if not PROMETHEUS_AVAILABLE:
        get_logger().warning(
            "monitoring.enable_metrics is set but prometheus-client is not installed; "
            "install with: pip install pr-agent[monitoring]")
        return False
    return True


class PrometheusMiddleware:
    """Pure ASGI middleware: no dependency on the response body, minimal overhead."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_counter, latency_histogram = _get_metrics()
        method = scope.get("method", "")
        path = scope.get("path", "")
        start = time.monotonic()
        status_holder = {"status": "500"}  # crash before response start counts as 500

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = str(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_counter.labels(method=method, path=path,
                                   status=status_holder["status"]).inc()
            latency_histogram.labels(method=method, path=path).observe(
                time.monotonic() - start)


def setup_metrics(app) -> None:
    """Attach the metrics middleware and /metrics endpoint to a FastAPI app.

    Call after the app is constructed (and before serving). No-op when metrics
    are disabled or prometheus-client is missing.
    """
    if not metrics_enabled():
        return

    from starlette.responses import Response

    endpoint = get_settings().get("monitoring.metrics_endpoint", "/metrics")

    async def metrics_route():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.add_api_route(endpoint, metrics_route, methods=["GET"], include_in_schema=False)
    app.add_middleware(PrometheusMiddleware)
    get_logger().info(f"Prometheus metrics enabled at {endpoint}")
