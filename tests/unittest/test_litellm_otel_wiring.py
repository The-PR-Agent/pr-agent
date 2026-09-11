"""Tests for the settings that hand pr-agent's telemetry choices to litellm.

pr-agent does not instrument LLM calls itself — litellm's built-in "otel"
callback does. These tests cover only the wiring in LiteLLMAIHandler.__init__:
that the configured values reach litellm's module globals and environment.
What litellm then emits is litellm's own behavior, covered by its test suite.
"""
import os

import litellm
import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

REQUEST_SPAN_ENV = "USE_OTEL_LITELLM_REQUEST_SPAN"


@pytest.fixture(autouse=True)
def _restore_litellm_globals(monkeypatch):
    """litellm's callbacks and turn_off_message_logging are process globals."""
    monkeypatch.setattr(litellm, "turn_off_message_logging", False, raising=False)
    monkeypatch.setattr(litellm, "success_callback", [], raising=False)
    monkeypatch.setattr(litellm, "failure_callback", [], raising=False)
    monkeypatch.setattr(litellm, "service_callback", [], raising=False)
    monkeypatch.setattr(litellm, "callbacks", [], raising=False)
    monkeypatch.delenv(REQUEST_SPAN_ENV, raising=False)


def _settings(**values):
    """Settings double whose .get answers the dotted keys __init__ reads."""
    return type("Settings", (), {
        "get": lambda self, key, default=None: values.get(key, default),
        "config": type("Config", (), {
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: default,
            "success_callback": values.get("LITELLM.SUCCESS_CALLBACK", []),
        })(),
    })()


def _build(monkeypatch, **values):
    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.get_settings", lambda: _settings(**values)
    )
    return LiteLLMAIHandler()


def test_turn_off_message_logging_reaches_litellm(monkeypatch):
    """Without this, litellm's callbacks attach the whole PR diff to what they emit."""
    _build(monkeypatch, **{"LITELLM.TURN_OFF_MESSAGE_LOGGING": True})

    assert litellm.turn_off_message_logging is True


def test_message_logging_untouched_when_not_configured(monkeypatch):
    """Absent config must not silently strip content from existing callback users."""
    _build(monkeypatch)

    assert litellm.turn_off_message_logging is False


def test_otel_callback_with_pr_agent_telemetry_requests_its_own_span(monkeypatch):
    """Otherwise litellm writes gen_ai attributes onto pr-agent's command span
    instead of emitting its own, and the two layers stop being separable."""
    _build(monkeypatch, **{
        "LITELLM.SUCCESS_CALLBACK": ["otel"],
        "OTEL.IS_ENABLED": True,
    })

    assert os.environ[REQUEST_SPAN_ENV] == "true"


@pytest.mark.parametrize("values", [
    {"LITELLM.SUCCESS_CALLBACK": ["otel"]},  # pr-agent telemetry off
    {"OTEL.IS_ENABLED": True},               # litellm otel callback off
])
def test_request_span_env_untouched_unless_both_layers_on(monkeypatch, values):
    _build(monkeypatch, **values)

    assert REQUEST_SPAN_ENV not in os.environ


def test_explicit_request_span_override_is_preserved(monkeypatch):
    """setdefault, not set: an operator who turned this off keeps it off."""
    monkeypatch.setenv(REQUEST_SPAN_ENV, "false")

    _build(monkeypatch, **{
        "LITELLM.SUCCESS_CALLBACK": ["otel"],
        "OTEL.IS_ENABLED": True,
    })

    assert os.environ[REQUEST_SPAN_ENV] == "false"
