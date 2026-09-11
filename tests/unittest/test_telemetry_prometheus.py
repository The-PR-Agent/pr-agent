"""Tests for the native Prometheus exporter and its `/metrics` endpoint.

The whole module runs with ``PROMETHEUS_MULTIPROC_DIR`` set (multiprocess
mode), mirroring a gunicorn worker: prometheus_client decides its storage mode
at import time from that env var, and the exporter defers the import to first
use. Nothing else in the telemetry suite imports prometheus_client, so the
env var above drives every process-global decision here.
"""

import os
import subprocess
import sys

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from pr_agent.telemetry.prometheus import (
    PrometheusMetricExporter,
    attach_metrics_endpoint,
    prometheus_metrics_enabled,
    prometheus_response,
)
from pr_agent.telemetry.prometheus_multiproc import ensure_prometheus_multiproc_dir


@pytest.fixture(scope="session", autouse=True)
def _multiproc_env(tmp_path_factory):
    """Set the multiprocess state dir before the first prometheus_client import."""
    state_dir = tmp_path_factory.mktemp("prometheus-multiproc")
    ensure_prometheus_multiproc_dir(str(state_dir))
    yield str(state_dir)


def _counter_provider():
    reader = PeriodicExportingMetricReader(PrometheusMetricExporter())
    provider = MeterProvider(metric_readers=[reader])
    return provider, reader


def test_counter_export_renders_prometheus_text():
    provider, reader = _counter_provider()
    counter = provider.get_meter("prometheus-test").create_counter(
        "pr_agent.commands", unit="{command}", description="PR-Agent commands executed"
    )
    counter.add(3, {"pr_agent.command": "review", "provider.name": "github"})
    reader.collect()

    body = prometheus_response().body.decode()
    # The OTel name "pr_agent.commands" is sanitized (dots -> underscores) and
    # Prometheus appends the counter's _total suffix.
    assert "# TYPE pr_agent_commands_total counter" in body
    assert "# HELP pr_agent_commands_total PR-Agent commands executed" in body
    assert 'pr_agent_commands_total{pr_agent_command="review",provider_name="github"} 3.0' in body


def test_multiple_workers_aggregate_at_scrape_time():
    """Run the exporter in two fresh interpreter processes (as gunicorn workers
    would), each recording one command, and assert /metrics merges them."""
    child_code = "; ".join([
        "from opentelemetry.sdk.metrics import MeterProvider",
        "from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader",
        "from pr_agent.telemetry.prometheus import PrometheusMetricExporter",
        "provider = MeterProvider(metric_readers=[PeriodicExportingMetricReader(PrometheusMetricExporter())])",
        "counter = provider.get_meter('mp-test').create_counter('pr_agent.commands', unit='{command}', description='PR-Agent commands executed')",
        "counter.add(1, {'pr_agent.command': 'review'})",
        "provider.shutdown()",
    ])
    for _ in range(2):
        subprocess.run([sys.executable, "-c", child_code], check=True, capture_output=True)

    body = prometheus_response().body.decode()
    # Both workers' files are merged into one series at scrape time; the
    # parent's own in-process contribution (different label set) also shows.
    assert 'pr_agent_commands_total{pr_agent_command="review"} 2.0' in body
    assert 'pr_agent_command="review",provider_name="github"}' in body


def test_attach_metrics_endpoint_registers_lazy_get_route():
    from fastapi import APIRouter

    router = APIRouter()
    attach_metrics_endpoint(router)

    assert len(router.routes) == 1
    route = router.routes[0]
    assert route.path == "/metrics"
    assert "GET" in route.methods
    assert route.include_in_schema is False
    # The endpoint resolves prometheus_client only on request — registering the
    # route must not pull it in (a gunicorn master importing the app would
    # otherwise lock in the wrong multiprocess mode before `when_ready`).
    assert callable(route.endpoint)


def test_prometheus_metrics_enabled_reads_settings(monkeypatch):
    from pr_agent.telemetry import prometheus as prometheus_module

    class FakeSettings:
        def __init__(self, values):
            self._values = values

        def get(self, key, default=None):
            return self._values.get(key, default)

    monkeypatch.setattr(prometheus_module, "get_settings", lambda: FakeSettings({}))
    assert prometheus_metrics_enabled() is False

    monkeypatch.setattr(prometheus_module, "get_settings", lambda: FakeSettings(
        {"OTEL.IS_ENABLED": True, "OTEL.EXPORTER_TYPE": "prometheus"}))
    assert prometheus_metrics_enabled() is True

    monkeypatch.setattr(prometheus_module, "get_settings", lambda: FakeSettings(
        {"OTEL.IS_ENABLED": True, "OTEL.EXPORTER_TYPE": "otlp"}))
    assert prometheus_metrics_enabled() is False


def test_importing_prometheus_bridge_does_not_import_prometheus_client():
    """The module must never import prometheus_client at import time — a gunicorn
    master that imports the app under preload_app would otherwise decide the
    non-multiprocess storage mode before when_ready provisions the state dir."""
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    code = (
        "import sys; "
        "from pr_agent.telemetry.prometheus import prometheus_metrics_enabled, "
        "attach_metrics_endpoint; "
        "assert 'prometheus_client' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, env=env)
