"""Tests for the standardized ExporterType constants and exporter creation.

Exporter type strings are defined once in pr_agent.telemetry.types.ExporterType
and referenced by config.py (validation), tracer.py (span exporter selection),
and meter.py (metric exporter selection). Tracer and meter must agree: an
unknown exporter type creates NO exporter for either (previously the meter
silently fell back to the console exporter).
"""

from unittest import mock

import pytest
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from pr_agent.telemetry import meter as meter_module
from pr_agent.telemetry import tracer as tracer_module
from pr_agent.telemetry.config import VALID_EXPORTER_TYPES
from pr_agent.telemetry.meter import _create_metric_exporter
from pr_agent.telemetry.tracer import _create_exporter
from pr_agent.telemetry.types import ExporterType
from tests.unittest._telemetry_helpers import clear_telemetry_caches, make_config


@pytest.fixture(autouse=True)
def _reset_telemetry():
    clear_telemetry_caches()
    yield
    clear_telemetry_caches()


def test_valid_exporter_types_equals_constant_set():
    assert VALID_EXPORTER_TYPES == {ExporterType.CONSOLE, ExporterType.OTLP, ExporterType.NONE}


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
    ("http://collector:4317", {"x-team": "k"}, {"endpoint": "http://collector:4317", "headers": {"x-team": "k"}}),
    ("http://collector:4317", None, {"endpoint": "http://collector:4317"}),
    (None, None, {}),
])
def test_span_exporter_otlp_kwargs(endpoint, headers, expected_kwargs):
    # Mock the module-level binding: the real constructor opens a gRPC channel.
    with mock.patch.object(tracer_module, "OTLPSpanExporter") as otlp_cls:
        config = make_config(exporter_type=ExporterType.OTLP, otlp_endpoint=endpoint, otlp_headers=headers)
        exporter = _create_exporter(config)

    otlp_cls.assert_called_once_with(**expected_kwargs)
    assert exporter is otlp_cls.return_value


@pytest.mark.parametrize("endpoint,headers,expected_kwargs", [
    ("http://collector:4317", {"x-team": "k"}, {"endpoint": "http://collector:4317", "headers": {"x-team": "k"}}),
    ("http://collector:4317", None, {"endpoint": "http://collector:4317"}),
    (None, None, {}),
])
def test_metric_exporter_otlp_kwargs(endpoint, headers, expected_kwargs):
    with mock.patch.object(meter_module, "OTLPMetricExporter") as otlp_cls:
        config = make_config(exporter_type=ExporterType.OTLP, otlp_endpoint=endpoint, otlp_headers=headers)
        exporter = _create_metric_exporter(config)

    otlp_cls.assert_called_once_with(**expected_kwargs)
    assert exporter is otlp_cls.return_value
