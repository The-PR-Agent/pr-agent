from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class BaseAiHandler(ABC):
    """
    This class defines the interface for an AI handler to be used by the PR Agents.
    """

    @abstractmethod
    def __init__(self):
        pass

    @property
    @abstractmethod
    def deployment_id(self):
        pass

    @abstractmethod
    async def chat_completion(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.2,
        img_path: str = None,
        tools: Optional[List[Dict]] = None,
    ):
        """
        This method should be implemented to return a chat completion from the AI model.
        Args:
            model (str): the name of the model to use for the chat completion
            system (str): the system message string to use for the chat completion
            user (str): the user message string to use for the chat completion
            temperature (float): the temperature to use for the chat completion
            img_path (str): optional image URL to include in the user message
            tools (list): optional OpenAI-compatible tool definitions
        """
        pass
