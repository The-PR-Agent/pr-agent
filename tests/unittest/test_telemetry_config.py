"""Startup/configuration tests for pr_agent.telemetry.config.

Covers ``get_otel_config()`` reading settings via ``get_settings().get``, every
validation/fallback branch, OTLP header parsing, secret hygiene in log output,
and — importantly — that the *shipped* defaults in
``pr_agent/settings/configuration.toml`` ``[otel]`` and
``pr_agent/settings/.secrets_template.toml`` ``[otel]`` form a valid
configuration when run through the real validation logic.
"""

import tomllib
from pathlib import Path

import pytest

import pr_agent
from pr_agent.algo.utils import get_version
from pr_agent.telemetry import config as config_module
from pr_agent.telemetry.config import VALID_EXPORTER_TYPES, _parse_otlp_headers, get_otel_config
from pr_agent.telemetry.types import ExporterType
from tests.unittest._telemetry_helpers import capture_loguru, clear_telemetry_caches

SETTINGS_DIR = Path(pr_agent.__file__).parent / "settings"


@pytest.fixture(autouse=True)
def _reset_telemetry():
    clear_telemetry_caches()
    yield
    clear_telemetry_caches()


class FakeSettings:
    """Minimal stand-in for Dynaconf exposing .get(key, default)."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _use_settings(monkeypatch, values):
    # config.py binds get_settings at import time, so patch it there —
    # patching pr_agent.config_loader.get_settings would not take effect.
    monkeypatch.setattr(config_module, "get_settings", lambda: FakeSettings(values))


VALID_ENABLED_SETTINGS = {
    "OTEL.IS_ENABLED": True,
    "OTEL.EXPORTER_TYPE": "console",
    "OTEL.SERVICE_NAME": "pr-agent",
    "OTEL.ENVIRONMENT": "development",
}


def test_disabled_by_default_returns_inert_config(monkeypatch):
    _use_settings(monkeypatch, {})

    config = get_otel_config()

    assert config.is_enabled is False
    assert config.exporter_type is None
    assert config.service_name is None
    assert config.service_version is None
    assert config.environment is None
    assert config.otlp_endpoint is None
    assert config.otlp_headers is None


def test_enabled_with_valid_console_settings(monkeypatch):
    _use_settings(monkeypatch, dict(VALID_ENABLED_SETTINGS))

    config = get_otel_config()

    assert config.is_enabled is True
    assert config.exporter_type == ExporterType.CONSOLE
    assert config.service_name == "pr-agent"
    assert config.environment == "development"
    assert config.service_version == get_version()


@pytest.mark.parametrize("settings", [
    {**VALID_ENABLED_SETTINGS, "OTEL.EXPORTER_TYPE": "bogus"},
    # Validation order guarantee: the bad type must surface even when the config
    # would anyway fall back for missing fields.
    {"OTEL.IS_ENABLED": True, "OTEL.EXPORTER_TYPE": "bogus"},
])
def test_invalid_exporter_type_raises_value_error(monkeypatch, settings):
    _use_settings(monkeypatch, settings)

    with pytest.raises(ValueError, match="Invalid OTEL.EXPORTER_TYPE 'bogus'"):
        get_otel_config()


def test_missing_required_fields_falls_back_disabled_with_warning(monkeypatch):
    settings = dict(VALID_ENABLED_SETTINGS)
    del settings["OTEL.SERVICE_NAME"]
    _use_settings(monkeypatch, settings)

    with capture_loguru(level="WARNING") as captured:
        config = get_otel_config()

    assert config.is_enabled is False, "incomplete config must fall back to disabled"
    assert "missing required configuration" in "\n".join(captured)


def test_otlp_without_endpoint_fails_closed_with_warning(monkeypatch):
    """A missing OTLP endpoint must disable export entirely — falling back to the
    console exporter would redirect telemetry (including opted-in PR URLs or
    error details) into process logs, a destination the operator never chose."""
    _use_settings(monkeypatch, {**VALID_ENABLED_SETTINGS, "OTEL.EXPORTER_TYPE": "otlp"})

    with capture_loguru(level="WARNING") as captured:
        config = get_otel_config()

    assert config.is_enabled is False, "missing OTLP endpoint must fail closed, not fall back"
    assert "OTEL.OTLP_ENDPOINT is not configured" in "\n".join(captured)


def test_otlp_with_endpoint_and_headers_parses_headers(monkeypatch):
    _use_settings(monkeypatch, {
        **VALID_ENABLED_SETTINGS,
        "OTEL.EXPORTER_TYPE": "otlp",
        "OTEL.OTLP_ENDPOINT": "http://collector:4318",
        "OTEL.OTLP_HEADERS": "x-team=abc,Authorization=Bearer tok",
    })

    config = get_otel_config()

    assert config.is_enabled is True
    assert config.exporter_type == ExporterType.OTLP
    assert config.otlp_endpoint == "http://collector:4318"
    assert config.otlp_headers == {"x-team": "abc", "Authorization": "Bearer tok"}


def test_otlp_timeout_defaults_and_reads_setting(monkeypatch):
    _use_settings(monkeypatch, dict(VALID_ENABLED_SETTINGS))
    assert get_otel_config().otlp_timeout == 3

    # String value covers env-var style settings; config.py coerces with int().
    _use_settings(monkeypatch, {**VALID_ENABLED_SETTINGS, "OTEL.OTLP_TIMEOUT": "10"})
    assert get_otel_config().otlp_timeout == 10


def test_otlp_protocol_defaults_to_http_and_reads_setting(monkeypatch):
    _use_settings(monkeypatch, dict(VALID_ENABLED_SETTINGS))
    assert get_otel_config().otlp_protocol == "http"

    # Case/whitespace tolerant, like env-var style settings.
    _use_settings(monkeypatch, {**VALID_ENABLED_SETTINGS, "OTEL.OTLP_PROTOCOL": " gRPC "})
    assert get_otel_config().otlp_protocol == "grpc"


def test_invalid_otlp_protocol_raises_value_error(monkeypatch):
    _use_settings(monkeypatch, {**VALID_ENABLED_SETTINGS, "OTEL.OTLP_PROTOCOL": "quic"})

    with pytest.raises(ValueError, match="Invalid OTEL.OTLP_PROTOCOL 'quic'"):
        get_otel_config()


@pytest.mark.parametrize("raw,expected", [
    ("", {}),
    ("   ", {}),
    ("x-honeycomb-team=KEY", {"x-honeycomb-team": "KEY"}),
    ("a=1,b=2", {"a": "1", "b": "2"}),
    # value containing '=' — split only on the first one
    ("Authorization=Bearer x=y", {"Authorization": "Bearer x=y"}),
    # whitespace around pairs, keys, and values is trimmed
    ("  a = 1 ,  b = 2  ", {"a": "1", "b": "2"}),
    # pair without '=' is silently skipped
    ("a=1,malformed,b=2", {"a": "1", "b": "2"}),
])
def test_parse_otlp_headers_cases(raw, expected):
    assert _parse_otlp_headers(raw) == expected


def test_secrets_never_appear_in_log_output(monkeypatch):
    """The OTLP endpoint and header values are secrets; the warning paths in
    get_otel_config must never render them (intentional per config.py)."""
    endpoint_sentinel = "SENTINEL-OTLP-ENDPOINT-NO-LEAK"
    header_sentinel = "SENTINEL-OTLP-HEADER-NO-LEAK"

    # Branch 1: missing required fields (service_name absent) with both secrets set.
    _use_settings(monkeypatch, {
        "OTEL.IS_ENABLED": True,
        "OTEL.EXPORTER_TYPE": "otlp",
        "OTEL.ENVIRONMENT": "development",
        "OTEL.OTLP_ENDPOINT": endpoint_sentinel,
        "OTEL.OTLP_HEADERS": f"x-team={header_sentinel}",
    })
    with capture_loguru(level="DEBUG") as captured_missing:
        get_otel_config()

    # Branch 2: otlp without endpoint (fail-closed warning) with secret headers set.
    _use_settings(monkeypatch, {
        **VALID_ENABLED_SETTINGS,
        "OTEL.EXPORTER_TYPE": "otlp",
        "OTEL.OTLP_HEADERS": f"x-team={header_sentinel}",
    })
    with capture_loguru(level="DEBUG") as captured_fallback:
        get_otel_config()

    combined = "\n".join(captured_missing + captured_fallback)
    assert endpoint_sentinel not in combined, "OTLP endpoint must never be logged"
    assert header_sentinel not in combined, "OTLP header values must never be logged"


# ---------------------------------------------------------------------------
# Shipped-defaults validity: the [otel] sections users actually edit
# (configuration.toml lines ~379-385 and .secrets_template.toml lines ~90-94)
# must constitute a valid configuration.
# ---------------------------------------------------------------------------

def _load_shipped_otel_sections():
    with open(SETTINGS_DIR / "configuration.toml", "rb") as f:
        configuration = tomllib.load(f)["otel"]
    with open(SETTINGS_DIR / ".secrets_template.toml", "rb") as f:
        secrets_template = tomllib.load(f)["otel"]
    return configuration, secrets_template


def test_shipped_configuration_toml_otel_section_values():
    configuration, _ = _load_shipped_otel_sections()

    assert configuration["is_enabled"] is False, "telemetry must ship disabled by default"
    assert configuration["exporter_type"] in VALID_EXPORTER_TYPES
    assert configuration["exporter_type"] == ExporterType.CONSOLE
    assert configuration["service_name"] == "pr-agent"
    assert configuration["environment"] == "development"
    assert configuration["otlp_timeout"] == 3
    assert configuration["otlp_protocol"] == "http", "the default transport must not need an extra"
    assert configuration["include_pr_url"] is False, "PR URLs must be opt-in (privacy)"
    assert configuration["include_error_details"] is False, "error details must be opt-in (privacy)"


def test_shipped_secrets_template_otel_section_values():
    _, secrets_template = _load_shipped_otel_sections()

    assert secrets_template["otlp_endpoint"] == "", "template must ship with empty endpoint"
    assert secrets_template["otlp_headers"] == "", "template must ship with empty headers"
    assert _parse_otlp_headers(secrets_template["otlp_headers"]) == {}


def test_shipped_defaults_form_valid_config_through_get_otel_config(monkeypatch):
    """Run the real shipped [otel] defaults through get_otel_config: as-shipped
    they resolve to a clean disabled config, and flipping only is_enabled=true
    yields a valid enabled console config with no fallback warnings."""
    configuration, secrets_template = _load_shipped_otel_sections()
    merged = {**configuration, **secrets_template}

    def as_settings(values):
        return {f"OTEL.{key.upper()}": value for key, value in values.items()}

    # (a) As shipped: disabled, no warnings, no exceptions.
    _use_settings(monkeypatch, as_settings(merged))
    with capture_loguru(level="WARNING") as captured:
        config = get_otel_config()
    assert config.is_enabled is False
    assert captured == [], "shipped defaults must not produce warnings"

    # (b) Only is_enabled flipped: a valid, enabled console configuration.
    _use_settings(monkeypatch, as_settings({**merged, "is_enabled": True}))
    with capture_loguru(level="WARNING") as captured:
        config = get_otel_config()
    assert config.is_enabled is True
    assert config.exporter_type == ExporterType.CONSOLE
    assert config.service_name == "pr-agent"
    assert config.environment == "development"
    assert captured == [], "enabling shipped defaults must not trigger fallback warnings"
