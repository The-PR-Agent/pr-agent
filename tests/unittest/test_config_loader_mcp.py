import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import _strip_json_comments, apply_mcp_server_config, load_mcp_server_config
from pr_agent.config_loader import load_repo_pyproject_settings


class TestMCPConfigLoader:
    def test_strip_json_comments_preserves_strings(self):
        content = (
            '{\n'
            '  // comment\n'
            '  "url": "https://example.com//path",\n'
            '  /* block */\n'
            '  "key": "value"\n'
            '}'
        )
        stripped = _strip_json_comments(content)
        data = json.loads(stripped)
        assert data == {"url": "https://example.com//path", "key": "value"}

    def test_load_mcp_server_config_supports_vscode_schema(self, tmp_path):
        config_path = tmp_path / "mcp.json"
        config_path.write_text(
            '{\n'
            '  // VS Code schema\n'
            '  "servers": {\n'
            '    "redmine": {"type": "stdio", "command": "podman"}\n'
            '  }\n'
            '}',
            encoding="utf-8",
        )
        config_data = load_mcp_server_config(config_path)
        assert config_data == {
            "servers": {
                "redmine": {"type": "stdio", "command": "podman"},
            }
        }

    def test_load_mcp_server_config_supports_trailing_commas(self, tmp_path):
        config_path = tmp_path / "mcp-trailing-commas.jsonc"
        config_path.write_text(
            "{\n"
            '  "servers": {\n'
            '    "redmine": {"type": "stdio", "command": "podman",},\n'
            "  },\n"
            "}\n",
            encoding="utf-8",
        )

        config_data = load_mcp_server_config(config_path)

        assert config_data == {
            "servers": {
                "redmine": {"type": "stdio", "command": "podman"},
            }
        }

    def test_load_mcp_server_config_supports_claude_schema(self, tmp_path):
        config_path = tmp_path / ".mcp.json"
        config_path.write_text(
            '{"mcpServers": {"sourcebot": {"type": "http", "url": "https://example.com/mcp"}}}',
            encoding="utf-8",
        )
        config_data = load_mcp_server_config(config_path)
        assert config_data == {
            "servers": {
                "sourcebot": {"type": "http", "url": "https://example.com/mcp"},
            }
        }

    def test_load_mcp_server_config_supports_aws_knowledge_schema(self, tmp_path):
        config_path = tmp_path / "aws-knowledge.json"
        config_path.write_text(
            '{"servers": {"AWS Knowledge": {"url": "https://knowledge-mcp.global.api.aws", "type": "http"}}}',
            encoding="utf-8",
        )
        config_data = load_mcp_server_config(config_path)
        assert config_data == {
            "servers": {
                "AWS Knowledge": {"url": "https://knowledge-mcp.global.api.aws", "type": "http"},
            }
        }

    def test_load_mcp_server_config_raises_on_missing_servers(self, tmp_path):
        config_path = tmp_path / "bad.json"
        config_path.write_text('{"other": {}}', encoding="utf-8")
        with pytest.raises(ValueError, match="must define either"):
            load_mcp_server_config(config_path)

    def test_load_mcp_server_config_raises_on_invalid_json(self, tmp_path):
        config_path = tmp_path / "invalid.json"
        config_path.write_text('{"servers":', encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid MCP config JSON"):
            load_mcp_server_config(config_path)

    def test_load_mcp_server_config_raises_on_missing_file(self, tmp_path):
        config_path = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            load_mcp_server_config(config_path)

    def test_apply_mcp_server_config_uses_env_override(self, tmp_path):
        config_path = tmp_path / "override.json"
        config_path.write_text(
            '{"servers": {"knowledge": {"type": "http", "url": "https://kb/mcp"}}}',
            encoding="utf-8",
        )
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            False if key == "MCP.FAIL_ON_INVALID_CONFIG" else default
        )
        with patch.dict("os.environ", {"MCP_CONFIG_PATH": str(config_path)}, clear=False), \
             patch("pr_agent.config_loader.get_settings", return_value=settings):
            apply_mcp_server_config()
        settings.set.assert_any_call(
            "MCP.SERVERS",
            {"knowledge": {"type": "http", "url": "https://kb/mcp"}},
            merge=False,
        )
        settings.set.assert_any_call("MCP.ACTIVE_CONFIG_PATH", str(config_path), merge=False)

    def test_apply_mcp_server_config_raises_when_configured(self, tmp_path):
        config_path = tmp_path / "invalid.json"
        config_path.write_text('{"servers":', encoding="utf-8")
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            True if key == "MCP.FAIL_ON_INVALID_CONFIG" else default
        )
        with patch.dict("os.environ", {"MCP_CONFIG_PATH": str(config_path)}, clear=False), \
             patch("pr_agent.config_loader.get_settings", return_value=settings):
            with pytest.raises(ValueError, match="Invalid MCP config JSON"):
                apply_mcp_server_config()

    def test_apply_mcp_server_config_skips_when_no_file(self, tmp_path):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            str(tmp_path / "nonexistent.json") if key == "MCP.CONFIG_PATH" else default
        )
        with patch("pr_agent.config_loader.get_settings", return_value=settings):
            apply_mcp_server_config()  # must not raise
        settings.set.assert_not_called()

    def test_apply_mcp_server_config_handles_exists_oserror(self):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            False if key == "MCP.FAIL_ON_INVALID_CONFIG" else default
        )

        with patch("pr_agent.config_loader.get_settings", return_value=settings), patch(
            "pathlib.Path.exists", side_effect=PermissionError("denied")
        ):
            apply_mcp_server_config()

        settings.set.assert_not_called()

    def test_apply_mcp_server_config_raises_on_exists_oserror_when_configured(self):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            True if key == "MCP.FAIL_ON_INVALID_CONFIG" else default
        )

        with patch("pr_agent.config_loader.get_settings", return_value=settings), patch(
            "pathlib.Path.exists", side_effect=PermissionError("denied")
        ):
            with pytest.raises(PermissionError, match="denied"):
                apply_mcp_server_config()

    def test_load_repo_pyproject_settings_preserves_trusted_mcp_settings(self, tmp_path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(
            "[tool.pr-agent.mcp]\n"
            "enabled = true\n"
            'config_path = "/tmp/untrusted.json"\n',
            encoding="utf-8",
        )
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            {"ENABLED": False, "CONFIG_PATH": "/etc/pr-agent/mcp.json", "RESOLVE_ENV_VARS": True}
            if key == "MCP"
            else default
        )

        load_repo_pyproject_settings(pyproject_path=pyproject_path, settings=settings)

        settings.load_file.assert_called_once_with(pyproject_path, env="tool.pr-agent")
        settings.set.assert_called_once_with(
            "MCP",
            {"ENABLED": False, "CONFIG_PATH": "/etc/pr-agent/mcp.json", "RESOLVE_ENV_VARS": True},
            merge=False,
        )
