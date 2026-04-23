from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler


class FakeToolHandler(BaseAiHandler):
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def deployment_id(self):
        return None

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "temperature": temperature,
                "img_path": img_path,
            }
        )
        if not self._responses:
            raise AssertionError("No more fake responses available")
        return self._responses.pop(0), "completed"


class TestToolOrchestration:
    async def test_tool_loop_executes_and_returns_final_answer(self):
        handler = FakeToolHandler(
            [
                '{"type": "tool_call", "tool": "mcp.echo", "arguments": {"text": "hello"}}',
                '{"type": "final", "content": "done"}',
            ]
        )

        tool_calls = []

        async def executor(tool_name, arguments):
            tool_calls.append((tool_name, arguments))
            return {"output": arguments["text"].upper()}

        tools = [
            {
                "name": "mcp.echo",
                "description": "Echo input text",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
            tools=tools,
            tool_executor=executor,
            max_tool_turns=2,
        )

        assert response == "done"
        assert finish_reason == "completed"
        assert tool_calls == [("mcp.echo", {"text": "hello"})]
        assert len(handler.calls) == 2
        assert "Available MCP tools" in handler.calls[0]["system"]
        assert "Always inspect the available tools first" in handler.calls[0]["system"]
        assert "Tool result for mcp.echo" in handler.calls[1]["user"]

    async def test_tool_loop_ignores_trailing_final_after_tool_call(self):
        first_turn_response = "".join(
            [
                "Ask question and use the documented tool format.\n",
                "{\"type\":\"tool_call\",",
                "\"tool\":\"AWS Knowledge.aws___read_documentation\",",
                "\"arguments\":{\"requests\":[",
                "{\"url\":\"https://aws.amazon.com/about-aws/whats-new/\",\"max_length\":8000}",
                "]}}\n",
                "{\"type\":\"final\",\"content\":\"should be ignored in the first turn\"}",
            ]
        )
        handler = FakeToolHandler(
            [
                first_turn_response,
                '{"type": "final", "content": "done"}',
            ]
        )

        tool_calls = []

        async def executor(tool_name, arguments):
            tool_calls.append((tool_name, arguments))
            return {"output": "ok"}

        tools = [
            {
                "name": "AWS Knowledge.aws___read_documentation",
                "description": "Read AWS documentation",
                "inputSchema": {
                    "type": "object",
                    "properties": {"requests": {"type": "array"}},
                },
            }
        ]

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
            tools=tools,
            tool_executor=executor,
            max_tool_turns=2,
        )

        assert response == "done"
        assert finish_reason == "completed"
        assert tool_calls == [
            (
                "AWS Knowledge.aws___read_documentation",
                {"requests": [{"url": "https://aws.amazon.com/about-aws/whats-new/", "max_length": 8000}]},
            )
        ]
        assert len(handler.calls) == 2

    async def test_tool_loop_falls_back_without_tools(self):
        handler = FakeToolHandler(["plain response"])

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
        )

        assert response == "plain response"
        assert finish_reason == "completed"
        assert len(handler.calls) == 1

    async def test_tool_loop_blocks_non_advertised_tool_name(self):
        handler = FakeToolHandler(
            [
                '{"type": "tool_call", "tool": "mcp.hidden", "arguments": {}}',
                '{"type": "final", "content": "done"}',
            ]
        )

        tool_calls = []

        async def executor(tool_name, arguments):
            tool_calls.append((tool_name, arguments))
            return {"output": "should-not-run"}

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "mcp.allowed",
                    "description": "Allowed tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
            tools=tools,
            tool_executor=executor,
            max_tool_turns=2,
        )

        assert response == "done"
        assert finish_reason == "completed"
        assert tool_calls == []
        assert "Tool not available: mcp.hidden" in handler.calls[1]["user"]

    async def test_tool_loop_handles_expected_executor_error(self):
        handler = FakeToolHandler(
            [
                '{"type": "tool_call", "tool": "mcp.echo", "arguments": {}}',
                '{"type": "final", "content": "done"}',
            ]
        )

        async def executor(tool_name, arguments):
            raise ValueError("bad input")

        tools = [{"name": "mcp.echo", "description": "Echo", "inputSchema": {"type": "object"}}]

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
            tools=tools,
            tool_executor=executor,
            max_tool_turns=2,
        )

        assert response == "done"
        assert finish_reason == "completed"
        assert "Tool error: bad input" in handler.calls[1]["user"]

    async def test_tool_output_limit_is_per_tool_and_warns_when_truncated(self, caplog):
        handler = FakeToolHandler(
            [
                '{"type": "tool_call", "tool": "mcp.big_one", "arguments": {}}',
                '{"type": "tool_call", "tool": "mcp.big_two", "arguments": {}}',
                '{"type": "final", "content": "done"}',
            ]
        )

        tool_calls = []

        async def executor(tool_name, arguments):
            tool_calls.append((tool_name, arguments))
            return "x" * 80

        tools = [
            {
                "name": "mcp.big_one",
                "description": "Large output tool one",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "mcp.big_two",
                "description": "Large output tool two",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

        caplog.set_level("WARNING")

        response, finish_reason = await handler.chat_completion_with_tools(
            model="gpt-5.4",
            system="system prompt",
            user="user prompt",
            tools=tools,
            tool_executor=executor,
            max_tool_turns=3,
            max_tool_output_chars=40,
        )

        assert response == "done"
        assert finish_reason == "completed"
        assert tool_calls == [("mcp.big_one", {}), ("mcp.big_two", {})]
        assert "[tool output truncated]" in handler.calls[1]["user"]
        assert "[tool output truncated]" in handler.calls[2]["user"]

        warning_messages = [record.message for record in caplog.records if "max_tool_output_chars" in record.message]
        assert len(warning_messages) == 2
        assert any("mcp.big_one" in message for message in warning_messages)
        assert any("mcp.big_two" in message for message in warning_messages)

    def test_tool_output_limit_never_exceeds_tiny_cap(self):
        truncated = BaseAiHandler._normalize_tool_result_text(
            tool_result="x" * 200,
            max_tool_output_chars=5,
            tool_name="mcp.tiny_cap",
        )

        assert len(truncated) == 5
