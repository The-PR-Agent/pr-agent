import os
import re
from contextlib import nullcontext

import openai
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.prompt_log_guard import prompt_logs_withheld, sanitized_provider_error
from pr_agent.algo.run_details import record_ai_call
from pr_agent.algo.token_budget import _as_int, get_max_input_tokens, get_max_tokens
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

_LANGCHAIN_INSTALLED = False

try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.runnables import Runnable
    from langchain_openai import AzureChatOpenAI, ChatOpenAI
    _LANGCHAIN_INSTALLED = True
except ImportError:  # we don't enforce langchain as a dependency, so if it's not installed, just move on
    pass

try:
    from langsmith import tracing_context
except ImportError:
    # LangSmith is optional; without it there is no LangSmith tracing to suppress.
    def tracing_context(*, enabled):
        return nullcontext()

OPENAI_RETRIES = 5


def _is_native_openai_endpoint(api_base: str | None) -> bool:
    """Recognize native defaults without overriding LangChain's endpoint selection."""
    api_base = api_base or os.getenv("OPENAI_API_BASE") or None
    if api_base is None:
        # Newer LangChain versions select this gateway before the SDK's own defaults.
        if os.getenv("LANGSMITH_GATEWAY", "").lower() not in ("", "false", "0", "no"):
            return False
        api_base = os.getenv("OPENAI_BASE_URL")
        if api_base is None:
            return True
    return re.fullmatch(r"(?ai:https://api\.openai\.com)(?::443)?/v1/?", api_base) is not None


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

    def get_model_token_limits(self, model: str, *, ignore_max_model_tokens: bool = False) -> tuple[int, int | None]:
        """Use this client's route, not LiteLLM's independent provider setting."""
        provider = "azure" if self.azure else "openai"
        window = get_max_tokens(
            model, ignore_max_model_tokens=ignore_max_model_tokens, custom_llm_provider=provider,
        )
        input_limit = get_max_input_tokens(model, custom_llm_provider=provider)
        api_base = get_settings().get("OPENAI.API_BASE", None)
        custom_limit = _as_int(get_settings().config.custom_model_max_tokens)
        # A compatible gateway does not establish ownership of the native model's full window.
        if (not self.azure and input_limit is not None and custom_limit > 0
                and not _is_native_openai_endpoint(api_base)):
            window = min(window, custom_limit)
        return window, input_limit

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
            error_msg = f"OpenAI {e.name} is required" if e.name else str(e)
            get_logger().error(error_msg)
            raise ValueError(error_msg) from e

    @retry(
        retry=retry_if_exception_type(openai.APIError) & retry_if_not_exception_type(
            (openai.RateLimitError, openai.BadRequestError, openai.UnprocessableEntityError,
             openai.AuthenticationError, openai.PermissionDeniedError)
        ),
        stop=stop_after_attempt(OPENAI_RETRIES),
    )
    async def chat_completion(
            self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str|None = None):
        if img_path:
            if prompt_logs_withheld():
                get_logger().warning("Image path is not supported for LangChainOpenAIHandler (details withheld)")
            else:
                get_logger().warning(
                    f"Image path is not supported for LangChainOpenAIHandler. Ignoring image path: {img_path}"
                )
        try:
            request_messages = self.build_request_messages(model, system, user)
            messages = [
                SystemMessage(content=request_messages[0]["content"]),
                HumanMessage(content=request_messages[1]["content"]),
            ]
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

            # LangSmith may trace inputs and outputs independently of PR-Agent's logs.
            # Restrict tracing suppression to this request, without affecting concurrent calls.
            trace_scope = tracing_context(enabled=False) if prompt_logs_withheld() else nullcontext()
            with trace_scope:
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

            metadata = getattr(resp, "response_metadata", None)
            if isinstance(metadata, dict):
                reason = (metadata.get("finish_reason") or metadata.get("stop_reason")
                          or metadata.get("finishReason"))
            else:
                reason = None
            finish_reason = str(reason).lower() if reason else "completed"
            if finish_reason in ("max_tokens", "max-tokens", "max_tokens_reached", "max_output_tokens"):
                finish_reason = "length"
            # Count the call but not its tokens. Langchain reports usage under key names of its
            # own (input_tokens/output_tokens) rather than the ones the collector reads, so
            # forwarding it unmapped would render zeros. Mapping them is not worth it while this
            # path stays cold: langchain is an optional extra (not installed by default) and no setting
            # selects this handler, so it is reachable only by injecting it into a command
            # programmatically.
            record_ai_call()
            return resp.content, finish_reason

        except openai.RateLimitError as e:
            if prompt_logs_withheld():
                get_logger().error(f"Rate limit during LLM inference ({sanitized_provider_error(e)}; details withheld)")
            else:
                get_logger().error(f"Rate limit error during LLM inference: {e}")
            raise
        except openai.APIError as e:
            if prompt_logs_withheld():
                get_logger().warning(f"Error during LLM inference ({sanitized_provider_error(e)}; details withheld)")
            else:
                get_logger().warning(f"Error during LLM inference: {e}")
            raise
        except Exception as e:
            if prompt_logs_withheld():
                get_logger().warning(f"Unknown error during LLM inference ({type(e).__name__}; details withheld)")
            else:
                get_logger().warning(f"Unknown error during LLM inference: {e}")
            raise
