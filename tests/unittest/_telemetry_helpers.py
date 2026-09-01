"""Test-only helpers for the pr_agent.telemetry test suite.

The telemetry entry points (``get_tracer``, ``get_meter``, the instrument
getters, and ``register_shutdown_handler``) are all ``functools.lru_cache``
singletons, so tests must clear them to avoid leaking a tracer initialized by
one test into the next.

OpenTelemetry's global providers are write-once: ``trace.set_tracer_provider``
and ``metrics.set_meter_provider`` silently no-op on a second call, poisoning
the whole pytest process. Helpers here therefore build *local* providers and
never touch the globals.
"""

from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_agent.telemetry.types import TelemetryConfig


def clear_telemetry_caches():
    """Reset every lru_cache'd telemetry entry point and the provider registry."""
    from pr_agent.telemetry import meter, registry, shutdown, tracer

    tracer.get_tracer.cache_clear()
    meter.get_meter.cache_clear()
    meter.get_commands_counter.cache_clear()
    shutdown.register_shutdown_handler.cache_clear()
    registry.provider_registry.reset()


def make_config(**overrides) -> TelemetryConfig:
    """Build a TelemetryConfig with all required fields filled; override per test."""
    fields = dict(
        is_enabled=True,
        exporter_type=None,
        service_name="test-svc",
        service_version="0.0-test",
        environment="test",
        otlp_endpoint=None,
        otlp_headers=None,
    )
    fields.update(overrides)
    return TelemetryConfig(**fields)


@contextmanager
def capture_loguru(level: str = "DEBUG"):
    """Capture loguru output into a list of rendered lines.

    pr-agent uses loguru; pytest's capsys/caplog don't capture it because the
    sink was bound to sys.stderr before pytest swapped it. Add a loguru sink
    directly so tests see what would actually land in a real log.
    """
    from loguru import logger as loguru_logger

    captured_lines = []
    sink_id = loguru_logger.add(
        lambda msg: captured_lines.append(str(msg)),
        level=level,
    )
    try:
        yield captured_lines
    finally:
        loguru_logger.remove(sink_id)


def build_in_memory_tracer():
    """Return (tracer, exporter) backed by a local provider — not the global one.

    SimpleSpanProcessor exports synchronously on span end, so finished spans
    are visible in the exporter without an explicit flush.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("telemetry-test"), exporter
