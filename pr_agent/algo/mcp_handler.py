from typing import Any, Dict, List


class MCPHandler:
    def __init__(self, command: str, args: List[str]):
        self.command = command
        self.args = args
        self.session = None
        self.stdio_client = None

    async def __aenter__(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise RuntimeError("MCP integration requires the optional 'mcp' Python package to be installed") from e

        self.server_params = StdioServerParameters(command=self.command, args=self.args)
        self.stdio_client = stdio_client(self.server_params)
        self.read, self.write = await self.stdio_client.__aenter__()
        self.session = ClientSession(self.read, self.write)
        await self.session.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.__aexit__(exc_type, exc_val, exc_tb)
        if self.stdio_client:
            await self.stdio_client.__aexit__(exc_type, exc_val, exc_tb)

    async def get_openai_tools(self) -> List[Dict[str, Any]]:
        # This converts MCP tool definitions to OpenAI tool definitions
        response = await self.session.list_tools()
        tools = []
        for tool in response.tools:
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                }
            })
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        response = await self.session.call_tool(name, arguments=arguments)
        return [content.model_dump() for content in response.content]
