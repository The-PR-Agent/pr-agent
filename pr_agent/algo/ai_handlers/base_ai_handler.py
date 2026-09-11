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
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2,
                              img_path: str = None, *, stage: str = None, chunk_index: int = None,
                              sample_index: int = None, files=None):
        """
        This method should be implemented to return a chat completion from the AI model.
        Args:
            model (str): the name of the model to use for the chat completion
            system (str): the system message string to use for the chat completion
            user (str): the user message string to use for the chat completion
            temperature (float): the temperature to use for the chat completion
            stage (str): the pipeline stage this call belongs to, for run-ledger attribution
            chunk_index (int): the diff chunk this call reviewed, when the caller chunks the diff
            sample_index (int): the consensus sample this call produced, when sampling more than one
            files (list[str]): the files covered by this call's diff, when known
        """
        pass
