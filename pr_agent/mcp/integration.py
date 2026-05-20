import asyncio
import functools
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.mcp.runtime import MCPRuntime


def _get_tool_budget(setting_name: str, default_value: int) -> int:
    value = get_settings().get(setting_name, default_value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default_value


async def maybe_chat_completion_with_mcp(
    ai_handler,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.2,
    img_path: str = None,
    command_name: Optional[str] = None,
):
    runtime = MCPRuntime()
    try:
        if not runtime.enabled:
            return await ai_handler.chat_completion(
                model=model,
                system=system,
                user=user,
                temperature=temperature,
                img_path=img_path,
            )

        max_tools = _get_tool_budget("MCP.MAX_TOOL_CATALOG_TOOLS", 12)
        max_schema_chars = _get_tool_budget("MCP.MAX_TOOL_CATALOG_SCHEMA_CHARS", 12000)

        loop = asyncio.get_running_loop()
        tools = await loop.run_in_executor(
            None,
            functools.partial(
                runtime.build_tool_schemas,
                max_tools=max_tools,
                max_schema_chars=max_schema_chars,
                include_server_prefix=True,
            ),
        )
        if not tools:
            return await ai_handler.chat_completion(
                model=model,
                system=system,
                user=user,
                temperature=temperature,
                img_path=img_path,
            )

        if command_name:
            system = f"{system}\n\nCommand context: {command_name}"

        allowed_tool_names: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function_info = tool.get("function")
            if isinstance(function_info, dict):
                function_name = function_info.get("name")
                if isinstance(function_name, str) and function_name.strip():
                    allowed_tool_names.add(function_name.strip())

        return await ai_handler.chat_completion_with_tools(
            model=model,
            system=system,
            user=user,
            tools=tools,
            tool_executor=runtime.create_tool_executor(allowed_tool_names=allowed_tool_names),
            temperature=temperature,
            img_path=img_path,
        )
    finally:
        runtime.disconnect_all()
