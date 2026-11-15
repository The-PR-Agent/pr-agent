from unittest import mock

import pytest

import pr_agent.servers.gerrit_server as gerrit_server
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.fixture(autouse=True)
def restore_start_settings():
    # start() sets the provider and CLI mode on the global settings.
    snapshot = snapshot_settings(["config.git_provider", "config.cli_mode"])
    yield
    restore_settings(snapshot)


def test_start_uses_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "4567")

    with mock.patch.object(gerrit_server.uvicorn, "run") as mock_run:
        gerrit_server.start()

    assert mock_run.call_args.kwargs["port"] == 4567


def test_start_default_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    with mock.patch.object(gerrit_server.uvicorn, "run") as mock_run:
        gerrit_server.start()

    assert mock_run.call_args.kwargs["port"] == 3000
