from unittest.mock import patch

from pr_agent.mcp.runtime import MCPRuntime, MCPToolDefinition


class TestMCPToolDiscovery:
    def test_build_tool_schemas_filters_by_server_and_budget(self):
        runtime = MCPRuntime(servers_config={"alpha": {}, "beta": {}})

        tools = [
            MCPToolDefinition("alpha", "tool_a", "desc a", {"type": "object"}),
            MCPToolDefinition("beta", "tool_b", "desc b", {"type": "object"}),
            MCPToolDefinition("beta", "tool_c", "desc c", {"type": "object"}),
        ]

        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(servers_config={"alpha": {}, "beta": {}})

        with patch.object(runtime, "list_all_tools", return_value=tools):
            schemas = runtime.build_tool_schemas(server_names=["beta"], max_tools=1, include_server_prefix=True)

        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "beta.tool_b"

    def test_build_tool_schemas_respects_character_budget(self):
        with patch("pr_agent.mcp.runtime.get_settings", return_value={"MCP": {"ENABLED": True}}):
            runtime = MCPRuntime(servers_config={"alpha": {}})

        tools = [
            MCPToolDefinition("alpha", "tool_a", "x" * 50, {"type": "object"}),
            MCPToolDefinition("alpha", "tool_b", "y" * 50, {"type": "object"}),
        ]

        with patch.object(runtime, "list_all_tools", return_value=tools):
            schemas = runtime.build_tool_schemas(max_schema_chars=250)

        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "alpha.tool_a"
