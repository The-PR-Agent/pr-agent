"""HTTP /health route tests.

Exercise build_app() directly through an in-process ASGI transport and monkeypatch
health_check (the no-retry behavior itself was proven in 2c). Verify the route and
200/503 response shape without relying on Starlette's thread-backed TestClient.

Also exercises the REAL health_check() (no stub) to lock in Fix A: the removed
'stop'-param gate must NOT short-circuit /health for models that lack 'stop' (e.g. the
shipped gpt-5.x defaults), since PR-Agent's LiteLLMAIHandler never sends 'stop'."""
import httpx
import litellm
import pytest

import pr_agent.mosaico.server as server_mod
from pr_agent.config_loader import get_settings
from pr_agent.mosaico.executor import health_check
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
_MODEL_WITHOUT_STOP = "gpt-5.6"


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

        # health_check does `import litellm` then `await litellm.acompletion(...)`.
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result = await health_check()

        # Must NOT short-circuit on the missing 'stop' param; it reaches the live probe.
        assert result == "OK"
        assert called.get("model") == _MODEL_WITHOUT_STOP

    @pytest.mark.asyncio
    async def test_live_probe_failure_returns_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", _MODEL_WITHOUT_STOP)

        async def boom_acompletion(**kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(litellm, "acompletion", boom_acompletion)

        result = await health_check()
        assert result.startswith("Unhealthy:")
        assert "connection refused" in result

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

        result = await health_check()

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

        result = await health_check()
        assert result == "Unhealthy: no model configured"
