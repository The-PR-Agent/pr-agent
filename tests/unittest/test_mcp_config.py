import pytest

import pr_agent.tools.pr_reviewer as pr_reviewer
from pr_agent.tools.pr_reviewer import PRReviewer


def _settings(mcp_config):
    return type("Settings", (), {
        "get": lambda self, key, default=None: mcp_config if key == "mcp" else default,
    })()


def test_mcp_config_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(None))

    assert PRReviewer._get_mcp_config(object()) is None


def test_mcp_config_requires_explicit_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "command": "node",
        "args": ["server.js"],
    }))

    assert PRReviewer._get_mcp_config(object()) is None


def test_mcp_config_returns_command_and_args_when_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "command": "node",
        "args": ["server.js"],
    }))

    assert PRReviewer._get_mcp_config(object()) == {"command": "node", "args": ["server.js"]}


def test_mcp_config_rejects_missing_command(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "args": ["server.js"],
    }))

    with pytest.raises(ValueError, match="mcp.command"):
        PRReviewer._get_mcp_config(object())


def test_mcp_config_rejects_non_list_args(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "command": "node",
        "args": "server.js",
    }))

    with pytest.raises(ValueError, match="mcp.args"):
        PRReviewer._get_mcp_config(object())
