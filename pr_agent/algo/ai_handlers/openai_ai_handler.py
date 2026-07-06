from os import environ

import openai
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

OPENAI_RETRIES = 5


class OpenAIHandler(BaseAiHandler):
    def __init__(self):
        # Initialize OpenAIHandler specific attributes here
        try:
            super().__init__()
            environ["OPENAI_API_KEY"] = get_settings().openai.key
            if get_settings().get("OPENAI.ORG", None):
                openai.organization = get_settings().openai.org
            if get_settings().get("OPENAI.API_TYPE", None):
                if get_settings().openai.api_type == "azure":
                    self.azure = True
                    openai.azure_key = get_settings().openai.key
            if get_settings().get("OPENAI.API_VERSION", None):
                openai.api_version = get_settings().openai.api_version
            if get_settings().get("OPENAI.API_BASE", None):
                environ["OPENAI_BASE_URL"] = get_settings().openai.api_base

        except AttributeError as e:
            raise ValueError("OpenAI key is required") from e

    @property
    def deployment_id(self):
        """
        Returns the deployment ID for the OpenAI API.
        """
        return get_settings().get("OPENAI.DEPLOYMENT_ID", None)

    @retry(
        retry=retry_if_exception_type(openai.APIError) & retry_if_not_exception_type(openai.RateLimitError),
        stop=stop_after_attempt(OPENAI_RETRIES),
    )
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        try:
            if img_path:
                get_logger().warning(f"Image path is not supported for OpenAIHandler. Ignoring image path: {img_path}")
            get_logger().info("System: ", system)
            get_logger().info("User: ", user)
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            client = AsyncOpenAI()
            chat_completion = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            resp = chat_completion.choices[0].message.content
            finish_reason = chat_completion.choices[0].finish_reason
            usage = chat_completion.usage
            get_logger().info("AI response", response=resp, messages=messages, finish_reason=finish_reason,
                              model=model, usage=usage)
            return resp, finish_reason
        except openai.RateLimitError as e:
            get_logger().error(f"Rate limit error during LLM inference: {e}")
            raise
        except openai.APIError as e:
            get_logger().warning(f"Error during LLM inference: {e}")
            raise
        except Exception as e:
            get_logger().warning(f"Unknown error during LLM inference: {e}")
            raise openai.APIError from e
