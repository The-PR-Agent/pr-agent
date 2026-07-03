"""
Tests for Databricks provider wiring in LiteLLMAIHandler.__init__.

Verifies that DATABRICKS.API_KEY / DATABRICKS.API_BASE settings are exported to
the env vars LiteLLM's Databricks provider reads (DATABRICKS_API_KEY /
DATABRICKS_API_BASE), and that nothing is exported when they are unset.
"""
import os

import litellm
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler

# Env vars LiteLLMAIHandler.__init__ branches on — clear them so the handler
# under test isn't influenced by (or leaking into) the runner environment.
_ISOLATED_ENV = (
    "DATABRICKS_API_KEY",
    "DATABRICKS_API_BASE",
    "OPENAI_API_KEY",
    "AWS_USE_IMDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION_NAME",
)


def _make_settings(overrides):
    """Minimal settings whose top-level .get() returns the provided overrides."""
    return type("Settings", (), {
        "config": type("Config", (), {
            "reasoning_effort": None,
            "ai_timeout": 30,
            "custom_reasoning_model": False,
            "max_model_tokens": 32000,
            "verbosity_level": 0,
            "seed": -1,
            "get": lambda self, key, default=None: default,
        })(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: overrides.get(key, default),
    })()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in _ISOLATED_ENV:
        monkeypatch.delenv(var, raising=False)
    # LiteLLMAIHandler.__init__ mutates the global litellm.api_key (sets the dummy
    # fallback when OPENAI_API_KEY is absent, as it is here). Snapshot it so this
    # file can't leak that global into order-dependent later tests.
    saved_api_key = litellm.api_key
    yield
    litellm.api_key = saved_api_key
    # Drop anything the handler wrote so it can't leak into other tests;
    # monkeypatch then restores any pre-existing originals.
    for var in ("DATABRICKS_API_KEY", "DATABRICKS_API_BASE"):
        os.environ.pop(var, None)


def test_databricks_env_vars_exported_from_settings(monkeypatch):
    overrides = {
        "DATABRICKS.API_KEY": "dapi-test-123",
        "DATABRICKS.API_BASE": "https://adb-1234.azuredatabricks.net/serving-endpoints",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    litellm_handler.LiteLLMAIHandler()

    assert os.environ["DATABRICKS_API_KEY"] == "dapi-test-123"
    assert os.environ["DATABRICKS_API_BASE"] == "https://adb-1234.azuredatabricks.net/serving-endpoints"


def test_databricks_env_vars_absent_when_unset(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))

    litellm_handler.LiteLLMAIHandler()

    assert "DATABRICKS_API_KEY" not in os.environ
    assert "DATABRICKS_API_BASE" not in os.environ


def test_databricks_api_base_optional(monkeypatch):
    """API base is optional (a workspace default may be configured elsewhere)."""
    overrides = {"DATABRICKS.API_KEY": "dapi-only-key"}
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    litellm_handler.LiteLLMAIHandler()

    assert os.environ["DATABRICKS_API_KEY"] == "dapi-only-key"
    assert "DATABRICKS_API_BASE" not in os.environ
