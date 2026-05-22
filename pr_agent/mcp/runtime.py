import json
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import requests

from pr_agent.config_loader import get_settings


def _get_logger():
    try:
        from pr_agent.log import get_logger

        return get_logger()
    except ImportError:
        class DummyLogger:
            def debug(self, *args, **kwargs):
                pass

            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

        return DummyLogger()


class MCPRuntimeError(Exception):
    pass


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Safely parse a boolean setting, handling string 'false'/'true' correctly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    if value is None:
        return default
    return bool(value)


@dataclass(frozen=True)
class MCPToolDefinition:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai_tool(self, include_server_prefix: bool = True) -> dict[str, Any]:
        tool_name = f"{self.server_name}.{self.name}" if include_server_prefix else self.name
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class BaseMCPClient(ABC):
    def __init__(self, server_name: str, config: dict[str, Any]):
        self.server_name = server_name
        self.config = config
        self.server_capabilities: dict[str, Any] = {}

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def close(self):
        pass

    @abstractmethod
    def list_tools(self) -> list[MCPToolDefinition]:
        pass

    @abstractmethod
    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        pass


class MCPStdioClient(BaseMCPClient):
    def __init__(self, server_name: str, config: dict[str, Any]):
        super().__init__(server_name, config)
        self.process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self.timeout = self._parse_timeout(config.get("timeout", 30))

    def _parse_timeout(self, timeout_value: Any) -> float:
        try:
            return float(timeout_value)
        except (TypeError, ValueError) as exc:
            raise MCPRuntimeError(
                f"Stdio MCP server '{self.server_name}' timeout must be a number"
            ) from exc

    def _normalize_args(self, args: Any) -> list[str]:
        if not isinstance(args, list):
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' args must be a list")

        normalized_args: list[str] = []
        for arg in args:
            if isinstance(arg, os.PathLike):
                normalized_args.append(os.fspath(arg))
            elif isinstance(arg, str):
                normalized_args.append(arg)
            else:
                raise MCPRuntimeError(
                    f"Stdio MCP server '{self.server_name}' args must contain only strings or path-like values"
                )
        return normalized_args

    def connect(self):
        command = self.config.get("command")
        if not command:
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' is missing 'command'")

        args = self._normalize_args(self.config.get("args") or [])

        env = os.environ.copy()
        server_env = self.config.get("env") or {}
        if not isinstance(server_env, dict):
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' env must be an object")
        env.update({str(k): str(v) for k, v in server_env.items()})

        cwd = self.config.get("cwd")
        self.process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
        try:
            init_result = self._send_request(
                "initialize",
                {
                    "protocolVersion": self.config.get("protocol_version", "2024-11-05"),
                    "capabilities": self.config.get("client_capabilities", {}),
                    "clientInfo": self.config.get(
                        "client_info",
                        {"name": "pr-agent", "version": "mcp-runtime"},
                    ),
                },
            )
            self.server_capabilities = init_result.get("capabilities", {})
            self._send_notification("notifications/initialized", {})
        except (MCPRuntimeError, OSError, subprocess.SubprocessError):
            self._terminate_process()
            self.process = None
            raise

    def close(self):
        if not self.process:
            return
        self._terminate_process()
        self.process = None

    def list_tools(self) -> list[MCPToolDefinition]:
        result = self._send_request("tools/list", {})
        if not isinstance(result, dict):
            return []
        tools = result.get("tools") or []
        parsed: list[MCPToolDefinition] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            parsed.append(
                MCPToolDefinition(
                    server_name=self.server_name,
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                )
            )
        return parsed

    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._send_request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        )

    def _send_notification(self, method: str, params: dict[str, Any]):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_message(payload)

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self._write_message(payload)
        response = self._read_response(request_id)
        if "error" in response:
            raise MCPRuntimeError(
                f"MCP server '{self.server_name}' returned error for '{method}': {response['error']}"
            )
        return response.get("result", {})

    def _write_message(self, payload: dict[str, Any]):
        if not self.process or not self.process.stdin:
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' is not connected")

        encoded = json.dumps(payload).encode("utf-8")
        frame = f"Content-Length: {len(encoded)}\r\n\r\n".encode("utf-8") + encoded
        self.process.stdin.write(frame)
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = self._read_message_with_timeout()
            if not isinstance(message, dict):
                raise MCPRuntimeError(
                    f"MCP server '{self.server_name}' returned a non-object JSON-RPC message"
                )
            if message.get("id") == request_id:
                return message


    def _read_message_with_timeout(self) -> dict[str, Any]:
        response_holder: dict[str, Any] = {}
        error_holder: dict[str, Exception] = {}

        def reader():
            try:
                response_holder["message"] = self._read_message()
            except MCPRuntimeError as exc:  # noqa: BLE001
                error_holder["error"] = exc
            except (OSError, ValueError, json.JSONDecodeError) as exc:  # noqa: BLE001
                error_holder["error"] = exc

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        reader_thread.join(timeout=self.timeout)

        if reader_thread.is_alive():
            self._terminate_process()
            self.process = None
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' timed out waiting for a response")

        if "error" in error_holder:
            error = error_holder["error"]
            if isinstance(error, MCPRuntimeError):
                raise error
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' failed while reading response") from error

        return response_holder["message"]

    def _terminate_process(self):
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _read_message(self) -> dict[str, Any]:
        if not self.process or not self.process.stdout:
            raise MCPRuntimeError(f"Stdio MCP server '{self.server_name}' is not connected")

        headers: dict[str, str] = {}
        while True:
            line = self.process.stdout.readline()
            if line == b"":
                raise MCPRuntimeError(f"MCP server '{self.server_name}' closed stdout unexpectedly")
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()

        content_length_value = headers.get("content-length")
        if not content_length_value:
            raise MCPRuntimeError(f"MCP server '{self.server_name}' response missing Content-Length")

        try:
            content_length = int(content_length_value)
        except ValueError as exc:
            raise MCPRuntimeError(
                f"MCP server '{self.server_name}' response has an invalid Content-Length: {content_length_value}"
            ) from exc
        if content_length <= 0:
            raise MCPRuntimeError(
                f"MCP server '{self.server_name}' response has a non-positive Content-Length: {content_length}"
            )

        body = self.process.stdout.read(content_length)
        if not body or len(body) != content_length:
            raise MCPRuntimeError(
                f"MCP server '{self.server_name}' returned an incomplete response body "
                f"({len(body) if body else 0}/{content_length} bytes)"
            )

        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPRuntimeError(f"MCP server '{self.server_name}' response is not valid JSON") from exc


class MCPHttpClient(BaseMCPClient):
    def __init__(self, server_name: str, config: dict[str, Any]):
        super().__init__(server_name, config)
        self.url = config.get("url")
        if not self.url:
            raise MCPRuntimeError(f"HTTP MCP server '{self.server_name}' is missing 'url'")

        try:
            self.timeout = float(config.get("timeout", 30))
        except (TypeError, ValueError) as exc:
            raise MCPRuntimeError(
                f"HTTP MCP server '{self.server_name}' timeout must be a number"
            ) from exc
        self._request_id = 0
        self._session = requests.Session()
        headers = config.get("headers") or {}
        if isinstance(headers, dict):
            self._session.headers.update({str(k): str(v) for k, v in headers.items()})

    def connect(self):
        init_result = self._send_request(
            "initialize",
            {
                "protocolVersion": self.config.get("protocol_version", "2024-11-05"),
                "capabilities": self.config.get("client_capabilities", {}),
                "clientInfo": self.config.get(
                    "client_info",
                    {"name": "pr-agent", "version": "mcp-runtime"},
                ),
            },
        )
        self.server_capabilities = init_result.get("capabilities", {})
        self._send_notification("notifications/initialized", {})

    def close(self):
        self._session.close()

    def list_tools(self) -> list[MCPToolDefinition]:
        result = self._send_request("tools/list", {})
        if not isinstance(result, dict):
            return []
        tools = result.get("tools") or []
        parsed: list[MCPToolDefinition] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            parsed.append(
                MCPToolDefinition(
                    server_name=self.server_name,
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                )
            )
        return parsed

    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._send_request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        )

    def _send_notification(self, method: str, params: dict[str, Any]):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            self._session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MCPRuntimeError(f"MCP HTTP notification failed for '{self.server_name}': {exc}") from exc

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        try:
            response = self._session.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise MCPRuntimeError(f"MCP HTTP request failed for '{self.server_name}': {exc}") from exc
        except ValueError as exc:
            raise MCPRuntimeError(f"MCP HTTP response is not valid JSON for '{self.server_name}'") from exc

        if not isinstance(body, dict):
            raise MCPRuntimeError(f"MCP HTTP response must be a JSON object for '{self.server_name}'")

        if "error" in body:
            raise MCPRuntimeError(
                f"MCP HTTP server '{self.server_name}' returned error for '{method}': {body['error']}"
            )

        result = body.get("result", {})
        if not isinstance(result, dict):
            raise MCPRuntimeError(f"MCP HTTP result for '{self.server_name}' must be a JSON object")
        return result


class MCPStreamableHttpClient(BaseMCPClient):
    """MCP client for the Streamable HTTP transport (MCP spec 2025-03-26).

    Sends POST requests with ``Accept: application/json, text/event-stream`` and
    handles both plain JSON responses and Server-Sent Events (SSE) streams.
    Session continuity is maintained via the ``Mcp-Session-Id`` header.
    """

    def __init__(self, server_name: str, config: dict[str, Any]):
        super().__init__(server_name, config)
        self.url = config.get("url")
        if not self.url:
            raise MCPRuntimeError(f"Streamable HTTP MCP server '{self.server_name}' is missing 'url'")

        try:
            self.timeout = float(config.get("timeout", 30))
        except (TypeError, ValueError) as exc:
            raise MCPRuntimeError(
                f"Streamable HTTP MCP server '{self.server_name}' timeout must be a number"
            ) from exc
        self._request_id = 0
        self._session_id: Optional[str] = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
        )
        headers = config.get("headers") or {}
        if isinstance(headers, dict):
            self._session.headers.update({str(k): str(v) for k, v in headers.items()})

    def connect(self):
        init_result = self._send_request(
            "initialize",
            {
                "protocolVersion": self.config.get("protocol_version", "2024-11-05"),
                "capabilities": self.config.get("client_capabilities", {}),
                "clientInfo": self.config.get(
                    "client_info",
                    {"name": "pr-agent", "version": "mcp-runtime"},
                ),
            },
        )
        self.server_capabilities = init_result.get("capabilities", {})
        self._send_notification("notifications/initialized", {})

    def close(self):
        self._session_id = None
        self._session.close()

    def list_tools(self) -> list[MCPToolDefinition]:
        result = self._send_request("tools/list", {})
        if not isinstance(result, dict):
            return []
        tools = result.get("tools") or []
        parsed: list[MCPToolDefinition] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            parsed.append(
                MCPToolDefinition(
                    server_name=self.server_name,
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                )
            )
        return parsed

    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._send_request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        )

    def _build_extra_headers(self) -> dict[str, str]:
        if self._session_id:
            return {"Mcp-Session-Id": self._session_id}
        return {}

    def _send_notification(self, method: str, params: dict[str, Any]):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            self._session.post(
                self.url,
                json=payload,
                headers=self._build_extra_headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MCPRuntimeError(
                f"MCP streamable HTTP notification failed for '{self.server_name}': {exc}"
            ) from exc

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response: Optional["requests.Response"] = None
        try:
            response = self._session.post(
                self.url,
                json=payload,
                headers=self._build_extra_headers(),
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            # Capture session ID returned on the initialize response.
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id and not self._session_id:
                self._session_id = session_id

            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                return self._parse_sse_response(response, request_id, method)
            return self._parse_json_response(response, method)
        except requests.RequestException as exc:
            raise MCPRuntimeError(
                f"MCP streamable HTTP request failed for '{self.server_name}': {exc}"
            ) from exc
        finally:
            if response is not None:
                response.close()

    def _parse_json_response(self, response: "requests.Response", method: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise MCPRuntimeError(
                f"MCP streamable HTTP response is not valid JSON for '{self.server_name}'"
            ) from exc
        if not isinstance(body, dict):
            raise MCPRuntimeError(
                f"MCP streamable HTTP response must be a JSON object for '{self.server_name}'"
            )
        return self._extract_result(body, method)

    def _parse_sse_response(
        self, response: "requests.Response", request_id: int, method: str
    ) -> dict[str, Any]:
        """Read an SSE stream and return the JSON-RPC result that matches *request_id*."""
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data = raw_line[5:].lstrip(" ")
                if not data:
                    continue
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return self._extract_result(message, method)
        except requests.RequestException as exc:
            raise MCPRuntimeError(
                f"MCP streamable HTTP SSE stream error for '{self.server_name}': {exc}"
            ) from exc

        raise MCPRuntimeError(
            f"MCP streamable HTTP server '{self.server_name}' SSE stream ended without a matching "
            f"response for request id {request_id}"
        )

    def _extract_result(self, body: dict[str, Any], method: Optional[str]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise MCPRuntimeError(f"MCP streamable HTTP response must be a JSON object for '{self.server_name}'")
        if "error" in body:
            raise MCPRuntimeError(
                f"MCP streamable HTTP server '{self.server_name}' returned error"
                + (f" for '{method}'" if method else "")
                + f": {body['error']}"
            )
        result = body.get("result", {})
        if not isinstance(result, dict):
            raise MCPRuntimeError(f"MCP streamable HTTP result for '{self.server_name}' must be a JSON object")
        return result


class MCPRuntime:
    def __init__(self, servers_config: Optional[dict[str, Any]] = None):
        self._logger = _get_logger()
        self._resolve_env_vars = _parse_bool(get_settings().get("MCP.RESOLVE_ENV_VARS", True), default=True)
        if servers_config is None:
            servers_config = get_settings().get("MCP.SERVERS", {}) or {}

        if not isinstance(servers_config, dict):
            self._logger.warning("MCP.SERVERS is not an object; ignoring MCP server configuration")
            servers_config = {}

        self._servers_config = servers_config
        self._clients: dict[str, BaseMCPClient] = {}

    @property
    def configured_server_names(self) -> list[str]:
        return list(self._servers_config.keys())

    @property
    def enabled(self) -> bool:
        return _parse_bool(get_settings().get("MCP.ENABLED", False), default=False)

    @property
    def enabled_server_names(self) -> list[str]:
        return self.configured_server_names

    def connect_all(self):
        if not self.enabled:
            self._logger.debug("MCP runtime is disabled; skipping server connections")
            return
        for server_name in self._servers_config.keys():
            self.connect_server(server_name)

    def connect_server(self, server_name: str):
        if not self.enabled:
            raise MCPRuntimeError("MCP runtime is disabled")
        if server_name in self._clients:
            return

        server_config = self._servers_config.get(server_name)
        if not isinstance(server_config, dict):
            raise MCPRuntimeError(f"MCP server '{server_name}' config must be an object")

        client = self._build_client(server_name, self._resolve_config_values(server_config))
        try:
            client.connect()
        except (MCPRuntimeError, OSError, requests.RequestException, subprocess.SubprocessError):
            client.close()
            raise

        self._clients[server_name] = client
        self._logger.info(f"Connected MCP server '{server_name}'")

    def disconnect_all(self):
        for server_name in list(self._clients.keys()):
            self.disconnect_server(server_name)

    def disconnect_server(self, server_name: str):
        client = self._clients.pop(server_name, None)
        if client:
            client.close()
            self._logger.info(f"Disconnected MCP server '{server_name}'")

    def list_server_tools(self, server_name: str) -> list[MCPToolDefinition]:
        client = self._clients.get(server_name)
        if not client:
            self.connect_server(server_name)
            client = self._clients[server_name]
        try:
            return client.list_tools()
        except (MCPRuntimeError, OSError) as exc:
            # Evict the stale client so the next call reconnects rather than reusing a dead connection.
            self._clients.pop(server_name, None)
            client.close()
            raise MCPRuntimeError(
                f"MCP server '{server_name}' failed during tool listing; evicted from cache"
            ) from exc

    def list_all_tools(self) -> list[MCPToolDefinition]:
        if not self.enabled:
            return []

        tools: list[MCPToolDefinition] = []
        for server_name in self._servers_config.keys():
            try:
                tools.extend(self.list_server_tools(server_name))
            except (MCPRuntimeError, OSError, ValueError, TypeError) as exc:
                self._logger.warning(f"Failed to list tools for MCP server '{server_name}': {exc}")
        return tools

    def build_tool_schemas(
        self,
        server_names: Optional[list[str]] = None,
        max_tools: Optional[int] = None,
        max_schema_chars: Optional[int] = None,
        include_server_prefix: bool = True,
    ) -> list[dict[str, Any]]:
        tool_definitions = self.list_all_tools()
        if server_names:
            allowed_servers = set(server_names)
            tool_definitions = [tool for tool in tool_definitions if tool.server_name in allowed_servers]

        schemas: list[dict[str, Any]] = []
        consumed_chars = 0
        for tool_definition in tool_definitions:
            schema = tool_definition.to_openai_tool(include_server_prefix=include_server_prefix)
            schema_text = json.dumps(schema, sort_keys=True)
            if max_schema_chars is not None and consumed_chars + len(schema_text) > max_schema_chars:
                self._logger.debug(
                    f"Skipping MCP tool '{tool_definition.server_name}.{tool_definition.name}': "
                    f"would exceed schema budget ({consumed_chars + len(schema_text)} > {max_schema_chars})"
                )
                continue
            schemas.append(schema)
            consumed_chars += len(schema_text)
            if max_tools is not None and len(schemas) >= max_tools:
                break

        return schemas

    def create_tool_executor(self, allowed_tool_names: Optional[set[str]] = None):
        import asyncio
        import functools

        runtime_ref = self
        allowed_names = {name for name in (allowed_tool_names or set()) if isinstance(name, str) and name}

        async def executor(tool_name: str, arguments: Optional[dict[str, Any]] = None):
            if allowed_names and tool_name not in allowed_names:
                raise MCPRuntimeError(f"Tool not available: {tool_name}")
            server_name, server_tool_name = runtime_ref._split_tool_name(tool_name)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                functools.partial(runtime_ref.call_tool, server_name, server_tool_name, arguments),
            )

        return executor

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise MCPRuntimeError("MCP runtime is disabled")
        client = self._clients.get(server_name)
        if not client:
            self.connect_server(server_name)
            client = self._clients[server_name]
        try:
            return client.call_tool(tool_name, arguments)
        except (MCPRuntimeError, OSError) as exc:
            # Evict the stale client so the next call reconnects rather than reusing a dead connection.
            self._clients.pop(server_name, None)
            client.close()
            raise MCPRuntimeError(
                f"MCP server '{server_name}' failed during tool call '{tool_name}'; evicted from cache"
            ) from exc

    def get_server_capabilities(self, server_name: str) -> dict[str, Any]:
        client = self._clients.get(server_name)
        if not client:
            self.connect_server(server_name)
            client = self._clients[server_name]
        return client.server_capabilities

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured_servers": self.configured_server_names,
            "connected_servers": list(self._clients.keys()),
        }

    def _resolve_config_values(self, value: Any) -> Any:
        if isinstance(value, str):
            if not self._resolve_env_vars:
                return value
            return os.path.expanduser(os.path.expandvars(value))
        if isinstance(value, list):
            return [self._resolve_config_values(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve_config_values(item) for key, item in value.items()}
        return value

    def _split_tool_name(self, tool_name: str) -> tuple[str, str]:
        # Try to match against known server names first (handles server names containing '.')
        for server_name in self._servers_config:
            prefix = f"{server_name}."
            if tool_name.startswith(prefix):
                tool_short_name = tool_name[len(prefix):]
                if tool_short_name:
                    return server_name, tool_short_name

        if len(self._servers_config) == 1:
            server_name = next(iter(self._servers_config.keys()))
            return server_name, tool_name

        raise MCPRuntimeError(
            f"Tool name '{tool_name}' must use the '<server>.<tool>' form when multiple MCP servers are configured"
        )

    def _build_client(self, server_name: str, server_config: dict[str, Any]) -> BaseMCPClient:
        server_type = str(server_config.get("type", "")).lower()

        if not server_type:
            if server_config.get("url"):
                server_type = "http"
            elif server_config.get("command"):
                server_type = "stdio"
            else:
                raise MCPRuntimeError(
                    f"MCP server '{server_name}' must define a transport type or command/url"
                )

        if server_type == "stdio":
            return MCPStdioClient(server_name, server_config)
        if server_type in {"http", "https"}:
            return MCPHttpClient(server_name, server_config)
        if server_type == "streamable_http":
            return MCPStreamableHttpClient(server_name, server_config)

        raise MCPRuntimeError(
            f"MCP server '{server_name}' uses unsupported transport type '{server_type}'"
        )
