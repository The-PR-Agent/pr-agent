from abc import ABC, abstractmethod

import httpx
import openai


def make_api_error(message: str) -> openai.APIError:
    """
    Build a raisable openai.APIError. The constructor requires an httpx.Request,
    so `raise openai.APIError(...)` with only a message (or no arguments at all)
    dies with a TypeError instead of the intended APIError - which also stops
    tenacity's retry_if_exception_type(openai.APIError) from retrying.
    """
    return openai.APIError(message, httpx.Request("POST", "https://api.openai.com"), body=None)


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
