from typing import Dict, Type
from pr_agent.tools.base import PRTool

class ToolRegistry:
    _tools: Dict[str, Type[PRTool]] = {}
    
    @classmethod
    def register(cls, command: str):
        def wrapper(subclass: Type[PRTool]):
            cls._tools[command] = subclass
            return subclass
        return wrapper
    
    @classmethod
    def get_tool(cls, command: str) -> Optional[Type[PRTool]]:
        return cls._tools.get(command)
    
    @classmethod
    def get_all_commands(cls) -> List[str]:
        return list(cls._tools.keys())
