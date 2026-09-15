from abc import ABC, abstractmethod


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

    def supports_tool_calling(self, model: str) -> bool:
        """
        Check whether the AI handler supports tool calling for the given model.
        Defaults to False unless overridden by a subclass.
        """
        return False
