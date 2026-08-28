import os
from unittest import mock

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")
import pr_agent.servers.gitlab_webhook as gitlab_webhook


def test_start_uses_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "4567")

    with mock.patch.object(gitlab_webhook.uvicorn, "run") as mock_run:
        gitlab_webhook.start()

    _, kwargs = mock_run.call_args
    assert kwargs["port"] == 4567
    assert kwargs["host"] == "0.0.0.0"


def test_start_invalid_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-number")

    with mock.patch.object(gitlab_webhook.uvicorn, "run") as mock_run:
        gitlab_webhook.start()

    _, kwargs = mock_run.call_args
    assert kwargs["port"] == 3000


def test_start_default_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    with mock.patch.object(gitlab_webhook.uvicorn, "run") as mock_run:
        gitlab_webhook.start()

    _, kwargs = mock_run.call_args
    assert kwargs["port"] == 3000


def test_start_invalid_port_range(monkeypatch):
    monkeypatch.setenv("PORT", "70000")

    with mock.patch.object(gitlab_webhook.uvicorn, "run") as mock_run:
        gitlab_webhook.start()

    _, kwargs = mock_run.call_args
    assert kwargs["port"] == 3000
