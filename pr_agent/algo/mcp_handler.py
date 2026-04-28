import json
from typing import List, Dict, Any, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import asyncio

class MCPHandler:
    def __init__(self, command: str, args: List[str]):
        self.server_params = StdioServerParameters(command=command, args=args)
        self.session: Optional[ClientSession] = None

    async def __aenter__(self):
        self.stdio_client = stdio_client(self.server_params)
        self.read, self.write = await self.stdio_client.__aenter__()
        self.session = ClientSession(self.read, self.write)
        await self.session.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.__aexit__(exc_type, exc_val, exc_tb)
        await self.stdio_client.__aexit__(exc_type, exc_val, exc_tb)

    async def list_tools(self) -> List[Dict[str, Any]]:
        response = await self.session.list_tools()
        return [tool.model_dump() for tool in response.tools]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        response = await self.session.call_tool(name, arguments=arguments)
        return [content.model_dump() for content in response.content]
