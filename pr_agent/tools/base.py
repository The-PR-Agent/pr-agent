from abc import ABC, abstractmethod
from typing import List, Optional

class PRTool(ABC):
    def __init__(self, pr_url: str, ai_handler=None, args: Optional[List[str]] = None):
        self.pr_url = pr_url
        self.ai_handler = ai_handler
        self.args = args

    @abstractmethod
    async def run(self) -> None:
        pass
