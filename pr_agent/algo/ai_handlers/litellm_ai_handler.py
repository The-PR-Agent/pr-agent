import os
import litellm
import openai
import requests
from litellm import acompletion
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt

from pr_agent.algo import CLAUDE_EXTENDED_THINKING_MODELS, NO_SUPPORT_TEMPERATURE_MODELS, SUPPORT_REASONING_EFFORT_MODELS, USER_MESSAGE_ONLY_MODELS, STREAMING_REQUIRED_MODELS
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_helpers import _handle_streaming_response, MockResponse, _get_azure_ad_token, \
    _process_litellm_extra_body
from pr_agent.algo.utils import ReasoningEffort, get_version
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
import json

MODEL_RETRIES = 2
DUMMY_LITELLM_API_KEY = "dummy_key"  # placeholder set when no OpenAI key is configured


class LiteLLMAIHandler(BaseAiHandler):
    """
    This class handles interactions with the OpenAI API for chat completions.
    It initializes the API key and other settings from a configuration file,
    and provides a method for performing chat completions using the OpenAI ChatCompletion API.
    """

    def __init__(self):
        """
        Initializes the OpenAI API key and other settings from a configuration file.
        Raises a ValueError if the OpenAI key is missing.
        """
        self.azure = False
        self.api_base = None
        self.repetition_penalty = None

        if get_settings().get("LITELLM.DISABLE_AIOHTTP", False):
            litellm.disable_aiohttp_transport = True
        if get_settings().get("OPENAI.KEY", None):
            openai.api_key = get_settings().openai.key
            litellm.openai_key = get_settings().openai.key
        elif 'OPENAI_API_KEY' not in os.environ:
            litellm.api_key = DUMMY_LITELLM_API_KEY
        if get_settings().get("aws.AWS_ACCESS_KEY_ID"):
            assert get_settings().aws.AWS_SECRET_ACCESS_KEY and get_settings().aws.AWS_REGION_NAME, "AWS credentials are incomplete"
            os.environ["AWS_ACCESS_KEY_ID"] = get_settings().aws.AWS_ACCESS_KEY_ID
            os.environ["AWS_SECRET_ACCESS_KEY"] = get_settings().aws.AWS_SECRET_ACCESS_KEY
            os.environ["AWS_REGION_NAME"] = get_settings().aws.AWS_REGION_NAME
        if get_settings().get("LITELLM.DROP_PARAMS", None):
            litellm.drop_params = get_settings().litellm.drop_params
        if get_settings().get("LITELLM.SUCCESS_CALLBACK", None):
            litellm.success_callback = get_settings().litellm.success_callback
        if get_settings().get("LITELLM.FAILURE_CALLBACK", None):
            litellm.failure_callback = get_settings().litellm.failure_callback
        if get_settings().get("LITELLM.SERVICE_CALLBACK", None):
            litellm.service_callback = get_settings().litellm.service_callback
        if get_settings().get("OPENAI.ORG", None):
            litellm.organization = get_settings().openai.org
        if get_settings().get("OPENAI.API_TYPE", None):
            if get_settings().openai.api_type == "azure":
                self.azure = True
                litellm.azure_key = get_settings().openai.key
        if get_settings().get("OPENAI.API_VERSION", None):
            litellm.api_version = get_settings().openai.api_version
        if get_settings().get("OPENAI.API_BASE", None):
            litellm.api_base = get_settings().openai.api_base
            self.api_base = get_settings().openai.api_base
        if get_settings().get("ANTHROPIC.KEY", None):
            litellm.anthropic_key = get_settings().anthropic.key
        if get_settings().get("COHERE.KEY", None):
            litellm.cohere_key = get_settings().cohere.key
        if get_settings().get("GROQ.KEY", None):
            litellm.api_key = get_settings().groq.key
        if get_settings().get("REPLICATE.KEY", None):
            litellm.replicate_key = get_settings().replicate.key
        if get_settings().get("XAI.KEY", None):
            litellm.api_key = get_settings().xai.key
        if get_settings().get("HUGGINGFACE.KEY", None):
            litellm.huggingface_key = get_settings().huggingface.key
        if get_settings().get("HUGGINGFACE.API_BASE", None) and 'huggingface' in get_settings().config.model:
            litellm.api_base = get_settings().huggingface.api_base
            self.api_base = get_settings().huggingface.api_base
        if get_settings().get("OLLAMA.API_BASE", None):
            litellm.api_base = get_settings().ollama.api_base
            self.api_base = get_settings().ollama.api_base
        if get_settings().get("OLLAMA.API_KEY", None):
            litellm.api_key = get_settings().ollama.api_key
        if get_settings().get("HUGGINGFACE.REPETITION_PENALTY", None):
            self.repetition_penalty = float(get_settings().huggingface.repetition_penalty)
        if get_settings().get("VERTEXAI.VERTEX_PROJECT", None):
            litellm.vertex_project = get_settings().vertexai.vertex_project
            litellm.vertex_location = get_settings().get(
                "VERTEXAI.VERTEX_LOCATION", None
            )
        # Google AI Studio
        # SEE https://docs.litellm.ai/docs/providers/gemini
        if get_settings().get("GOOGLE_AI_STUDIO.GEMINI_API_KEY", None):
          os.environ["GEMINI_API_KEY"] = get_settings().google_ai_studio.gemini_api_key

        # Support deepseek models
        if get_settings().get("DEEPSEEK.KEY", None):
            os.environ['DEEPSEEK_API_KEY'] = get_settings().get("DEEPSEEK.KEY")

        # Support deepinfra models
        if get_settings().get("DEEPINFRA.KEY", None):
            os.environ['DEEPINFRA_API_KEY'] = get_settings().get("DEEPINFRA.KEY")

        # Support mistral models
        if get_settings().get("MISTRAL.KEY", None):
            os.environ["MISTRAL_API_KEY"] = get_settings().get("MISTRAL.KEY")
        
        # Support codestral models
        if get_settings().get("CODESTRAL.KEY", None):
            os.environ["CODESTRAL_API_KEY"] = get_settings().get("CODESTRAL.KEY")

        # Check for Azure AD configuration
        if get_settings().get("AZURE_AD.CLIENT_ID", None):
            self.azure = True
            # Generate access token using Azure AD credentials from settings
            access_token = _get_azure_ad_token()
            litellm.api_key = access_token
            openai.api_key = access_token
            
            # Set API base from settings
            self.api_base = get_settings().azure_ad.api_base
            litellm.api_base = self.api_base
            openai.api_base = self.api_base

        # Support for Openrouter models
        if get_settings().get("OPENROUTER.KEY", None):
            openrouter_api_key = get_settings().get("OPENROUTER.KEY", None)
            os.environ["OPENROUTER_API_KEY"] = openrouter_api_key
            litellm.api_key = openrouter_api_key
            openai.api_key = openrouter_api_key

            openrouter_api_base = get_settings().get("OPENROUTER.API_BASE", "https://openrouter.ai/api/v1")
            os.environ["OPENROUTER_API_BASE"] = openrouter_api_base
            self.api_base = openrouter_api_base
            litellm.api_base = openrouter_api_base

        # Models that only use user message
        self.user_message_only_models = USER_MESSAGE_ONLY_MODELS

        # Model that doesn't support temperature argument
        self.no_support_temperature_models = NO_SUPPORT_TEMPERATURE_MODELS

        # Models that support reasoning effort
        self.support_reasoning_models = SUPPORT_REASONING_EFFORT_MODELS

        # Models that support extended thinking (config override replaces the built-in list when non-empty)
        override = get_settings().config.get("claude_extended_thinking_models_override", []) or []
        if override and not isinstance(override, list):
            get_logger().warning(
                "Invalid claude_extended_thinking_models_override in config; expected a list of model names. "
                "Falling back to the built-in Claude extended-thinking model list."
            )
            override = []
        elif override and not all(isinstance(model, str) and model.strip() for model in override):
            get_logger().warning(
                "Invalid claude_extended_thinking_models_override in config; "
                "expected a list of model name strings. "
                "Falling back to the built-in Claude extended-thinking model list."
            )
            override = []
        self.claude_extended_thinking_models = list(override) if override else CLAUDE_EXTENDED_THINKING_MODELS

        # Models that require streaming
        self.streaming_required_models = STREAMING_REQUIRED_MODELS

    def prepare_logs(self, response, system, user, resp, finish_reason):
        response_log = response.dict().copy()
        response_log['system'] = system
        response_log['user'] = user
        response_log['output'] = resp
        response_log['finish_reason'] = finish_reason
        if hasattr(self, 'main_pr_language'):
            response_log['main_pr_language'] = self.main_pr_language
        else:
            response_log['main_pr_language'] = 'unknown'
        return response_log

    def _configure_claude_extended_thinking(self, model: str, kwargs: dict) -> dict:
        """
        Configure Claude extended thinking parameters if applicable.

        Args:
            model (str): The AI model being used
            kwargs (dict): The keyword arguments for the model call

        Returns:
            dict: Updated kwargs with extended thinking configuration
        """
        extended_thinking_budget_tokens = get_settings().config.get("extended_thinking_budget_tokens", 2048)
        extended_thinking_max_output_tokens = get_settings().config.get("extended_thinking_max_output_tokens", 4096)

        # Validate extended thinking parameters
        if not isinstance(extended_thinking_budget_tokens, int) or extended_thinking_budget_tokens <= 0:
            raise ValueError(f"extended_thinking_budget_tokens must be a positive integer, got {extended_thinking_budget_tokens}")
        if not isinstance(extended_thinking_max_output_tokens, int) or extended_thinking_max_output_tokens <= 0:
            raise ValueError(f"extended_thinking_max_output_tokens must be a positive integer, got {extended_thinking_max_output_tokens}")
        if extended_thinking_max_output_tokens < extended_thinking_budget_tokens:
            raise ValueError(f"extended_thinking_max_output_tokens ({extended_thinking_max_output_tokens}) must be greater than or equal to extended_thinking_budget_tokens ({extended_thinking_budget_tokens})")

        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": extended_thinking_budget_tokens
        }
        if get_settings().config.verbosity_level >= 2:
            get_logger().info(f"Adding max output tokens {extended_thinking_max_output_tokens} to model {model}, extended thinking budget tokens: {extended_thinking_budget_tokens}")
        kwargs["max_tokens"] = extended_thinking_max_output_tokens

        # temperature may only be set to 1 when thinking is enabled
        if get_settings().config.verbosity_level >= 2:
            get_logger().info("Temperature may only be set to 1 when thinking is enabled with claude models.")
        kwargs["temperature"] = 1

        return kwargs

    def add_litellm_callbacks(self, kwargs) -> dict:
        captured_extra = []

        def capture_logs(message):
            # Parsing the log message and context
            record = message.record
            log_entry = {}
            if record.get('extra', None).get('command', None) is not None:
                log_entry.update({"command": record['extra']["command"]})
            if record.get('extra', {}).get('pr_url', None) is not None:
                log_entry.update({"pr_url": record['extra']["pr_url"]})

            # Append the log entry to the captured_logs list
            captured_extra.append(log_entry)

        # Adding the custom sink to Loguru
        handler_id = get_logger().add(capture_logs)
        get_logger().debug("Capturing logs for litellm callbacks")
        get_logger().remove(handler_id)

        context = captured_extra[0] if len(captured_extra) > 0 else None

        command = context.get("command", "unknown")
        pr_url = context.get("pr_url", "unknown")
        git_provider = get_settings().config.git_provider

        metadata = dict()
        callbacks = litellm.success_callback + litellm.failure_callback + litellm.service_callback
        if "langfuse" in callbacks:
            metadata.update({
                "trace_name": command,
                "tags": [git_provider, command, f'version:{get_version()}'],
                "trace_metadata": {
                    "command": command,
                    "pr_url": pr_url,
                },
            })
        if "langsmith" in callbacks:
            metadata.update({
                "run_name": command,
                "tags": [git_provider, command, f'version:{get_version()}'],
                "extra": {
                    "metadata": {
                        "command": command,
                        "pr_url": pr_url,
                    }
                },
            })

        # Adding the captured logs to the kwargs
        kwargs["metadata"] = metadata

        return kwargs

    @property
    def deployment_id(self):
        """
        Returns the deployment ID for the OpenAI API.
        """
        return get_settings().get("OPENAI.DEPLOYMENT_ID", None)

    @retry(
        retry=retry_if_exception_type(openai.APIError) & retry_if_not_exception_type(openai.RateLimitError),
        stop=stop_after_attempt(MODEL_RETRIES),
    )
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        try:
            resp, finish_reason = None, None
            deployment_id = self.deployment_id
            if self.azure:
                model = 'azure/' + model
            if 'claude' in model and not system:
                system = "No system prompt provided"
                get_logger().warning(
                    "Empty system prompt for claude model. Adding a newline character to prevent OpenAI API error.")
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

            if img_path:
                try:
                    # check if the image link is alive
                    r = requests.head(img_path, allow_redirects=True)
                    if r.status_code == 404:
                        error_msg = f"The image link is not [alive](img_path).\nPlease repost the original image as a comment, and send the question again with 'quote reply' (see [instructions](https://pr-agent-docs.codium.ai/tools/ask/#ask-on-images-using-the-pr-code-as-context))."
                        get_logger().error(error_msg)
                        return f"{error_msg}", "error"
                except Exception as e:
                    get_logger().error(f"Error fetching image: {img_path}", e)
                    return f"Error fetching image: {img_path}", "error"
                messages[1]["content"] = [{"type": "text", "text": messages[1]["content"]},
                                          {"type": "image_url", "image_url": {"url": img_path}}]

            thinking_kwargs_gpt5 = None
            if model.startswith('gpt-5'):
                # Use configured reasoning_effort or default to MEDIUM
                config_effort = get_settings().config.reasoning_effort
                try:
                    ReasoningEffort(config_effort)
                    effort = config_effort
                except (ValueError, TypeError):
                    effort = ReasoningEffort.MEDIUM.value
                    if config_effort is not None:
                        get_logger().warning(
                            f"Invalid reasoning_effort '{config_effort}' in config. "
                            f"Using default '{effort}'. Valid values: {[e.value for e in ReasoningEffort]}"
                        )

                thinking_kwargs_gpt5 = {
                    "reasoning_effort": effort,
                    "allowed_openai_params": ["reasoning_effort"],
                }
                get_logger().info(f"Using reasoning_effort='{effort}' for GPT-5 model")
                model = 'openai/'+model.replace('_thinking', '')  # remove _thinking suffix


            # Currently, some models do not support a separate system and user prompts
            if model in self.user_message_only_models or get_settings().config.custom_reasoning_model:
                user = f"{system}\n\n\n{user}"
                system = ""
                get_logger().info(f"Using model {model}, combining system and user prompts")
                messages = [{"role": "user", "content": user}]
                kwargs = {
                    "model": model,
                    "deployment_id": deployment_id,
                    "messages": messages,
                    "timeout": get_settings().config.ai_timeout,
                    "api_base": self.api_base,
                }
            else:
                kwargs = {
                    "model": model,
                    "deployment_id": deployment_id,
                    "messages": messages,
                    "timeout": get_settings().config.ai_timeout,
                    "api_base": self.api_base,
                }

            # Add temperature only if model supports it
            if model not in self.no_support_temperature_models and not get_settings().config.custom_reasoning_model:
                # get_logger().info(f"Adding temperature with value {temperature} to model {model}.")
                kwargs["temperature"] = temperature

            if thinking_kwargs_gpt5:
                kwargs.update(thinking_kwargs_gpt5)
                if 'temperature' in kwargs:
                    del kwargs['temperature']

            # Add reasoning_effort if model supports it
            if model in self.support_reasoning_models:
                config_effort = get_settings().config.reasoning_effort
                try:
                    ReasoningEffort(config_effort)
                    reasoning_effort = config_effort
                except (ValueError, TypeError):
                    reasoning_effort = ReasoningEffort.MEDIUM.value
                    if config_effort is not None:
                        get_logger().warning(
                            f"Invalid reasoning_effort '{config_effort}' in config. "
                            f"Using default '{reasoning_effort}'. Valid values: {[e.value for e in ReasoningEffort]}"
                        )

                get_logger().info(f"Adding reasoning_effort with value {reasoning_effort} to model {model}.")
                kwargs["reasoning_effort"] = reasoning_effort

            # https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
            if (model in self.claude_extended_thinking_models) and get_settings().config.get("enable_claude_extended_thinking", False):
                kwargs = self._configure_claude_extended_thinking(model, kwargs)

            if get_settings().litellm.get("enable_callbacks", False):
                kwargs = self.add_litellm_callbacks(kwargs)

            seed = get_settings().config.get("seed", -1)
            if temperature > 0 and seed >= 0:
                raise ValueError(f"Seed ({seed}) is not supported with temperature ({temperature}) > 0")
            elif seed >= 0:
                get_logger().info(f"Using fixed seed of {seed}")
                kwargs["seed"] = seed

            if self.repetition_penalty:
                kwargs["repetition_penalty"] = self.repetition_penalty

            #Added support for extra_headers while using litellm to call underlying model, via a api management gateway, would allow for passing custom headers for security and authorization
            if get_settings().get("LITELLM.EXTRA_HEADERS", None):
                try:
                    litellm_extra_headers = json.loads(get_settings().litellm.extra_headers)
                    if not isinstance(litellm_extra_headers, dict):
                        raise ValueError("LITELLM.EXTRA_HEADERS must be a JSON object")
                except json.JSONDecodeError as e:
                    raise ValueError(f"LITELLM.EXTRA_HEADERS contains invalid JSON: {str(e)}")
                kwargs["extra_headers"] = litellm_extra_headers

            # Support for custom OpenAI body fields (e.g., Flex Processing)
            kwargs = _process_litellm_extra_body(kwargs)

            # Support for Bedrock custom inference profile via model_id
            model_id = get_settings().get("litellm.model_id")
            if model_id and 'bedrock/' in model:
                kwargs["model_id"] = model_id
                get_logger().info(f"Using Bedrock custom inference profile: {model_id}")

            get_logger().debug("Prompts", artifact={"system": system, "user": user})

            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"\nSystem prompt:\n{system}")
                get_logger().info(f"\nUser prompt:\n{user}")

            # Inject api_key to the call. This key is populated during init by providers
            # like Groq, XAI, Azure AD, and OpenRouter. Skip if None or placeholder.
            if litellm.api_key and litellm.api_key != DUMMY_LITELLM_API_KEY:
                kwargs["api_key"] = litellm.api_key

            # Get completion with automatic streaming detection
            resp, finish_reason, response_obj = await self._get_completion(**kwargs)

        except openai.RateLimitError as e:
            get_logger().error(f"Rate limit error during LLM inference: {e}")
            raise
        except openai.APIError as e:
            get_logger().warning(f"Error during LLM inference: {e}")
            raise
        except Exception as e:
            get_logger().warning(f"Unknown error during LLM inference: {e}")
            raise openai.APIError from e

        get_logger().debug(f"\nAI response:\n{resp}")

        # log the full response for debugging
        response_log = self.prepare_logs(response_obj, system, user, resp, finish_reason)
        get_logger().debug("Full_response", artifact=response_log)

        # for CLI debugging
        if get_settings().config.verbosity_level >= 2:
            get_logger().info(f"\nAI response:\n{resp}")

        return resp, finish_reason

    async def _get_completion(self, **kwargs):
        """
        Wrapper that automatically handles streaming for required models.
        """
        model = kwargs["model"]
        if model in self.streaming_required_models:
            kwargs["stream"] = True
            get_logger().info(f"Using streaming mode for model {model}")
            response = await acompletion(**kwargs)
            resp, finish_reason = await _handle_streaming_response(response)
            # Create MockResponse for streaming since we don't have the full response object
            mock_response = MockResponse(resp, finish_reason)
            return resp, finish_reason, mock_response
        else:
            response = await acompletion(**kwargs)
            if response is None or len(response["choices"]) == 0:
                raise openai.APIError
            return (response["choices"][0]['message']['content'],
                    response["choices"][0]["finish_reason"],
                    response)
