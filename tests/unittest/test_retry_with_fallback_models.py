import asyncio

import pytest

from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import SENTINEL, restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "config.model",
    "config.model_weak",
    "config.model_reasoning",
    "config.fallback_models",
    "openai.deployment_id",
    "openai.fallback_deployments",
)


def _snapshot_settings():
    return snapshot_settings(_TRACKED_KEYS)


def _restore_settings(snapshot):
    restore_settings(snapshot)


def test_primary_model_success_invoked_once_and_returns_value():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "primary-model")
        get_settings().set("config.fallback_models", ["fallback-1", "fallback-2"])
        get_settings().set("openai.deployment_id", None)
        get_settings().set("openai.fallback_deployments", [])

        calls = []

        async def fake_f(model):
            calls.append(model)
            return "primary-result"

        result = asyncio.run(retry_with_fallback_models(fake_f))

        assert result == "primary-result"
        assert calls == ["primary-model"]
    finally:
        _restore_settings(snapshot)


def test_primary_fails_fallback_succeeds():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "primary-model")
        get_settings().set("config.fallback_models", ["fallback-1", "fallback-2"])
        get_settings().set("openai.deployment_id", None)
        get_settings().set("openai.fallback_deployments", [])

        calls = []

        async def fake_f(model):
            calls.append(model)
            if model == "primary-model":
                raise RuntimeError("primary failed")
            return f"ok:{model}"

        result = asyncio.run(retry_with_fallback_models(fake_f))

        assert result == "ok:fallback-1"
        assert calls == ["primary-model", "fallback-1"]
    finally:
        _restore_settings(snapshot)


def test_all_models_fail_raises_with_aggregate_message_and_cause():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "primary-model")
        get_settings().set("config.fallback_models", ["fallback-1"])
        get_settings().set("openai.deployment_id", None)
        get_settings().set("openai.fallback_deployments", [])

        last_error = ValueError("last failure")
        attempted = []

        async def fake_f(model):
            attempted.append(model)
            if model == "fallback-1":
                raise last_error
            raise RuntimeError("primary failure")

        with pytest.raises(Exception) as exc_info:
            asyncio.run(retry_with_fallback_models(fake_f))

        assert attempted == ["primary-model", "fallback-1"]
        assert "Failed to generate prediction with any model" in str(exc_info.value)
        # Production code uses `raise ... from e`, so the last failure should be chained.
        assert exc_info.value.__cause__ is last_error
    finally:
        _restore_settings(snapshot)


def test_deployment_id_updated_per_attempt():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "primary-model")
        get_settings().set("config.fallback_models", ["fallback-1", "fallback-2"])
        get_settings().set("openai.deployment_id", "deployment-primary")
        get_settings().set(
            "openai.fallback_deployments",
            ["deployment-fb1", "deployment-fb2"],
        )

        observed = []

        async def fake_f(model):
            observed.append(
                (model, get_settings().get("openai.deployment_id", None))
            )
            if model != "fallback-1":
                raise RuntimeError(f"fail for {model}")
            return "fallback-ok"

        result = asyncio.run(retry_with_fallback_models(fake_f))

        assert result == "fallback-ok"
        assert observed == [
            ("primary-model", "deployment-primary"),
            ("fallback-1", "deployment-fb1"),
        ]
    finally:
        _restore_settings(snapshot)


def test_weak_model_type_uses_weak_setting_and_forwards_identifier():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "regular-model")
        get_settings().set("config.model_weak", "weak-model-id")
        get_settings().set("config.fallback_models", [])
        get_settings().set("openai.deployment_id", None)
        get_settings().set("openai.fallback_deployments", [])

        calls = []

        async def fake_f(model):
            calls.append(model)
            return model

        result = asyncio.run(
            retry_with_fallback_models(fake_f, model_type=ModelType.WEAK)
        )

        assert result == "weak-model-id"
        assert calls == ["weak-model-id"]
    finally:
        _restore_settings(snapshot)


def test_reasoning_model_type_uses_reasoning_setting():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "regular-model")
        get_settings().set("config.model_reasoning", "reasoning-model-id")
        get_settings().set("config.fallback_models", [])
        get_settings().set("openai.deployment_id", None)
        get_settings().set("openai.fallback_deployments", [])

        calls = []

        async def fake_f(model):
            calls.append(model)
            return model

        result = asyncio.run(
            retry_with_fallback_models(fake_f, model_type=ModelType.REASONING)
        )

        assert result == "reasoning-model-id"
        assert calls == ["reasoning-model-id"]
    finally:
        _restore_settings(snapshot)


def test_restore_settings_truly_removes_originally_missing_dotted_keys():
    """Regression: SENTINEL-snapshotted dotted leaves must be removed, not left behind."""
    settings = get_settings()
    key = "openai.fallback_deployments"
    # Ensure key is absent on entry; if a previous test leaked it, clean it.
    if settings.get(key, SENTINEL) is not SENTINEL:
        _restore_settings({key: SENTINEL})
    assert settings.get(key, SENTINEL) is SENTINEL

    snapshot = _snapshot_settings()
    try:
        settings.set(key, ["leaked-deployment"])
        assert settings.get(key) == ["leaked-deployment"]
    finally:
        _restore_settings(snapshot)

    assert settings.get(key, SENTINEL) is SENTINEL
