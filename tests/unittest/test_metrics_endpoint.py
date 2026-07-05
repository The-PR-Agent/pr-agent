import pytest

pytest.importorskip("prometheus_client")
pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pr_agent.config_loader import get_settings  # noqa: E402
from pr_agent.servers.metrics import metrics_enabled, setup_metrics  # noqa: E402


@pytest.fixture()
def metrics_setting():
    original = get_settings().get("monitoring.enable_metrics", False)
    yield
    get_settings().set("monitoring.enable_metrics", original)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    setup_metrics(app)
    return app


class TestMetricsDisabled:
    def test_disabled_by_default(self, metrics_setting):
        get_settings().set("monitoring.enable_metrics", False)
        assert metrics_enabled() is False

    def test_no_metrics_route_when_disabled(self, metrics_setting):
        get_settings().set("monitoring.enable_metrics", False)
        client = TestClient(build_app())
        assert client.get("/metrics").status_code == 404


class TestMetricsEnabled:
    def test_metrics_route_exposes_counters(self, metrics_setting):
        get_settings().set("monitoring.enable_metrics", True)
        client = TestClient(build_app())

        assert client.get("/ping").status_code == 200
        response = client.get("/metrics")
        assert response.status_code == 200
        body = response.text
        assert "pr_agent_http_requests_total" in body
        assert 'path="/ping"' in body
        assert "pr_agent_http_request_duration_seconds" in body

    def test_counter_increments_per_request(self, metrics_setting):
        get_settings().set("monitoring.enable_metrics", True)
        client = TestClient(build_app())

        def ping_count() -> float:
            for line in client.get("/metrics").text.splitlines():
                if line.startswith("pr_agent_http_requests_total") and '/ping' in line and 'status="200"' in line:
                    return float(line.rsplit(" ", 1)[1])
            return 0.0

        before = ping_count()
        client.get("/ping")
        client.get("/ping")
        assert ping_count() == before + 2
