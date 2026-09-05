"""HTTP /health route tests.

Exercise build_app() directly through an in-process ASGI transport and monkeypatch
health_check (the no-retry behavior itself was proven in 2c). Verify the route and
200/503 response shape without relying on Starlette's thread-backed TestClient.

Also exercises the REAL health_check() (no stub) to lock in Fix A: the removed
'stop'-param gate must NOT short-circuit /health for models that lack 'stop', since
PR-Agent's LiteLLMAIHandler never sends 'stop'."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import litellm
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
import pr_agent.algo.ai_handlers.litellm_helpers as litellm_helpers
import pr_agent.mosaico.executor as executor_mod
import pr_agent.mosaico.server as server_mod
from pr_agent.config_loader import get_settings
from pr_agent.mosaico.server import build_app
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


def _app(monkeypatch, health_value):
    async def fake_health_check():
        return health_value

    # health_check is imported into server_mod's namespace and called by _HealthApp._health.
    monkeypatch.setattr(server_mod, "health_check", fake_health_check)
    return build_app()


async def _get_health(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/health")


class TestHealthRoute:
    @pytest.mark.asyncio
    async def test_healthy_returns_200(self, monkeypatch):
        resp = await _get_health(_app(monkeypatch, "OK"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_healthy"] is True
        assert body["status"] == "OK"

    @pytest.mark.asyncio
    async def test_unhealthy_returns_503(self, monkeypatch):
        resp = await _get_health(_app(monkeypatch, "Unhealthy: connection refused"))
        assert resp.status_code == 503
        body = resp.json()
        assert body["is_healthy"] is False
        assert "Unhealthy" in body["status"]
        assert "Unhealthy" in body["detail"]


# A model id whose litellm-reported supported params genuinely LACK 'stop' (verified
# under the pinned litellm). Under the OLD (removed) gate, health_check() short-circuited
# to "Unhealthy: LLM does not support 'stop' parameter" for exactly such models — so these
# tests would have failed before Fix A. They guard against the gate being reintroduced.
_MODEL_WITHOUT_STOP = "perplexity/sonar"


def _probe_handler(provider_params=None):
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.azure = False
    handler.streaming_required_models = litellm_handler.STREAMING_REQUIRED_MODELS
    handler.force_streaming_provider = ""
    handler.force_streaming_api_base_substrings = []
    handler._aws_imds_mode = False
    handler._azure_ad = False
    handler._provider_request_params = provider_params or {}
    handler._custom_llm_provider = ""
    handler._request_provider_cache = {}
    handler._aws_active_creds = {}
    return handler


@pytest.fixture
def restore_config_model():
    """Restore LLM settings exactly, including originally-absent state."""
    snapshot = snapshot_settings(
        ["CONFIG.MODEL", "LITELLM.CUSTOM_LLM_PROVIDER"]
    )
    yield get_settings()
    restore_settings(snapshot)


class TestHealthCheckGate:
    """Exercise the REAL health_check() (not the monkeypatched stub) to lock in Fix A."""

    @pytest.mark.asyncio
    async def test_model_without_stop_probes_live_and_returns_ok(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", _MODEL_WITHOUT_STOP)

        called = {}

        async def fake_acompletion(**kwargs):
            called.update(kwargs)
            return {"choices": [{"message": {"content": "pong"}}]}

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        # Must NOT short-circuit on the missing 'stop' param; it reaches the live probe.
        assert result == "OK"
        assert called.get("model") == _MODEL_WITHOUT_STOP

    @pytest.mark.asyncio
    async def test_live_probe_failure_returns_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", _MODEL_WITHOUT_STOP)
        call_count = 0

        async def boom_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("connection refused")

        monkeypatch.setattr(litellm, "acompletion", boom_acompletion)

        result = await executor_mod.health_check()
        assert result == "Unhealthy: LLM check failed"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_streaming_required_model_is_consumed(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")
        consumed = []

        async def stream_response():
            consumed.append("started")
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="pong"),
                        finish_reason="stop",
                    )
                ]
            )
            consumed.append("finished")

        async def fake_acompletion(**kwargs):
            assert kwargs["stream"] is True
            return stream_response()

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "OK"
        assert consumed == ["started", "finished"]

    @pytest.mark.asyncio
    async def test_streaming_probe_enforces_total_timeout_and_closes_stream(self, monkeypatch):
        handler = _probe_handler()
        real_timeout = asyncio.timeout
        timeout_calls = []

        class StalledStream:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = StalledStream()

        async def fake_acompletion(**kwargs):
            return stream

        def track_timeout(delay):
            timeout_calls.append(delay)
            return real_timeout(delay)

        monkeypatch.setattr(litellm_handler.asyncio, "timeout", track_timeout)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                handler.probe_completion("openai/qwq-plus", timeout=0.01, _completion=fake_acompletion),
                timeout=0.5,
            )

        assert timeout_calls == [0.01, litellm_helpers.CANCELLATION_CLEANUP_SECONDS]
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_streaming_probe_bounds_close_after_total_timeout(self, monkeypatch):
        handler = _probe_handler()
        monkeypatch.setattr(litellm_handler, "CANCELLATION_CLEANUP_SECONDS", 0.01)

        class StalledStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Event().wait()

            async def aclose(self):
                await asyncio.Event().wait()

        async def fake_acompletion(**kwargs):
            return StalledStream()

        task = asyncio.create_task(
            handler.probe_completion("openai/qwq-plus", timeout=0.01, _completion=fake_acompletion)
        )
        done, pending = await asyncio.wait({task}, timeout=0.3)

        if pending:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert done == {task}
        assert not pending
        with pytest.raises(TimeoutError):
            task.result()

    @pytest.mark.asyncio
    async def test_streaming_reasoning_only_response_is_healthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")

        async def stream_response():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content="thinking"),
                        finish_reason="stop",
                    )
                ]
            )

        async def fake_acompletion(**kwargs):
            return stream_response()

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        assert await executor_mod.health_check() == "OK"

    @pytest.mark.asyncio
    async def test_streaming_content_without_finish_reason_is_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")

        async def stream_response():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="pong"),
                        finish_reason=None,
                    )
                ]
            )

        async def fake_acompletion(**kwargs):
            return stream_response()

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        assert await executor_mod.health_check() == "Unhealthy: LLM check failed"

    @pytest.mark.asyncio
    async def test_streaming_finish_event_without_content_is_healthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")

        async def stream_response():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content=None),
                        finish_reason="length",
                    )
                ]
            )

        async def fake_acompletion(**kwargs):
            return stream_response()

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        assert await executor_mod.health_check() == "OK"

    @pytest.mark.asyncio
    async def test_streaming_metadata_only_response_is_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")

        async def stream_response():
            yield SimpleNamespace(choices=[], usage={"total_tokens": 0})

        async def fake_acompletion(**kwargs):
            return stream_response()

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        assert await executor_mod.health_check() == "Unhealthy: LLM check failed"

    @pytest.mark.asyncio
    async def test_azure_routed_streaming_required_model_is_consumed(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")
        consumed = []

        async def stream_response():
            consumed.append("started")
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="pong"),
                        finish_reason="stop",
                    )
                ]
            )
            consumed.append("finished")

        async def fake_acompletion(**kwargs):
            assert kwargs["model"] == "azure/qwq-plus"
            assert kwargs["stream"] is True
            return stream_response()

        handler = _probe_handler()
        handler.azure = True
        monkeypatch.setattr(litellm_handler, "LiteLLMAIHandler", lambda: handler)
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "OK"
        assert consumed == ["started", "finished"]

    @pytest.mark.asyncio
    async def test_streaming_probe_failure_redacts_provider_details(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "openai/qwq-plus")
        secret = "provider-secret"

        class FailingStream:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError(f"stream failed with {secret}")

            async def aclose(self):
                self.closed = True

        stream = FailingStream()

        async def fake_acompletion(**kwargs):
            return stream

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        logger = MagicMock()
        monkeypatch.setattr(executor_mod, "get_logger", lambda: logger)
        monkeypatch.setattr(litellm_helpers, "get_logger", lambda: logger)

        result = await executor_mod.health_check()

        assert result == "Unhealthy: LLM check failed"
        assert stream.closed is True
        logger.error.assert_called_once_with("Error handling streaming response: RuntimeError")
        assert secret not in logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_live_probe_rejects_missing_choices(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", _MODEL_WITHOUT_STOP)

        async def fake_acompletion(**kwargs):
            return None

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "Unhealthy: LLM check failed"

    @pytest.mark.asyncio
    async def test_live_probe_failure_does_not_expose_provider_credentials(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "moonshot/kimi-k3")
        secret = "moonshot-secret"
        called = {}

        async def fake_acompletion(**kwargs):
            called.update(kwargs)
            api_key = kwargs["api_key"]
            raise RuntimeError(f"request failed with {api_key}")

        handler = _probe_handler({"moonshot": {"api_key": secret}})
        monkeypatch.setattr(litellm_handler, "LiteLLMAIHandler", lambda: handler)
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        logger = MagicMock()
        monkeypatch.setattr(executor_mod, "get_logger", lambda: logger)

        result = await executor_mod.health_check()

        assert called["api_key"] == secret
        assert result == "Unhealthy: LLM check failed"
        logger.warning.assert_called_once_with("MOSAICO health_check unhealthy: RuntimeError")
        assert secret not in logger.warning.call_args.args[0]

    @pytest.mark.asyncio
    async def test_live_probe_forwards_request_local_provider_settings(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "moonshot/kimi-k3")
        called = {}

        async def fake_acompletion(**kwargs):
            called.update(kwargs)
            return {"choices": [{"message": {"content": "pong"}}]}

        handler = _probe_handler({
            "moonshot": {
                "api_key": "moonshot-key",
                "api_base": "https://api.moonshot.cn/v1",
            }
        })
        monkeypatch.setattr(litellm_handler, "LiteLLMAIHandler", lambda: handler)
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "OK"
        assert called["api_key"] == "moonshot-key"
        assert called["api_base"] == "https://api.moonshot.cn/v1"

    @pytest.mark.asyncio
    async def test_live_probe_uses_custom_provider_and_forced_streaming(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "hosted-model")
        consumed = []

        async def stream_response():
            consumed.append("started")
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="pong"),
                        finish_reason="stop",
                    )
                ]
            )
            consumed.append("finished")

        async def fake_acompletion(**kwargs):
            assert kwargs["model"] == "hosted-model"
            assert kwargs["custom_llm_provider"] == "openai"
            assert kwargs["api_key"] == "gateway-key"
            assert kwargs["stream"] is True
            assert kwargs["stream_options"] == {"include_usage": True}
            return stream_response()

        handler = _probe_handler({
            "openai": {
                "api_key": "gateway-key",
                "api_base": "https://example.snowflakecomputing.com/v1",
            }
        })
        handler.azure = True
        handler._custom_llm_provider = "openai"
        handler.force_streaming_provider = "openai"
        handler.force_streaming_api_base_substrings = ["snowflakecomputing.com"]
        monkeypatch.setattr(litellm_handler, "LiteLLMAIHandler", lambda: handler)
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "OK"
        assert consumed == ["started", "finished"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("custom_llm_provider", "expected_model", "expected_provider"),
        [
            ("", "openrouter/openrouter/auto", ""),
            (" OpenRouter ", "openrouter/openrouter/auto", "openrouter"),
            (" OpenAI ", "openrouter/auto", "openai"),
        ],
    )
    async def test_openrouter_router_model_preserves_provider_routing(
        self, monkeypatch, restore_config_model, custom_llm_provider, expected_model, expected_provider
    ):
        restore_config_model.set("CONFIG.MODEL", "openrouter/auto")
        restore_config_model.set("LITELLM.CUSTOM_LLM_PROVIDER", custom_llm_provider)

        called = {}

        async def fake_acompletion(**kwargs):
            called.update(kwargs)
            return {"choices": [{"message": {"content": "pong"}}]}

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await executor_mod.health_check()

        assert result == "OK"
        assert called.get("model") == expected_model
        if expected_provider:
            assert called.get("custom_llm_provider") == expected_provider
        else:
            assert "custom_llm_provider" not in called

    @pytest.mark.asyncio
    async def test_no_model_configured_returns_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "")

        async def should_not_be_called(**kwargs):
            raise AssertionError("acompletion must not run when no model is configured")

        monkeypatch.setattr(litellm, "acompletion", should_not_be_called)

        result = await executor_mod.health_check()
        assert result == "Unhealthy: no model configured"
