"""Tests for the standardized ExporterType constants and exporter creation.

Exporter type strings are defined once in pr_agent.telemetry.types.ExporterType
and referenced by config.py (validation), tracer.py (span exporter selection),
and meter.py (metric exporter selection). Tracer and meter must agree: an
unknown exporter type creates NO exporter for either (previously the meter
silently fell back to the console exporter).
"""

import sys
from unittest import mock

import pytest
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from pr_agent.telemetry import meter as meter_module
from pr_agent.telemetry import tracer as tracer_module
from pr_agent.telemetry.config import VALID_EXPORTER_TYPES
from pr_agent.telemetry.meter import _create_metric_exporter
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.tracer import _create_exporter
from pr_agent.telemetry.types import ExporterType
from tests.unittest._telemetry_helpers import capture_loguru, clear_telemetry_caches, make_config


@pytest.fixture(autouse=True)
def _reset_telemetry():
    clear_telemetry_caches()
    yield
    clear_telemetry_caches()


def test_valid_exporter_types_equals_constant_set():
    assert VALID_EXPORTER_TYPES == {ExporterType.CONSOLE, ExporterType.OTLP, ExporterType.NONE}


def test_disabled_tracer_is_noop_never_global(monkeypatch):
    """When telemetry is disabled the tracer must be an explicit no-op — not a
    tracer from the process-global provider, which a host application embedding
    pr-agent may have configured with a real exporter."""
    from opentelemetry.trace import NoOpTracer

    monkeypatch.setattr(tracer_module, "get_otel_config", lambda: make_config(is_enabled=False))
    tracer_module.get_tracer.cache_clear()

    assert isinstance(tracer_module.get_tracer(), NoOpTracer)
    assert len(provider_registry) == 0, "disabled telemetry must not create a provider"


def test_disabled_meter_is_noop_never_global(monkeypatch):
    """Same as the tracer: disabled telemetry must never borrow the global meter."""
    from opentelemetry.metrics import NoOpMeter

    monkeypatch.setattr(meter_module, "get_otel_config", lambda: make_config(is_enabled=False))
    meter_module.get_meter.cache_clear()

    assert isinstance(meter_module.get_meter(), NoOpMeter)
    assert len(provider_registry) == 0, "disabled telemetry must not create a provider"


def test_exporter_type_constant_values():
    """The constants are the single source of truth for the literal strings
    users put in configuration.toml."""
    assert ExporterType.OTLP == "otlp"
    assert ExporterType.CONSOLE == "console"
    assert ExporterType.NONE == "none"


def test_span_exporter_console():
    exporter = _create_exporter(make_config(exporter_type=ExporterType.CONSOLE))
    assert isinstance(exporter, ConsoleSpanExporter)


def test_metric_exporter_console():
    exporter = _create_metric_exporter(make_config(exporter_type=ExporterType.CONSOLE))
    assert isinstance(exporter, ConsoleMetricExporter)


def test_span_exporter_none():
    assert _create_exporter(make_config(exporter_type=ExporterType.NONE)) is None


def test_metric_exporter_none():
    assert _create_metric_exporter(make_config(exporter_type=ExporterType.NONE)) is None


def test_span_exporter_unknown_returns_none():
    assert _create_exporter(make_config(exporter_type="bogus")) is None


def test_metric_exporter_unknown_returns_none():
    """Tracer/meter alignment: an unknown type must drop metrics, not silently
    fall back to the console exporter (previous behavior)."""
    assert _create_metric_exporter(make_config(exporter_type="bogus")) is None


@pytest.mark.parametrize("endpoint,headers,expected_kwargs", [
    # OTLP/HTTP needs the per-signal path; OTEL.OTLP_ENDPOINT is documented as the base URL.
    ("http://collector:4318", {"x-team": "k"},
     {"timeout": 3, "endpoint": "http://collector:4318/v1/traces", "headers": {"x-team": "k"}}),
    ("http://collector:4318/", None, {"timeout": 3, "endpoint": "http://collector:4318/v1/traces"}),
    # Already signal-scoped: appended once, never twice.
    ("http://collector:4318/v1/traces", None, {"timeout": 3, "endpoint": "http://collector:4318/v1/traces"}),
    (None, None, {"timeout": 3}),
])
def test_span_exporter_otlp_kwargs(endpoint, headers, expected_kwargs):
    # Patch the class in its home module — the lazy import inside _create_exporter
    # re-resolves it at call time, and the real constructor opens a session.
    with mock.patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as otlp_cls:
        config = make_config(exporter_type=ExporterType.OTLP, otlp_endpoint=endpoint, otlp_headers=headers)
        exporter = _create_exporter(config)

    otlp_cls.assert_called_once_with(**expected_kwargs)
    assert exporter is otlp_cls.return_value


@pytest.mark.parametrize("endpoint,headers,expected_kwargs", [
    ("http://collector:4318", {"x-team": "k"},
     {"timeout": 3, "endpoint": "http://collector:4318/v1/metrics", "headers": {"x-team": "k"}}),
    ("http://collector:4318/", None, {"timeout": 3, "endpoint": "http://collector:4318/v1/metrics"}),
    ("http://collector:4318/v1/metrics", None, {"timeout": 3, "endpoint": "http://collector:4318/v1/metrics"}),
    (None, None, {"timeout": 3}),
])
def test_metric_exporter_otlp_kwargs(endpoint, headers, expected_kwargs):
    with mock.patch("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter") as otlp_cls:
        config = make_config(exporter_type=ExporterType.OTLP, otlp_endpoint=endpoint, otlp_headers=headers)
        exporter = _create_metric_exporter(config)

    otlp_cls.assert_called_once_with(**expected_kwargs)
    assert exporter is otlp_cls.return_value


@pytest.mark.parametrize("create,module,cls", [
    (_create_exporter, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", "OTLPSpanExporter"),
    (_create_metric_exporter, "opentelemetry.exporter.otlp.proto.grpc.metric_exporter", "OTLPMetricExporter"),
])
def test_otlp_grpc_protocol_uses_grpc_exporter_with_verbatim_endpoint(monkeypatch, create, module, cls):
    """gRPC takes the base endpoint as-is — no /v1/<signal> path is appended.

    The gRPC exporter is an optional extra and may genuinely be absent from the
    test environment, so a fake module is injected rather than patching the
    real one."""
    otlp_cls = mock.Mock()
    monkeypatch.setitem(sys.modules, module, mock.Mock(**{cls: otlp_cls}))

    exporter = create(make_config(
        exporter_type=ExporterType.OTLP, otlp_endpoint="http://collector:4317", otlp_protocol="grpc"))

    otlp_cls.assert_called_once_with(timeout=3, endpoint="http://collector:4317")
    assert exporter is otlp_cls.return_value


@pytest.mark.parametrize("create,module,package,protocol", [
    # The default http exporter and the optional grpc one must both degrade the
    # same way: a warning naming the missing package, never a broken import.
    (_create_exporter, "opentelemetry.exporter.otlp.proto.http.trace_exporter",
     "opentelemetry-exporter-otlp-proto-http", "http"),
    (_create_metric_exporter, "opentelemetry.exporter.otlp.proto.http.metric_exporter",
     "opentelemetry-exporter-otlp-proto-http", "http"),
    (_create_exporter, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
     "pr-agent[otel-grpc]", "grpc"),
    (_create_metric_exporter, "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
     "pr-agent[otel-grpc]", "grpc"),
])
def test_otlp_missing_exporter_package_degrades_with_warning(monkeypatch, create, module, package, protocol):
    """An environment without the configured exporter package must lose OTLP
    export (with a warning naming what to install), never the ability to import
    or run pr-agent. A None sys.modules entry makes the lazy import raise
    ImportError."""
    monkeypatch.setitem(sys.modules, module, None)

    with capture_loguru(level="WARNING") as captured:
        exporter = create(make_config(
            exporter_type=ExporterType.OTLP, otlp_endpoint="http://collector:4318",
            otlp_protocol=protocol))

    assert exporter is None
    assert package in "\n".join(captured)


def test_get_tracer_lru_cache_identity(monkeypatch):
    """The tracer object is cached; repeated calls must not build new providers."""
    monkeypatch.setattr(tracer_module, "get_otel_config", lambda: make_config(is_enabled=False))
    tracer_module.get_tracer.cache_clear()

    assert tracer_module.get_tracer() is tracer_module.get_tracer()
