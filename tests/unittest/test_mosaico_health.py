"""HTTP /health route tests.

Uses Starlette TestClient against build_app(); monkeypatches health_check (the no-retry
behavior itself was proven in 2c). Verifies the route + 200/503 response shape.

Also exercises the REAL health_check() (no stub) to lock in Fix A: the removed
'stop'-param gate must NOT short-circuit /health for models that lack 'stop' (e.g. the
shipped gpt-5.x defaults), since PR-Agent's LiteLLMAIHandler never sends 'stop'."""
import litellm
import pytest
from starlette.testclient import TestClient

import pr_agent.mosaico.server as server_mod
from pr_agent.config_loader import get_settings
from pr_agent.mosaico.executor import health_check
from pr_agent.mosaico.server import build_app


def _client(monkeypatch, health_value):
    async def fake_health_check():
        return health_value

    # health_check is imported into server_mod's namespace and called by _HealthApp._health.
    monkeypatch.setattr(server_mod, "health_check", fake_health_check)
    return TestClient(build_app())


class TestHealthRoute:
    def test_healthy_returns_200(self, monkeypatch):
        client = _client(monkeypatch, "OK")
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_healthy"] is True
        assert body["status"] == "OK"

    def test_unhealthy_returns_503(self, monkeypatch):
        client = _client(monkeypatch, "Unhealthy: connection refused")
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["is_healthy"] is False
        assert "Unhealthy" in body["status"]
        assert "Unhealthy" in body["detail"]


# A model id whose litellm-reported supported params genuinely LACK 'stop' (verified
# under the pinned litellm). Under the OLD (removed) gate, health_check() short-circuited
# to "Unhealthy: LLM does not support 'stop' parameter" for exactly such models — so these
# tests would have failed before Fix A. They guard against the gate being reintroduced.
_MODEL_WITHOUT_STOP = "gpt-5.5-2026-04-23"


@pytest.fixture
def restore_config_model():
    """Snapshot/restore CONFIG.MODEL on the shared settings (no request scope here, so
    get_settings() resolves to global_settings). Mirrors the snapshot/restore convention
    in test_mosaico_isolation.py."""
    settings = get_settings()
    sentinel = object()
    before = settings.get("CONFIG.MODEL", sentinel)
    yield settings
    if before is sentinel:
        # Best-effort removal of a key we introduced; dynaconf has no public delete, so
        # blank it out rather than leak a fake model into sibling tests.
        settings.set("CONFIG.MODEL", "")
    else:
        settings.set("CONFIG.MODEL", before)


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
    async def test_no_model_configured_returns_unhealthy(
        self, monkeypatch, restore_config_model
    ):
        restore_config_model.set("CONFIG.MODEL", "")

        async def should_not_be_called(**kwargs):
            raise AssertionError("acompletion must not run when no model is configured")

        monkeypatch.setattr(litellm, "acompletion", should_not_be_called)

        result = await health_check()
        assert result == "Unhealthy: no model configured"
