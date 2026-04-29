import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from pr_agent.mcp.runtime import (
    MCPHttpClient,
    MCPRuntime,
    MCPRuntimeError,
    MCPStdioClient,
    MCPStreamableHttpClient,
    MCPToolDefinition,
)


class FakeClient:
    def __init__(self, name, tools=None):
        self.name = name
        self.tools = tools or []
        self.connected = False
        self.server_capabilities = {"tools": True}

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def list_tools(self):
        return self.tools

    def call_tool(self, tool_name, arguments=None):
        return {
            "name": tool_name,
            "arguments": arguments or {},
            "server": self.name,
        }


class FailingConnectClient(FakeClient):
    def __init__(self, name, tools=None):
        super().__init__(name, tools)
        self.closed = False

    def connect(self):
        raise MCPRuntimeError("connect failed")

    def close(self):
        self.closed = True


class TestMCPRuntime:
    def test_runtime_uses_settings_when_not_provided(self):
        with patch(
            "pr_agent.mcp.runtime.get_settings",
            return_value={
                "MCP.SERVERS": {"srv": {"type": "http", "url": "https://example.com/mcp"}},
                "MCP": {"ENABLED": True},
            },
        ):
            runtime = MCPRuntime()
            assert runtime.configured_server_names == ["srv"]
            assert runtime.enabled

    def test_build_client_from_type(self):
        runtime = MCPRuntime(servers_config={"s1": {"type": "stdio", "command": "echo"}})
        client = runtime._build_client("s1", {"type": "stdio", "command": "echo"})
        assert isinstance(client, MCPStdioClient)

        client = runtime._build_client("s2", {"type": "http", "url": "https://example.com/mcp"})
        assert isinstance(client, MCPHttpClient)

    def test_build_client_type_inferred(self):
        runtime = MCPRuntime(servers_config={})

        client = runtime._build_client("s1", {"command": "echo"})
        assert isinstance(client, MCPStdioClient)

        client = runtime._build_client("s2", {"url": "https://example.com/mcp"})
        assert isinstance(client, MCPHttpClient)

    def test_build_client_unsupported_transport(self):
        runtime = MCPRuntime(servers_config={})
        with pytest.raises(MCPRuntimeError, match="unsupported transport"):
            runtime._build_client("bad", {"type": "sse", "url": "https://example.com/sse"})

    def test_list_all_tools(self):
        alpha_tools = [MCPToolDefinition("alpha", "tool_a", "desc", {})]
        beta_tools = [MCPToolDefinition("beta", "tool_b", "desc", {})]
        fake_clients = {
            "alpha": FakeClient("alpha", alpha_tools),
            "beta": FakeClient("beta", beta_tools),
        }

        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(
                servers_config={
                    "alpha": {"type": "http", "url": "https://alpha.example.com/mcp"},
                    "beta": {"type": "http", "url": "https://beta.example.com/mcp"},
                }
            )

            with patch.object(runtime, "_build_client", side_effect=lambda name, cfg: fake_clients[name]):
                all_tools = runtime.list_all_tools()

            assert {tool.name for tool in all_tools} == {"tool_a", "tool_b"}
            assert runtime.get_status() == {
                "enabled": True,
                "configured_servers": ["alpha", "beta"],
                "connected_servers": ["alpha", "beta"],
            }

    def test_call_tool_connects_lazy(self):
        fake_client = FakeClient("alpha")

        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(
                servers_config={"alpha": {"type": "http", "url": "https://alpha.example.com/mcp"}}
            )

            with patch.object(runtime, "_build_client", return_value=fake_client):
                result = runtime.call_tool("alpha", "sum", {"x": 1, "y": 2})

            assert result["name"] == "sum"
            assert result["arguments"] == {"x": 1, "y": 2}
            assert fake_client.connected

    def test_connect_server_closes_client_when_connect_fails(self):
        failing_client = FailingConnectClient("alpha")

        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(servers_config={"alpha": {"type": "http", "url": "https://alpha.example.com/mcp"}})

            with patch.object(runtime, "_build_client", return_value=failing_client):
                with pytest.raises(MCPRuntimeError, match="connect failed"):
                    runtime.connect_server("alpha")

            assert failing_client.closed
            assert "alpha" not in runtime._clients

    def test_disconnect_all(self):
        fake_clients = {
            "alpha": FakeClient("alpha"),
            "beta": FakeClient("beta"),
        }

        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(
                servers_config={
                    "alpha": {"type": "http", "url": "https://alpha.example.com/mcp"},
                    "beta": {"type": "http", "url": "https://beta.example.com/mcp"},
                }
            )

            with patch.object(runtime, "_build_client", side_effect=lambda name, cfg: fake_clients[name]):
                runtime.connect_all()
                runtime.disconnect_all()

            assert not fake_clients["alpha"].connected
            assert not fake_clients["beta"].connected

    def test_resolve_env_vars_disabled_preserves_placeholders(self):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "MCP": {"ENABLED": True},
            "MCP.RESOLVE_ENV_VARS": False,
        }.get(key, default)

        with patch("pr_agent.mcp.runtime.get_settings", return_value=settings), patch.dict(
            "os.environ", {"MCP_TEST_ENV": "expanded"}, clear=False
        ):
            runtime = MCPRuntime(servers_config={})
            assert runtime._resolve_config_values("$MCP_TEST_ENV/path") == "$MCP_TEST_ENV/path"

    def test_resolve_env_vars_enabled_expands_placeholders(self):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "MCP": {"ENABLED": True},
            "MCP.RESOLVE_ENV_VARS": True,
        }.get(key, default)

        with patch("pr_agent.mcp.runtime.get_settings", return_value=settings), patch.dict(
            "os.environ", {"MCP_TEST_ENV": "expanded"}, clear=False
        ):
            runtime = MCPRuntime(servers_config={})
            assert runtime._resolve_config_values("$MCP_TEST_ENV/path") == "expanded/path"

    @pytest.mark.asyncio
    async def test_tool_executor_rejects_non_allowlisted_tool(self):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "MCP": {"ENABLED": True},
            "MCP.RESOLVE_ENV_VARS": True,
        }.get(key, default)
        fake_client = FakeClient("alpha")

        with patch("pr_agent.mcp.runtime.get_settings", return_value=settings):
            runtime = MCPRuntime(
                servers_config={"alpha": {"type": "http", "url": "https://alpha.example.com/mcp"}}
            )

            with patch.object(runtime, "_build_client", return_value=fake_client):
                executor = runtime.create_tool_executor(allowed_tool_names={"alpha.allowed"})
                with pytest.raises(MCPRuntimeError, match="Tool not available"):
                    await executor("alpha.blocked", {})


def _make_fake_response(body: dict, content_type: str = "application/json", headers: dict = None):
    """Build a fake requests.Response for patching."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.headers = {"Content-Type": content_type, **(headers or {})}
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def _make_sse_response(events: list[dict], extra_headers: dict = None):
    """Build a fake SSE requests.Response."""
    lines = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")  # SSE event separator

    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.headers = {"Content-Type": "text/event-stream", **(extra_headers or {})}
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = iter(lines)
    return resp


class TestMCPStreamableHttpClient:
    def _client(self, url="https://mcp.example.com/mcp", **extra):
        return MCPStreamableHttpClient("TestServer", {"url": url, **extra})

    def test_missing_url_raises(self):
        with pytest.raises(MCPRuntimeError, match="missing 'url'"):
            MCPStreamableHttpClient("TestServer", {})

    def test_connect_plain_json_response(self):
        client = self._client()
        init_resp = _make_fake_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {"tools": {}}}},
        )
        notif_resp = _make_fake_response({})

        with patch.object(client._session, "post", side_effect=[init_resp, notif_resp]) as mock_post:
            client.connect()

        assert client.server_capabilities == {"tools": {}}
        # initialize + notifications/initialized
        assert mock_post.call_count == 2
        # Accept header must negotiate both formats
        session_headers = dict(client._session.headers)
        assert "text/event-stream" in session_headers.get("Accept", "")

    def test_connect_captures_session_id(self):
        client = self._client()
        init_resp = _make_fake_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
            headers={"Mcp-Session-Id": "abc123"},
        )
        notif_resp = _make_fake_response({})

        with patch.object(client._session, "post", side_effect=[init_resp, notif_resp]):
            client.connect()

        assert client._session_id == "abc123"

    def test_send_request_includes_session_id_header(self):
        client = self._client()
        client._session_id = "sess-42"

        resp = _make_fake_response({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client._send_request("tools/list", {})

        extra_headers = mock_post.call_args.kwargs.get("headers", {})
        assert extra_headers.get("Mcp-Session-Id") == "sess-42"

    def test_list_tools_plain_json(self):
        client = self._client()
        tools_resp = _make_fake_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "current_time", "description": "Get current time", "inputSchema": {"type": "object"}}
                    ]
                },
            }
        )
        with patch.object(client._session, "post", return_value=tools_resp):
            tools = client.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "current_time"
        assert tools[0].server_name == "TestServer"

    def test_list_tools_sse_response(self):
        client = self._client()
        sse_resp = _make_sse_response(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [{"name": "get_time", "description": "Time", "inputSchema": {"type": "object"}}]
                    },
                }
            ]
        )
        with patch.object(client._session, "post", return_value=sse_resp):
            tools = client.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "get_time"

    def test_sse_stream_with_notification_before_result(self):
        """Notifications (no 'id') in the SSE stream must be skipped."""
        client = self._client()
        notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        result_event = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        sse_resp = _make_sse_response([notification, result_event])

        with patch.object(client._session, "post", return_value=sse_resp):
            result = client._send_request("tools/list", {})

        assert result == {"tools": []}

    def test_sse_stream_exhausted_without_match_raises(self):
        client = self._client()
        # Only a notification, never a matching id
        sse_resp = _make_sse_response([{"jsonrpc": "2.0", "method": "ping", "params": {}}])

        with patch.object(client._session, "post", return_value=sse_resp):
            with pytest.raises(MCPRuntimeError, match="SSE stream ended without a matching response"):
                client._send_request("tools/list", {})

    def test_server_error_in_sse_raises(self):
        client = self._client()
        sse_resp = _make_sse_response(
            [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad request"}}]
        )
        with patch.object(client._session, "post", return_value=sse_resp):
            with pytest.raises(MCPRuntimeError, match="bad request"):
                client._send_request("tools/list", {})

    def test_http_error_raises(self):
        client = self._client()
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.raise_for_status.side_effect = requests.RequestException("406 Not Acceptable")

        with patch.object(client._session, "post", return_value=bad_resp):
            with pytest.raises(MCPRuntimeError, match="406 Not Acceptable"):
                client._send_request("initialize", {})

    def test_close_clears_session_id(self):
        client = self._client()
        client._session_id = "will-be-cleared"
        with patch.object(client._session, "close"):
            client.close()
        assert client._session_id is None


class TestBuildClientStreamableHttp:
    def test_build_client_streamable_http_type(self):
        runtime = MCPRuntime(servers_config={})
        client = runtime._build_client(
            "Sourcebot", {"type": "streamable_http", "url": "http://sourcebot.example.com/api/mcp"}
        )
        assert isinstance(client, MCPStreamableHttpClient)


class TestMCPStdioClientConfigValidation:
    def test_invalid_timeout_raises_mcp_runtime_error(self):
        with pytest.raises(MCPRuntimeError, match="timeout must be a number"):
            MCPStdioClient("TestServer", {"command": "echo", "timeout": "slow"})

    def test_invalid_args_element_raises_mcp_runtime_error(self):
        client = MCPStdioClient("TestServer", {"command": "echo", "timeout": 30})

        with pytest.raises(MCPRuntimeError, match="args must contain only strings or path-like values"):
            client._normalize_args(["ok", 123])
