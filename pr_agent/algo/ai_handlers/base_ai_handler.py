import inspect
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

from pr_agent.mcp.runtime import MCPRuntimeError


class BaseAiHandler(ABC):
    """
    This class defines the interface for an AI handler to be used by the PR Agents.
    """

    @abstractmethod
    def __init__(self):
        pass

    _logger = logging.getLogger(__name__)

    @property
    @abstractmethod
    def deployment_id(self):
        pass

    @abstractmethod
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        """
        This method should be implemented to return a chat completion from the AI model.
        Args:
            model (str): the name of the model to use for the chat completion
            system (str): the system message string to use for the chat completion
            user (str): the user message string to use for the chat completion
            temperature (float): the temperature to use for the chat completion
        """
        pass

    async def chat_completion_with_tools(
        self,
        model: str,
        system: str,
        user: str,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_executor: Optional[Callable[[str, dict[str, Any]], Any | Awaitable[Any]]] = None,
        temperature: float = 0.2,
        img_path: str = None,
        max_tool_turns: int = 4,
        max_tool_output_chars: int = 12000,
    ):
        """
        Run a structured tool-calling loop on top of plain chat completion.

        The model is instructed to emit JSON tool requests in the form:
        {"type": "tool_call", "tool": "server.tool", "arguments": {...}}
        and to finish with:
        {"type": "final", "content": "..."}

        max_tool_output_chars is applied per tool call, not across all tool calls.
        """
        if not tools or tool_executor is None:
            return await self.chat_completion(model, system, user, temperature=temperature, img_path=img_path)

        allowed_tool_names = self._extract_allowed_tool_names(tools)
        tool_call_example = json.dumps(
            {
                "type": "tool_call",
                "tool": "server.tool",
                "arguments": {"param": "value"},
            },
            separators=(",", ":"),
        )
        final_response_example = json.dumps(
            {"type": "final", "content": "..."},
            separators=(",", ":"),
        )

        tool_catalog_text = json.dumps(tools, indent=2, sort_keys=True)
        structured_system = (
            f"{system}\n\n"
            f"Available MCP tools (JSON schema):\n{tool_catalog_text}\n\n"
            "Always inspect the available tools first and use them before responding "
            "whenever they can help answer the user's request.\n"
            "When you need a tool, respond with ONLY a JSON object exactly in this shape:\n"
            f"{tool_call_example}\n"
            "Do not include a final answer in the same message as a tool call.\n"
            "When you are finished, respond with ONLY a JSON object exactly in this shape:\n"
            f"{final_response_example}\n"
            "Do not wrap the JSON in markdown fences."
        )

        conversation_history = [user]
        remaining_turns = max_tool_turns
        current_img_path = img_path

        while True:
            current_user = "\n\n".join(conversation_history)
            response_text, finish_reason = await self.chat_completion(
                model=model,
                system=structured_system,
                user=current_user,
                temperature=temperature,
                img_path=current_img_path,
            )
            current_img_path = None

            parsed_response = self._parse_tool_or_final_response(response_text)
            if parsed_response is None:
                return response_text, finish_reason

            response_type = parsed_response.get("type", "final")
            if response_type == "final":
                return str(parsed_response.get("content", "")), finish_reason

            if response_type != "tool_call":
                return response_text, finish_reason

            if remaining_turns <= 0:
                self._logger.warning("MCP tool orchestration exceeded the configured turn budget")
                return response_text, finish_reason

            tool_name = str(parsed_response.get("tool", "")).strip()
            arguments = parsed_response.get("arguments") or {}
            if not tool_name:
                self._logger.warning("MCP tool orchestration returned an empty tool name; aborting tool loop")
                return response_text, finish_reason
            if not isinstance(arguments, dict):
                self._logger.warning("MCP tool orchestration arguments must be a JSON object; aborting tool loop")
                return response_text, finish_reason

            if tool_name not in allowed_tool_names:
                self._logger.warning("MCP tool '%s' was not in the advertised tool catalog; skipping", tool_name)
                tool_result = f"Tool not available: {tool_name}"
            else:
                try:
                    tool_result = tool_executor(tool_name, arguments)
                    if inspect.isawaitable(tool_result):
                        tool_result = await tool_result
                except (MCPRuntimeError, TypeError, ValueError, OSError, KeyError) as exc:
                    self._logger.warning("MCP tool '%s' raised an exception: %s", tool_name, exc)
                    tool_result = f"Tool error: {exc}"

            tool_result_text = self._normalize_tool_result_text(
                tool_result,
                max_tool_output_chars=max_tool_output_chars,
                tool_name=tool_name,
            )
            conversation_history.append(f"Previous assistant tool request:\n{response_text}")
            conversation_history.append(f"Tool result for {tool_name}:\n{tool_result_text}")
            remaining_turns -= 1

    @classmethod
    def _normalize_tool_result_text(
        cls,
        tool_result: Any,
        max_tool_output_chars: int,
        tool_name: str = "<unknown>",
    ) -> str:
        if isinstance(tool_result, str):
            result_text = tool_result
        else:
            result_text = json.dumps(tool_result, indent=2, sort_keys=True, default=str)

        if len(result_text) > max_tool_output_chars:
            cls._logger.warning(
                "Tool output for '%s' exceeded per-tool max_tool_output_chars (%s > %s); truncating output",
                tool_name,
                len(result_text),
                max_tool_output_chars,
            )
            if max_tool_output_chars <= 0:
                return ""
            suffix = "\n[tool output truncated]"
            if max_tool_output_chars <= len(suffix):
                return suffix[:max_tool_output_chars]
            truncated_prefix_len = max(0, max_tool_output_chars - len(suffix))
            return result_text[:truncated_prefix_len] + suffix
        return result_text

    @staticmethod
    def _parse_tool_or_final_response(response_text: str) -> Optional[dict[str, Any]]:
        candidate = response_text.strip()
        if not candidate:
            return None

        for json_candidate in BaseAiHandler._iter_json_object_candidates(candidate):
            try:
                parsed = json.loads(json_candidate)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                response_type = parsed.get("type")
                if response_type in {"tool_call", "final"}:
                    return parsed

        return None

    @staticmethod
    def _iter_json_object_candidates(text: str) -> list[str]:
        candidates: list[str] = []
        depth = 0
        start_index: Optional[int] = None
        in_string = False
        is_escaped = False

        for index, char in enumerate(text):
            if in_string:
                if is_escaped:
                    is_escaped = False
                elif char == "\\":
                    is_escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                if depth == 0:
                    start_index = index
                depth += 1
                continue

            if char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start_index is not None:
                    candidates.append(text[start_index : index + 1])
                    start_index = None

        return candidates

    @staticmethod
    def _extract_allowed_tool_names(tools: list[dict[str, Any]]) -> set[str]:
        allowed: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                continue

            function_info = tool.get("function")
            if isinstance(function_info, dict):
                function_name = function_info.get("name")
                if isinstance(function_name, str) and function_name.strip():
                    allowed.add(function_name.strip())

            simple_name = tool.get("name")
            if isinstance(simple_name, str) and simple_name.strip():
                allowed.add(simple_name.strip())

        return allowed
