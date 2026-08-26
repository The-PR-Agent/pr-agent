"""Tests for pr_agent/mosaico/env_bridge.py and observability.py.

These tests mutate the module-global ``global_settings`` (no request context is active),
so the ``restore_settings`` fixture snapshots and restores the 7 settings keys the
env-bridge touches. Test #8 also mutates litellm module state, restored in teardown.

asyncio_mode=auto; monkeypatch is the repo convention for env vars."""
import litellm
import pytest

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.mosaico.env_bridge import (apply_mosaico_env,
                                         langfuse_env_present)
from pr_agent.mosaico.observability import (mosaico_log_context,
                                            parse_observability_metadata)

_SNAPSHOT_KEYS = [
    "OPENAI.API_BASE",
    "OPENAI.KEY",
    "CONFIG.MODEL",
    "CONFIG.FALLBACK_MODELS",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS",
    "LITELLM.SUCCESS_CALLBACK",
    "LITELLM.FAILURE_CALLBACK",
    "LITELLM.ENABLE_CALLBACKS",
]

_MOSAICO_ENV_VARS = [
    "API_BASE", "API_KEY", "MODEL_NAME", "MODEL_MAX_TOKENS",
    "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
]

_SENTINEL = object()


@pytest.fixture
def restore_settings():
    """Snapshot the 7 settings keys (+ litellm callback module attrs) and restore after."""
    snapshot = {k: global_settings.get(k, _SENTINEL) for k in _SNAPSHOT_KEYS}
    litellm_success = litellm.success_callback
    litellm_failure = litellm.failure_callback
    yield
    for k, v in snapshot.items():
        if v is _SENTINEL:
            # Key was absent before; best-effort reset to empty so the global is not polluted.
            global_settings.set(k, [] if k.endswith("CALLBACK") or k == "CONFIG.FALLBACK_MODELS" else None)
        else:
            global_settings.set(k, v)
    litellm.success_callback = litellm_success
    litellm.failure_callback = litellm_failure


@pytest.fixture
def clear_mosaico_env(monkeypatch):
    for var in _MOSAICO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestApplyMosaicoEnv:
    def test_apply_mosaico_env_noop_when_absent(self, restore_settings, clear_mosaico_env):
        """THE load-bearing regression guard: with no MOSAICO env, nothing changes."""
        before = {k: global_settings.get(k, _SENTINEL) for k in _SNAPSHOT_KEYS}
        apply_mosaico_env()
        after = {k: global_settings.get(k, _SENTINEL) for k in _SNAPSHOT_KEYS}
        assert before == after

    def test_apply_mosaico_env_maps_llm(self, restore_settings, clear_mosaico_env, monkeypatch):
        monkeypatch.setenv("API_BASE", "https://mosaico.example/v1")
        monkeypatch.setenv("API_KEY", "secret-key")
        monkeypatch.setenv("MODEL_NAME", "my-model")
        apply_mosaico_env()
        s = get_settings()
        assert s.get("OPENAI.API_BASE") == "https://mosaico.example/v1"
        assert s.get("OPENAI.KEY") == "secret-key"
        assert s.get("CONFIG.MODEL") == "openai/my-model"
        assert s.get("CONFIG.FALLBACK_MODELS") == []

    def test_model_name_with_prefix_not_double_prefixed(self, restore_settings, clear_mosaico_env, monkeypatch):
        monkeypatch.setenv("MODEL_NAME", "anthropic/claude-x")
        apply_mosaico_env()
        assert get_settings().get("CONFIG.MODEL") == "anthropic/claude-x"

    def test_register_langfuse_callback_appends_once(self, restore_settings, clear_mosaico_env, monkeypatch):
        monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        assert langfuse_env_present() is True
        apply_mosaico_env()
        apply_mosaico_env()  # idempotent
        s = get_settings()
        success = list(s.get("LITELLM.SUCCESS_CALLBACK") or [])
        failure = list(s.get("LITELLM.FAILURE_CALLBACK") or [])
        assert success.count("langfuse_otel") == 1
        assert failure.count("langfuse_otel") == 1
        assert s.get("LITELLM.ENABLE_CALLBACKS") is True

    def test_register_langfuse_callback_removes_legacy(self, restore_settings, clear_mosaico_env, monkeypatch):
        """Legacy 'langfuse' entries must be stripped and replaced by exactly one 'langfuse_otel'."""
        monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        # Pre-seed legacy callback in both lists
        global_settings.set("LITELLM.SUCCESS_CALLBACK", ["langfuse"])
        global_settings.set("LITELLM.FAILURE_CALLBACK", ["langfuse"])
        apply_mosaico_env()
        s = get_settings()
        assert list(s.get("LITELLM.SUCCESS_CALLBACK") or []) == ["langfuse_otel"]
        assert list(s.get("LITELLM.FAILURE_CALLBACK") or []) == ["langfuse_otel"]

    def test_langfuse_not_registered_without_keys(self, restore_settings, clear_mosaico_env, monkeypatch):
        monkeypatch.setenv("API_BASE", "https://mosaico.example/v1")
        # snapshot/reset callbacks to empty so we observe no registration
        global_settings.set("LITELLM.SUCCESS_CALLBACK", [])
        global_settings.set("LITELLM.FAILURE_CALLBACK", [])
        apply_mosaico_env()
        s = get_settings()
        assert list(s.get("LITELLM.SUCCESS_CALLBACK") or []) == []
        assert list(s.get("LITELLM.FAILURE_CALLBACK") or []) == []

    def test_model_max_tokens_valid_positive_value(self, restore_settings, clear_mosaico_env, monkeypatch):
        """A valid positive MODEL_MAX_TOKENS is written through unchanged."""
        monkeypatch.setenv("MODEL_NAME", "my-model")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "8192")
        apply_mosaico_env()
        assert get_settings().get("CONFIG.CUSTOM_MODEL_MAX_TOKENS") == 8192

    def test_model_max_tokens_zero_falls_back_to_default(self, restore_settings, clear_mosaico_env, monkeypatch):
        """MODEL_MAX_TOKENS=0 is non-positive; must degrade to DEFAULT_CUSTOM_MODEL_MAX_TOKENS."""
        from pr_agent.mosaico.env_bridge import DEFAULT_CUSTOM_MODEL_MAX_TOKENS
        monkeypatch.setenv("MODEL_NAME", "my-model")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "0")
        apply_mosaico_env()
        assert get_settings().get("CONFIG.CUSTOM_MODEL_MAX_TOKENS") == DEFAULT_CUSTOM_MODEL_MAX_TOKENS

    def test_model_max_tokens_negative_falls_back_to_default(self, restore_settings, clear_mosaico_env, monkeypatch):
        """MODEL_MAX_TOKENS=-5 is non-positive; must degrade to DEFAULT_CUSTOM_MODEL_MAX_TOKENS."""
        from pr_agent.mosaico.env_bridge import DEFAULT_CUSTOM_MODEL_MAX_TOKENS
        monkeypatch.setenv("MODEL_NAME", "my-model")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "-5")
        apply_mosaico_env()
        assert get_settings().get("CONFIG.CUSTOM_MODEL_MAX_TOKENS") == DEFAULT_CUSTOM_MODEL_MAX_TOKENS

    def test_apply_mosaico_env_registers_langfuse_on_real_handler(
            self, restore_settings, clear_mosaico_env, monkeypatch):
        """Integration test: bridge populates settings -> a REAL
        LiteLLMAIHandler() applies them to the litellm module attributes."""
        monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.setenv("API_BASE", "https://mosaico.example/v1")
        monkeypatch.setenv("MODEL_NAME", "my-model")
        # Ensure a clean callback baseline so the handler assignment is observable.
        global_settings.set("LITELLM.SUCCESS_CALLBACK", [])
        global_settings.set("LITELLM.FAILURE_CALLBACK", [])
        litellm.success_callback = []
        litellm.failure_callback = []

        apply_mosaico_env()

        from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
        LiteLLMAIHandler()
        assert litellm.success_callback == ["langfuse_otel"]
        assert litellm.failure_callback == ["langfuse_otel"]


class TestParseObservabilityMetadata:
    def test_all_three_keys(self):
        raw = {
            "mosaico-root-task-id": "r1",
            "mosaico-super-task-id": "s1",
            "mosaico-root-task-name": "name1",
        }
        assert parse_observability_metadata(raw) == raw

    def test_missing_one_key_returns_partial_dict(self):
        raw = {"mosaico-root-task-id": "r1", "mosaico-root-task-name": "name1"}
        out = parse_observability_metadata(raw)
        assert out == {"mosaico-root-task-id": "r1", "mosaico-root-task-name": "name1"}
        assert out != {}

    def test_non_string_value_key_omitted(self):
        raw = {"mosaico-root-task-id": "r1", "mosaico-super-task-id": 12345}
        assert parse_observability_metadata(raw) == {"mosaico-root-task-id": "r1"}

    def test_non_mapping_returns_empty_and_never_raises(self):
        for bad in (None, [], "string", 42):
            assert parse_observability_metadata(bad) == {}

    def test_extra_keys_ignored(self):
        raw = {"mosaico-root-task-id": "r1", "unrelated": "x"}
        assert parse_observability_metadata(raw) == {"mosaico-root-task-id": "r1"}


class TestMosaicoLogContext:
    def test_binds_ids(self):
        from pr_agent.log import get_logger
        captured = {}

        def sink(message):
            captured.update(message.record["extra"])

        logger = get_logger()
        handler_id = logger.add(sink, format="{message}")
        try:
            meta = {"mosaico-root-task-id": "r1", "mosaico-super-task-id": "s1"}
            with mosaico_log_context(meta, "ctx-123"):
                logger.info("inside")
            assert captured.get("mosaico-root-task-id") == "r1"
            assert captured.get("mosaico-super-task-id") == "s1"
            assert captured.get("context_id") == "ctx-123"
            # After exit, a fresh log record must not carry the bindings.
            captured.clear()
            logger.info("outside")
            assert "mosaico-root-task-id" not in captured
        finally:
            logger.remove(handler_id)

    def test_empty_meta_is_clean_passthrough(self):
        # Must not raise and must yield even with empty meta and no context_id.
        with mosaico_log_context({}, None):
            pass
