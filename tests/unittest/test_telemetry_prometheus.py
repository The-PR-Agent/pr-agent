"""Tests for the native Prometheus exporter and its `/metrics` endpoint.

The whole module runs with ``PROMETHEUS_MULTIPROC_DIR`` set (multiprocess
mode), mirroring a gunicorn worker: prometheus_client decides its storage mode
at import time from that env var, and the exporter defers the import to first
use. Nothing else in the telemetry suite imports prometheus_client, so the
env var above drives every process-global decision here.
"""

import json
import os
import subprocess
import sys
from textwrap import dedent

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


def test_counter_repeated_flushes_maintain_value():
    """Verify that repeated metric flushes after a single increment do not accumulate cumulative totals."""
    provider, reader = _counter_provider()
    counter = provider.get_meter("prometheus-flush-test").create_counter(
        "pr_agent.flushed_commands", unit="{command}", description="PR-Agent commands with flush"
    )
    counter.add(1, {"pr_agent.command": "describe", "provider.name": "gitlab"})

    # Collect / flush multiple times
    for _ in range(4):
        reader.collect()

    body = prometheus_response().body.decode()
    assert 'pr_agent_flushed_commands_total{pr_agent_command="describe",provider_name="gitlab"} 1.0' in body


def test_multiple_workers_aggregate_at_scrape_time():
    """Run the exporter in two fresh interpreter processes (as gunicorn workers
    would), each recording one command, and assert /metrics merges them."""
    child_code = "; ".join([
        "from opentelemetry.sdk.metrics import MeterProvider",
        "from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader",
        "from pr_agent.telemetry.prometheus import PrometheusMetricExporter",
        "provider = MeterProvider(metric_readers=[PeriodicExportingMetricReader(PrometheusMetricExporter())])",
        (
            "counter = provider.get_meter('mp-test').create_counter("
            "'pr_agent.commands', unit='{command}', description='PR-Agent commands executed')"
        ),
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


@pytest.mark.parametrize("order", ["command-first", "scrape-first"])
def test_direct_azure_start_ignores_retained_multiprocess_state(tmp_path, order):
    state_dir = tmp_path / "retained-state"
    state_dir.mkdir(mode=0o700)
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "PYTHONPATH", "LANG", "LC_ALL", "TIKTOKEN_CACHE_DIR",
    }}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    seed = dedent("""
        import sys
        from pr_agent.telemetry.prometheus_multiproc import ensure_prometheus_multiproc_dir
        ensure_prometheus_multiproc_dir(sys.argv[1])
        from prometheus_client import Counter
        counter = Counter("pr_agent_commands", "PR-Agent commands executed",
                          ["pr_agent_command", "provider_name"])
        counter.labels("review", "azure").inc(5)
    """)
    subprocess.run([sys.executable, "-c", seed, str(state_dir)], env=env,
                   check=True, capture_output=True, text=True, timeout=45)
    retained_files = list(state_dir.glob("*.db"))
    assert retained_files

    code = dedent("""
        import json
        import os
        import sys
        from pr_agent.config_loader import get_settings
        settings = get_settings()
        settings.set("OTEL.IS_ENABLED", True)
        settings.set("OTEL.EXPORTER_TYPE", "prometheus")
        settings.set("OTEL.SERVICE_NAME", "direct-test")
        settings.set("OTEL.ENVIRONMENT", "test")
        settings.set("OTEL.PROMETHEUS_MULTIPROC_DIR", sys.argv[1])
        assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ
        assert "prometheus_multiproc_dir" not in os.environ
        from pr_agent.servers import azuredevops_server_webhook as azure
        assert "prometheus_client" not in sys.modules
        listeners = []
        azure.uvicorn.run = lambda app, **kwargs: listeners.append(app)
        azure.start()
        assert len(listeners) == 1
        from starlette.routing import Match
        scope = {"type": "http", "path": "/metrics", "root_path": "", "method": "GET"}
        assert any(route.matches(scope)[0] == Match.FULL for route in listeners[0].routes)
        assert "prometheus_client" not in sys.modules
        from pr_agent.telemetry.prometheus import prometheus_response
        if sys.argv[2] == "scrape-first":
            prometheus_response()
        from pr_agent.telemetry.meter import get_commands_counter
        from pr_agent.telemetry.registry import provider_registry
        counter = get_commands_counter()
        for _ in range(2):
            counter.add(1, {"pr_agent.command": "review", "provider.name": "azure"})
        assert len(provider_registry) == 1
        provider_registry.flush_all()
        body = prometheus_response().body.decode()
        from prometheus_client.values import ValueClass
        print(json.dumps({"multiprocess": ValueClass._multiprocess, "body": body}))
        provider_registry.shutdown_all()
    """)
    result = subprocess.run([sys.executable, "-c", code, str(state_dir), order], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    observation = json.loads(result.stdout)
    series = 'pr_agent_commands_total{pr_agent_command="review",provider_name="azure"}'
    assert series + " 2.0" in observation["body"], observation
    assert observation["multiprocess"] is False
    assert series + " 5.0" not in observation["body"]
    assert all(path.exists() for path in retained_files)
