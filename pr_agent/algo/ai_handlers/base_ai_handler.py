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

    def build_request_messages(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        *,
        image_path: str | None = None,
    ) -> list[dict]:
        """Build the text-only message payload dispatched by the base handler contract."""
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

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
