_LANGCHAIN_INSTALLED = False

try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import AzureChatOpenAI, ChatOpenAI
    _LANGCHAIN_INSTALLED = True
except:  # we don't enforce langchain as a dependency, so if it's not installed, just move on
    pass


import openai
from langchain_core.runnables import Runnable
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

OPENAI_RETRIES = 5


class LangChainOpenAIHandler(BaseAiHandler):
    def __init__(self):
        if not _LANGCHAIN_INSTALLED:
            error_msg = "LangChain is not installed. Please install it with `pip install langchain`."
            get_logger().error(error_msg)
            raise ImportError(error_msg)
        
        super().__init__()
        self.azure = get_settings().get("OPENAI.API_TYPE", "").lower() == "azure"

    @property
    def deployment_id(self):
        """
        Returns the deployment ID for the OpenAI API.
        """
        return get_settings().get("OPENAI.DEPLOYMENT_ID", None)

    async def _create_chat_async(self, deployment_id=None):
        try:
            if self.azure:
                # Using Azure OpenAI service
                return AzureChatOpenAI(
                    openai_api_key=get_settings().openai.key,
                    openai_api_version=get_settings().openai.api_version,
                    azure_deployment=deployment_id,
                    azure_endpoint=get_settings().openai.api_base,
                )
            else:
                # Using standard OpenAI or other LLM services
                openai_api_base = get_settings().get("OPENAI.API_BASE", None)
                if openai_api_base is None or len(openai_api_base) == 0:
                    return ChatOpenAI(openai_api_key=get_settings().openai.key)
                else:
                    return ChatOpenAI(
                        openai_api_key=get_settings().openai.key, 
                        openai_api_base=openai_api_base
                    )
        except AttributeError as e:
            # Handle configuration errors
            error_msg = f"OpenAI {e.name} is required" if getattr(e, "name") else str(e)
            get_logger().error(error_msg)
            raise ValueError(error_msg) from e

    @retry(
        retry=retry_if_exception_type(openai.APIError) & retry_if_not_exception_type(openai.RateLimitError),
        stop=stop_after_attempt(OPENAI_RETRIES),
    )
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        if img_path:
            get_logger().warning(f"Image path is not supported for LangChainOpenAIHandler. Ignoring image path: {img_path}")
        try:
            messages = [SystemMessage(content=system), HumanMessage(content=user)]
            llm = await self._create_chat_async(deployment_id=self.deployment_id)
            
            if not isinstance(llm, Runnable):
                error_message = (
                    f"The Langchain LLM object ({type(llm)}) does not implement the Runnable interface. "
                    f"Please update your Langchain library to the latest version or "
                    f"check your LLM configuration to support async calls. "
                    f"PR-Agent is designed to utilize Langchain's async capabilities."
                )
                get_logger().error(error_message)
                raise NotImplementedError(error_message)

            # Handle parameters based on LLM type
            if isinstance(llm, (ChatOpenAI, AzureChatOpenAI)):
                # OpenAI models support all parameters
                resp = await llm.ainvoke(
                    input=messages,
                    model=model,
                    temperature=temperature
                )
            else:
                # Other LLMs (like Gemini) only support input parameter
                get_logger().info(f"Using simplified ainvoke for {type(llm)}")
                resp = await llm.ainvoke(input=messages)

            finish_reason = "completed"
            return resp.content, finish_reason

        except openai.RateLimitError as e:
            get_logger().error(f"Rate limit error during LLM inference: {e}")
            raise
        except openai.APIError as e:
            get_logger().warning(f"Error during LLM inference: {e}")
            raise
        except Exception as e:
            get_logger().warning(f"Unknown error during LLM inference: {e}")
            raise openai.APIError from e
