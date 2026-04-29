from unittest.mock import MagicMock, patch

import pytest

from pr_agent.mcp.integration import maybe_chat_completion_with_mcp


class FakeHandler:
    def __init__(self):
        self.chat_calls = []
        self.tool_calls = []

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        self.chat_calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "temperature": temperature,
                "img_path": img_path,
            }
        )
        return "plain response", "completed"

    async def chat_completion_with_tools(
        self,
        model,
        system,
        user,
        tools,
        tool_executor,
        temperature=0.2,
        img_path=None,
    ):
        self.tool_calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "tools": tools,
                "temperature": temperature,
                "img_path": img_path,
            }
        )
        return "tool response", "completed"


class FakeRuntime:
    def __init__(self, enabled, tools=None):
        self.enabled = enabled
        self.tools = tools or []
        self.executor_created = False
        self.disconnected = False
        self.allowed_tool_names = None

    def build_tool_schemas(self, **kwargs):
        self.build_kwargs = kwargs
        return self.tools

    def create_tool_executor(self, allowed_tool_names=None):
        self.executor_created = True
        self.allowed_tool_names = allowed_tool_names

        async def executor(tool_name, arguments=None):
            return {"tool": tool_name, "arguments": arguments or {}}

        return executor

    def disconnect_all(self):
        self.disconnected = True


class TestMCPIntegrationHelper:
    @pytest.mark.asyncio
    async def test_falls_back_when_runtime_disabled(self):
        handler = FakeHandler()
        runtime = FakeRuntime(enabled=False)
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: default

        with patch("pr_agent.mcp.integration.MCPRuntime", return_value=runtime), patch(
            "pr_agent.mcp.integration.get_settings", return_value=settings
        ):
            response, finish_reason = await maybe_chat_completion_with_mcp(
                handler,
                model="gpt-5.4",
                system="system prompt",
                user="user prompt",
            )

        assert response == "plain response"
        assert finish_reason == "completed"
        assert len(handler.chat_calls) == 1
        assert not handler.tool_calls
        assert runtime.disconnected

    @pytest.mark.asyncio
    async def test_uses_tool_orchestration_when_enabled(self):
        handler = FakeHandler()
        runtime = FakeRuntime(
            enabled=True,
            tools=[{"type": "function", "function": {"name": "alpha.echo", "parameters": {}}}],
        )
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: {
            "MCP.MAX_TOOL_CATALOG_TOOLS": 2,
            "MCP.MAX_TOOL_CATALOG_SCHEMA_CHARS": 1000,
            "MCP.ENABLED_SERVERS": None,
        }.get(key, default)

        with patch("pr_agent.mcp.integration.MCPRuntime", return_value=runtime), patch(
            "pr_agent.mcp.integration.get_settings", return_value=settings
        ):
            response, finish_reason = await maybe_chat_completion_with_mcp(
                handler,
                model="gpt-5.4",
                system="system prompt",
                user="user prompt",
                command_name="review",
            )

        assert response == "tool response"
        assert finish_reason == "completed"
        assert runtime.executor_created
        assert runtime.disconnected
        assert len(handler.tool_calls) == 1
        assert "Command context: review" in handler.tool_calls[0]["system"]
        assert runtime.allowed_tool_names == {"alpha.echo"}
