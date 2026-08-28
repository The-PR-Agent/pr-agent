import asyncio
import contextlib
import json
import os

import httpx
import litellm
import openai
import requests
from litellm import acompletion
from tenacity import (retry, retry_if_exception_type,
                      retry_if_not_exception_type, stop_after_attempt)

from pr_agent.algo import (CLAUDE_EXTENDED_THINKING_MODELS,
                           NO_SUPPORT_TEMPERATURE_MODELS,
                           STREAMING_REQUIRED_MODELS,
                           SUPPORT_REASONING_EFFORT_MODELS,
                           USER_MESSAGE_ONLY_MODELS)
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_helpers import (
    _get_azure_ad_token, _handle_streaming_response,
    _process_litellm_extra_body, _response_field)
from pr_agent.algo.run_details import _as_decimal_cost, record_ai_call
from pr_agent.algo.utils import ReasoningEffort, get_version
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

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
        self._aws_imds_mode = False
        self._aws_static_creds = None
        self._aws_imds_fell_back = False
        self._aws_boto3_creds = None  # original boto3 credentials object for IMDS refresh
        self._aws_bedrock_lock = asyncio.Lock()

        if get_settings().get("LITELLM.DISABLE_AIOHTTP", False):
            litellm.disable_aiohttp_transport = True
        if get_settings().get("OPENAI.KEY", None):
            openai.api_key = get_settings().openai.key
            litellm.openai_key = get_settings().openai.key
        elif 'OPENAI_API_KEY' not in os.environ:
            # These are process-wide globals and __init__ runs per request, so drop any
            # key a previous request's provider branch left behind — chat_completion()
            # forwards a truthy litellm.api_key, which would authenticate this request
            # with (and leak) the earlier one's credential. The provider branches below
            # repopulate it for the current request.
            litellm.api_key = None
            # Keyless OpenAI-compatible endpoints (vLLM, LM Studio, ...) still need *some*
            # key or the OpenAI SDK raises "The api_key client option must be set".
            # The placeholder must live in litellm.openai_key, which LiteLLM only consults
            # on the OpenAI-compatible path: the global litellm.api_key is checked ahead of
            # provider env vars (OPENROUTER_API_KEY, AZURE_API_KEY, ...) in LiteLLM's
            # resolution chain, so a placeholder there silently shadows them.
            litellm.openai_key = DUMMY_LITELLM_API_KEY
        # Custom Bedrock endpoint (e.g. a VPC endpoint interface endpoint); litellm reads this
        # directly from the environment regardless of the credentials model below
        if not os.environ.get("AWS_BEDROCK_RUNTIME_ENDPOINT") and get_settings().get("aws.AWS_BEDROCK_RUNTIME_ENDPOINT"):
            os.environ["AWS_BEDROCK_RUNTIME_ENDPOINT"] = get_settings().aws.AWS_BEDROCK_RUNTIME_ENDPOINT
        if os.environ.get("AWS_USE_IMDS", "").strip().lower() in ("1", "true", "yes"):
            import boto3
            import botocore.exceptions
            session = boto3.Session()
            try:
                creds = session.get_credentials()
                if creds:
                    self._aws_boto3_creds = creds  # store for refresh; avoids env-var re-read
                    self._write_frozen_aws_creds_to_env(creds.get_frozen_credentials())
                    self._aws_imds_mode = True
                    get_logger().info("Using ambient AWS credentials from IMDS/task-role/IRSA")
                else:
                    get_logger().warning(
                        "AWS_USE_IMDS is set but boto3 found no credentials; "
                        "falling through to static keys"
                    )
            except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError):
                # ClientError is intentionally not a BotoCoreError subclass in botocore's
                # design; it is raised by STS-backed providers (AssumeRole, IRSA web-identity).
                get_logger().exception(
                    "AWS_USE_IMDS: failed to resolve credentials via boto3; "
                    "falling through to static keys"
                )
            if not os.environ.get("AWS_REGION_NAME"):
                if get_settings().get("aws.AWS_REGION_NAME"):
                    os.environ["AWS_REGION_NAME"] = get_settings().aws.AWS_REGION_NAME
                else:
                    try:
                        region = session.region_name
                        if region:
                            os.environ["AWS_REGION_NAME"] = region
                            get_logger().info(f"AWS region resolved from environment: {region}")
                        else:
                            get_logger().warning(
                                "AWS_USE_IMDS: could not determine AWS region; "
                                "set AWS_REGION_NAME explicitly"
                            )
                    except Exception as e:
                        get_logger().warning(f"AWS_USE_IMDS: failed to resolve region via boto3: {e}")
            if get_settings().get("aws.AWS_ACCESS_KEY_ID"):
                if get_settings().aws.AWS_SECRET_ACCESS_KEY and get_settings().aws.AWS_REGION_NAME:
                    static_creds = {
                        "AWS_ACCESS_KEY_ID": get_settings().aws.AWS_ACCESS_KEY_ID,
                        "AWS_SECRET_ACCESS_KEY": get_settings().aws.AWS_SECRET_ACCESS_KEY,
                        "AWS_REGION_NAME": get_settings().aws.AWS_REGION_NAME,
                    }
                    static_token = get_settings().get("aws.AWS_SESSION_TOKEN", None)
                    if static_token:
                        static_creds["AWS_SESSION_TOKEN"] = static_token
                    if self._aws_imds_mode:
                        # IMDS succeeded; stash static keys for runtime fallback only
                        self._aws_static_creds = static_creds
                    else:
                        # IMDS failed; activate static credentials immediately and stash
                        # them so the runtime fallback path is also available if needed.
                        self._aws_static_creds = static_creds
                        os.environ["AWS_ACCESS_KEY_ID"] = static_creds["AWS_ACCESS_KEY_ID"]
                        os.environ["AWS_SECRET_ACCESS_KEY"] = static_creds["AWS_SECRET_ACCESS_KEY"]
                        os.environ["AWS_REGION_NAME"] = static_creds["AWS_REGION_NAME"]
                        if static_token:
                            os.environ["AWS_SESSION_TOKEN"] = static_token
                        elif "AWS_SESSION_TOKEN" in os.environ:
                            del os.environ["AWS_SESSION_TOKEN"]
                        get_logger().info(
                            "AWS_USE_IMDS: IMDS resolution failed; using static credentials"
                        )
        elif get_settings().get("aws.AWS_ACCESS_KEY_ID"):
            assert get_settings().aws.AWS_SECRET_ACCESS_KEY and get_settings().aws.AWS_REGION_NAME, "AWS credentials are incomplete"
            os.environ["AWS_ACCESS_KEY_ID"] = get_settings().aws.AWS_ACCESS_KEY_ID
            os.environ["AWS_SECRET_ACCESS_KEY"] = get_settings().aws.AWS_SECRET_ACCESS_KEY
            os.environ["AWS_REGION_NAME"] = get_settings().aws.AWS_REGION_NAME
            static_token = get_settings().get("aws.AWS_SESSION_TOKEN", None)
            if static_token:
                os.environ["AWS_SESSION_TOKEN"] = static_token
            elif "AWS_SESSION_TOKEN" in os.environ:
                del os.environ["AWS_SESSION_TOKEN"]
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
        if get_settings().get("SAMBANOVA.KEY", None):
            litellm.api_key = get_settings().sambanova.key
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

        # Support GLM (Z.AI / Zhipu) models
        if get_settings().get("ZAI.KEY", None):
            os.environ['ZAI_API_KEY'] = get_settings().get("ZAI.KEY")

        # Support Moonshot (Kimi) models
        if get_settings().get("MOONSHOT.KEY", None):
            os.environ['MOONSHOT_API_KEY'] = get_settings().get("MOONSHOT.KEY")
        # Optional Moonshot endpoint override (e.g. China: https://api.moonshot.cn/v1)
        if get_settings().get("MOONSHOT.API_BASE", None):
            os.environ['MOONSHOT_API_BASE'] = get_settings().get("MOONSHOT.API_BASE")

        # Support Qwen (Alibaba DashScope) models
        if get_settings().get("DASHSCOPE.KEY", None):
            os.environ['DASHSCOPE_API_KEY'] = get_settings().get("DASHSCOPE.KEY")

        # Support Xiaomi MiMo models
        if get_settings().get("XIAOMI_MIMO.KEY", None):
            os.environ['XIAOMI_MIMO_API_KEY'] = get_settings().get("XIAOMI_MIMO.KEY")

        # Support deepinfra models
        if get_settings().get("DEEPINFRA.KEY", None):
            os.environ['DEEPINFRA_API_KEY'] = get_settings().get("DEEPINFRA.KEY")

        # Support mistral models
        if get_settings().get("MISTRAL.KEY", None):
            os.environ["MISTRAL_API_KEY"] = get_settings().get("MISTRAL.KEY")

        # Support codestral models
        if get_settings().get("CODESTRAL.KEY", None):
            os.environ["CODESTRAL_API_KEY"] = get_settings().get("CODESTRAL.KEY")

        # Support Databricks-hosted models (e.g. Azure Databricks serving endpoints).
        # Uses PAT/key authentication via LiteLLM's env vars.
        # SEE https://docs.litellm.ai/docs/providers/databricks
        if get_settings().get("DATABRICKS.API_KEY", None):
            os.environ["DATABRICKS_API_KEY"] = get_settings().get("DATABRICKS.API_KEY")
        if get_settings().get("DATABRICKS.API_BASE", None):
            os.environ["DATABRICKS_API_BASE"] = get_settings().get("DATABRICKS.API_BASE")

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
        # Store stripped names so exact-match checks against the model succeed even when the config
        # entries contain surrounding whitespace (validation above already used model.strip()).
        self.claude_extended_thinking_models = (
            [model.strip() for model in override] if override else CLAUDE_EXTENDED_THINKING_MODELS
        )

        # Models that require streaming
        self.streaming_required_models = STREAMING_REQUIRED_MODELS
        self.force_streaming_provider = str(
            getattr(get_settings().litellm, "force_streaming_custom_llm_provider", "") or ""
        ).strip().lower()
        raw_force_streaming_api_base_substrings = getattr(
            get_settings().litellm, "force_streaming_api_base_substrings", []
        )
        if isinstance(raw_force_streaming_api_base_substrings, (list, tuple, set)):
            self.force_streaming_api_base_substrings = [
                str(value).strip().lower()
                for value in raw_force_streaming_api_base_substrings
                if value is not None and str(value).strip()
            ]
        else:
            if raw_force_streaming_api_base_substrings:
                get_logger().warning(
                    "LITELLM.FORCE_STREAMING_API_BASE_SUBSTRINGS must be a list, tuple, or set. "
                    "Ignoring invalid value."
                )
            self.force_streaming_api_base_substrings = []

    @staticmethod
    def _write_frozen_aws_creds_to_env(frozen) -> None:
        """Write a botocore FrozenCredentials snapshot into os.environ for litellm/Bedrock."""
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token
        elif "AWS_SESSION_TOKEN" in os.environ:
            del os.environ["AWS_SESSION_TOKEN"]

    def _refresh_aws_imds_credentials(self) -> bool:
        """Refresh ambient AWS credentials from boto3 provider chain. Called before each Bedrock call
        to avoid serving stale credentials from long-lived processes (EC2 roles rotate every ~6h).

        Uses the credentials object stored during __init__ rather than creating a new boto3.Session,
        which would read the already-set AWS_* env vars and return stale values.

        Returns True on success, False on failure (caller should trigger static fallback)."""
        import botocore.exceptions
        try:
            if self._aws_boto3_creds is None:
                get_logger().warning("IMDS credential refresh: no boto3 credentials object stored")
                return False
            self._write_frozen_aws_creds_to_env(self._aws_boto3_creds.get_frozen_credentials())
            return True
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError):
            # ClientError (STS/AssumeRole failures) is not a BotoCoreError subclass.
            get_logger().exception("IMDS credential refresh failed")
            return False

    def _activate_static_aws_fallback(self):
        """Swap process env to static credentials for Bedrock fallback after IMDS failure."""
        os.environ["AWS_ACCESS_KEY_ID"] = self._aws_static_creds["AWS_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = self._aws_static_creds["AWS_SECRET_ACCESS_KEY"]
        os.environ["AWS_REGION_NAME"] = self._aws_static_creds["AWS_REGION_NAME"]
        if "AWS_SESSION_TOKEN" in self._aws_static_creds:
            os.environ["AWS_SESSION_TOKEN"] = self._aws_static_creds["AWS_SESSION_TOKEN"]
        elif "AWS_SESSION_TOKEN" in os.environ:
            del os.environ["AWS_SESSION_TOKEN"]
        self._aws_imds_fell_back = True
        get_logger().warning("Bedrock call failed with ambient (IMDS) credentials; retrying with static credentials")

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

    @staticmethod
    def _record_completion_metadata(response, model=None, display_model=None) -> None:
        """Count a successful call and synchronously collect usage-based cost when possible."""
        usage = _response_field(response, "usage")

        cost_usd = None
        if get_settings().get("config.output_run_cost", False):
            # The guard covers the whole cost block, not just completion_cost:
            # reading inline costs and probing usage call model_dump() on
            # provider-specific objects, and a cost estimate must never fail a
            # call that already succeeded and was billed.
            try:
                cost_usd = LiteLLMAIHandler._read_positive_response_cost(response, usage)
                if cost_usd is None and model and LiteLLMAIHandler._has_priceable_usage(usage):
                    # Preserve LiteLLM's full usage object so completion_cost can price cache,
                    # reasoning, and provider-specific categories. Convert the small completed
                    # stream wrapper to a dictionary while retaining `response.usage`.
                    cost_response = response
                    if not isinstance(response, dict) and not hasattr(response, "model_dump"):
                        cost_response = response.dict()
                    cost_usd = litellm.completion_cost(completion_response=cost_response, model=model)
            except Exception as e:
                # Treat missing model pricing or insufficient usage as an unavailable call cost.
                # Retain the successful call so the collector marks the aggregate safely.
                get_logger().debug(f"Unable to estimate API cost for model {model}: {type(e).__name__}")

        recorded_model = display_model if display_model is not None else model
        record_ai_call(usage, model=recorded_model, cost_usd=cost_usd)

    @staticmethod
    def _read_positive_response_cost(response, usage):
        """Read a finalized inline cost, rejecting zero placeholders and invalid values."""
        candidates = [
            _response_field(usage, "response_cost"),
            _response_field(usage, "cost"),
        ]

        hidden_params = _response_field(response, "_hidden_params")
        if hasattr(hidden_params, "model_dump"):
            hidden_params = hidden_params.model_dump()
        if isinstance(hidden_params, dict):
            candidates.append(hidden_params.get("response_cost"))

        for candidate in candidates:
            decimal_cost = _as_decimal_cost(candidate)
            if decimal_cost is not None:
                return decimal_cost
        return None

    @staticmethod
    def _has_priceable_usage(usage) -> bool:
        """Return true when finalized usage reports a positive token count.

        Only token counters gate pricing: provider extras such as Groq's timing
        floats (queue_time, prompt_time) are not billable quantities, and letting
        them pass would send zero-token usage to completion_cost, which prices
        it as 0.0 instead of raising.
        """
        if usage is None:
            return False
        return any(
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            for count in (
                _response_field(usage, "prompt_tokens"),
                _response_field(usage, "completion_tokens"),
                _response_field(usage, "total_tokens"),
            )
        )

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
        probe = object()
        captured_extra = []

        def capture_logs(message):
            # Parsing the log message and context
            record = message.record
            extra = record.get("extra") or {}
            if extra.get("litellm_callbacks_probe") is not probe:
                return
            log_entry = {}
            if extra.get("command") is not None:
                log_entry.update({"command": extra["command"]})
            if extra.get("pr_url") is not None:
                log_entry.update({"pr_url": extra["pr_url"]})

            # Append the log entry to the captured_logs list
            captured_extra.append(log_entry)

        # Adding the custom sink to Loguru
        handler_id = get_logger().add(capture_logs)
        try:
            get_logger().debug("Capturing logs for litellm callbacks",
                               litellm_callbacks_probe=probe)
        finally:
            get_logger().remove(handler_id)

        context = captured_extra[0] if len(captured_extra) > 0 else {}

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

    def _get_request_user_field(self) -> str:
        """
        Build the value for the OpenAI-compatible "user" request field from the current
        logging context: a compact JSON string carrying the command and the PR URL,
        e.g. {"command":"improve","pr_url":"https://..."}. Returns an empty string when
        no context is available.
        """
        # The probe record is matched by identity, so a concurrent request adding
        # its own sink at the same time cannot capture this request's context nor
        # leak its own into it.
        probe = object()
        captured_extra = []

        def capture_logs(message):
            extra = message.record.get("extra") or {}
            if extra.get("user_field_probe") is not probe:
                return
            log_entry = {}
            if extra.get("command") is not None:
                log_entry.update({"command": extra["command"]})
            if extra.get("pr_url") is not None:
                log_entry.update({"pr_url": extra["pr_url"]})
            captured_extra.append(log_entry)

        handler_id = get_logger().add(capture_logs)
        try:
            get_logger().debug("Capturing the request context for the user field",
                               user_field_probe=probe)
        finally:
            get_logger().remove(handler_id)

        context = captured_extra[0] if len(captured_extra) > 0 else {}
        if not context:
            return ""
        # Cap the individual values before serialization, so the result stays
        # valid JSON: slicing the serialized string could cut through closing
        # quotes and braces. 30 chars cover every tool command; 200 chars of
        # pr_url keep the total under 256 with the JSON overhead.
        for key, max_len in (("command", 30), ("pr_url", 200)):
            value = context.get(key)
            if isinstance(value, str) and len(value) > max_len:
                context[key] = value[:max_len]
        return json.dumps(context, separators=(",", ":"))

    @property
    def deployment_id(self):
        """
        Returns the deployment ID for the OpenAI API.
        """
        return get_settings().get("OPENAI.DEPLOYMENT_ID", None)

    @staticmethod
    def _resolve_cache_control_injection_points():
        """Read and validate LITELLM.CACHE_CONTROL_INJECTION_POINTS for Anthropic prompt caching
        via LiteLLM (https://docs.litellm.ai/docs/tutorials/prompt_caching).

        Accepts a native TOML array in the [litellm] section of configuration.toml / .pr_agent.toml,
        e.g. ``cache_control_injection_points = [{location = "message", role = "system"}]``; a
        JSON-string form is also accepted so the value can be supplied via an environment-variable
        override. Returns the parsed list, or None when unset/disabled. Raises ValueError on a
        malformed value so the caller can surface it as a configuration error rather than retrying it.
        """
        cache_control_injection_points = get_settings().get("LITELLM.CACHE_CONTROL_INJECTION_POINTS", None)
        # Only genuinely unset/disabled values short-circuit. Other falsy-but-malformed values
        # (e.g. 0, False, {}) fall through to type validation below and raise ValueError.
        if cache_control_injection_points in (None, "", []):
            return None
        if isinstance(cache_control_injection_points, str):
            try:
                cache_control_injection_points = json.loads(cache_control_injection_points)
            except json.JSONDecodeError as e:
                raise ValueError(f"LITELLM.CACHE_CONTROL_INJECTION_POINTS contains invalid JSON: {str(e)}") from e
        if not isinstance(cache_control_injection_points, list):
            raise ValueError("LITELLM.CACHE_CONTROL_INJECTION_POINTS must be a JSON/TOML array")
        return cache_control_injection_points

    @retry(
        retry=retry_if_exception_type(openai.APIError) & retry_if_not_exception_type(openai.RateLimitError),
        stop=stop_after_attempt(MODEL_RETRIES),
        reraise=True,  # surface the provider's error; RetryError hides the reason
    )
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        # Serialize env-var mutation + Bedrock call for IMDS mode to prevent concurrent
        # requests from interleaving os.environ credentials during asyncio.gather usage.
        # Validate config-derived kwargs before the try/except below, so a malformed value raises a
        # ValueError config error instead of being wrapped as openai.APIError and retried.
        cache_control_injection_points = self._resolve_cache_control_injection_points()
        _bedrock_imds = self._aws_imds_mode and any(
            provider in model for provider in ("bedrock/", "bedrock_mantle/")
        )
        async with (self._aws_bedrock_lock if _bedrock_imds else contextlib.nullcontext()):
            if _bedrock_imds and not self._aws_imds_fell_back:
                if not self._refresh_aws_imds_credentials() and self._aws_static_creds:
                    self._activate_static_aws_fallback()
                    self._aws_imds_fell_back = True
            try:
                resp, finish_reason = None, None
                deployment_id = self.deployment_id
                # Capture the original model string so an explicit provider prefix in the
                # user config (e.g. "azure/gpt-5...") can be preserved when the GPT-5 branch
                # rebuilds the routed model name below.
                user_model = model
                # Capture the provider prefix before any rewriting below. Databricks auth/endpoint
                # selection keys off this; rewriting (e.g. 'azure/' + model when Azure is enabled in
                # a multi-provider config) would otherwise hide the 'databricks/' prefix and bypass
                # the guards that keep Databricks on its own DATABRICKS_API_KEY/DATABRICKS_API_BASE.
                is_databricks = model.startswith("databricks/")
                # OpenRouter models keep their "openrouter/" prefix: __init__ already
                # routed them to the OpenRouter api_key/api_base, and rewriting to
                # "azure/openrouter/..." would both misroute the request and skip the
                # OpenRouter controls block below (guarded by the same prefix).
                is_openrouter = isinstance(model, str) and model.startswith("openrouter/")
                if self.azure and not is_databricks and not is_openrouter:
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
                # Detect GPT-5 family regardless of provider prefix(es) on the model name.
                # Users sometimes put a provider prefix in config (e.g. "openai/gpt-5.1-codex-max"),
                # and Azure mode auto-prepends "azure/", which together can produce stacked prefixes
                # like "azure/openai/gpt-5...". Without normalization the GPT-5 path is skipped and
                # litellm rejects the request with UnsupportedParamsError for temperature=0.2.
                model_base = model
                while model_base.startswith(('openai/', 'azure/')):
                    model_base = model_base.removeprefix('openai/').removeprefix('azure/')
                if model_base.startswith('gpt-5'):
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
                    # Routing priority: Azure mode > explicit provider prefix in user config > openai/
                    # default. This preserves an explicit "azure/" the user wrote in config even when
                    # self.azure is false, and avoids stacking when self.azure already added "azure/".
                    if self.azure:
                        provider_prefix = 'azure/'
                    elif user_model.startswith('azure/'):
                        provider_prefix = 'azure/'
                    elif user_model.startswith('openai/'):
                        provider_prefix = 'openai/'
                    else:
                        provider_prefix = 'openai/'
                    model = provider_prefix + model_base.replace('_thinking', '')  # remove _thinking suffix


                # Currently, some models do not support a separate system and user prompts
                if model in self.user_message_only_models or get_settings().config.custom_reasoning_model:
                    user = f"{system}\n\n\n{user}"
                    system = ""
                    get_logger().info(f"Using model {model}, combining system and user prompts")
                    messages = [{"role": "user", "content": user}]

                # Build request kwargs after normalizing messages for the target model.
                # Databricks selects its endpoint via the DATABRICKS_API_BASE env var; don't let an
                # api_base configured by another provider (OpenRouter/Ollama/Azure AD/OpenAI) during
                # __init__ override it in multi-provider configs. None lets LiteLLM read the env var.
                api_base = os.environ.get("DATABRICKS_API_BASE") if is_databricks else self.api_base
                kwargs = {
                        "model": model,
                        "deployment_id": deployment_id,
                        "messages": messages,
                        "timeout": get_settings().config.ai_timeout,
                        "api_base": api_base,
                    }

                # Add temperature only if model supports it
                if model not in self.no_support_temperature_models and not get_settings().config.custom_reasoning_model:
                    # get_logger().info(f"Adding temperature with value {temperature} to model {model}.")
                    kwargs["temperature"] = temperature

                if thinking_kwargs_gpt5:
                    kwargs.update(thinking_kwargs_gpt5)
                    if 'temperature' in kwargs:
                        del kwargs['temperature']

                openrouter_reasoning_effort = None
                reasoning_model = model.rsplit(":", 1)[0] if model.startswith("openrouter/") else model
                # Add reasoning_effort if model supports it. Match the bare model
                # id as well as any provider-prefixed form (e.g.
                # "openrouter/google/gemini-2.5-pro", "gemini/gemini-2.5-pro"), so a
                # configured reasoning_effort is not silently dropped for models the
                # user references with a provider prefix. OpenRouter routing variants
                # such as :nitro and :floor are stripped only for this membership test.
                if any(
                    reasoning_model == m or reasoning_model.endswith("/" + m)
                    for m in self.support_reasoning_models
                ):
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

                    if model.startswith("openrouter/"):
                        # LiteLLM 1.98.0 rejects top-level reasoning_effort for some
                        # OpenRouter model IDs it does not mark as reasoning-capable;
                        # defer to OpenRouter's unified reasoning object below.
                        openrouter_reasoning_effort = reasoning_effort
                    else:
                        get_logger().info(f"Adding reasoning_effort with value {reasoning_effort} to model {model}.")
                        kwargs["reasoning_effort"] = reasoning_effort

                # https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
                if (model in self.claude_extended_thinking_models) and get_settings().config.get("enable_claude_extended_thinking", False):
                    kwargs = self._configure_claude_extended_thinking(model, kwargs)

                # Optional output token limit; 0 = unset. Without max_tokens some
                # providers apply a low service-side default (Bedrock Converse: 4096,
                # which reasoning can fully consume, returning empty content).
                # setdefault keeps the extended-thinking limit authoritative.
                try:
                    max_output_tokens = int(get_settings().config.get("max_output_tokens", 0))
                except (TypeError, ValueError):
                    max_output_tokens = 0
                if max_output_tokens > 0:
                    kwargs.setdefault("max_tokens", max_output_tokens)

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

                # Optional provider-side request attribution: when config.add_user_to_requests
                # is enabled, send the current command and PR URL in the OpenAI-compatible
                # "user" field, so provider logs and usage exports can be attributed to a
                # specific PR without timestamp correlation (OpenRouter shows it as
                # "external_user" and includes it in the activity export). Disabled by
                # default: it shares request-attribution data with the model provider.
                if get_settings().config.get("add_user_to_requests", False):
                    request_user = self._get_request_user_field()
                    if request_user:
                        try:
                            supported_params = litellm.get_supported_openai_params(model=model) or []
                        except Exception:
                            supported_params = []
                        if "user" in supported_params:
                            kwargs["user"] = request_user
                        elif isinstance(model, str) and model.startswith("openrouter/"):
                            # LiteLLM's OpenRouter transformation does not forward the
                            # standard "user" parameter; extra_body reaches the
                            # OpenAI-compatible request body verbatim.
                            user_extra_body = kwargs.get("extra_body") or {}
                            user_extra_body["user"] = request_user
                            kwargs["extra_body"] = user_extra_body
                        else:
                            # Providers whose parameter mapping does not accept "user"
                            # (e.g. gemini, deepseek) would reject the request when
                            # litellm.drop_params is off: skip the field instead of
                            # breaking the call.
                            get_logger().debug(
                                f"add_user_to_requests: user field unsupported for {model}, skipped")

                # Anthropic prompt caching via LiteLLM's cache_control_injection_points. The value
                # is validated before the try/except (see above) so a malformed config surfaces as
                # a ValueError instead of being retried. The kwarg is Anthropic-specific (Claude via
                # the Anthropic API, Bedrock or Vertex), so gate on the model to avoid passing an
                # unsupported param to other providers when litellm.drop_params is off. setdefault
                # guards against overwriting a value already merged into kwargs.
                if cache_control_injection_points:
                    if isinstance(model, str) and "claude" in model.lower():
                        kwargs.setdefault("cache_control_injection_points", cache_control_injection_points)
                    else:
                        get_logger().debug(
                            f"cache_control_injection_points configured but not applied: {model} is not an "
                            "Anthropic (Claude) model")

                # Classic `bedrock/` calls use model_id for Bedrock Runtime inference profiles.
                # Bedrock Mantle uses Projects, so `bedrock_mantle/` intentionally omits it.
                model_id = get_settings().get("litellm.model_id")
                if model_id and 'bedrock/' in model:
                    kwargs["model_id"] = model_id
                    get_logger().info(f"Using Bedrock custom inference profile: {model_id}")

                # OpenRouter provider routing, reasoning control and output cap.
                # Registered reasoning models inherit config.reasoning_effort when
                # no OpenRouter-specific effort or token budget is configured.
                if isinstance(model, str) and model.startswith("openrouter/"):
                    openrouter_settings = get_settings().get("openrouter", {})
                    extra_body = kwargs.get("extra_body") or {}

                    # Normalize operator-controlled config: Dynaconf/env overrides can
                    # arrive as strings (AUTO_CAST_FOR_DYNACONF is disabled), so coerce
                    # defensively instead of trusting the declared types.
                    def _as_list(value):
                        if isinstance(value, (list, tuple)):
                            return [str(v).strip() for v in value if str(v).strip()]
                        if isinstance(value, str):
                            return [v.strip() for v in value.split(",") if v.strip()]
                        return []

                    def _as_bool(value, default=True):
                        if isinstance(value, bool):
                            return value
                        if isinstance(value, str):
                            return value.strip().lower() in ("1", "true", "yes", "on")
                        return default

                    def _as_int(value):
                        try:
                            return int(value)
                        except (TypeError, ValueError):
                            return 0

                    provider_only = _as_list(openrouter_settings.get("provider_only", []))
                    provider_order = _as_list(openrouter_settings.get("provider_order", []))
                    if provider_only:
                        extra_body.setdefault("provider", {})["only"] = provider_only
                    elif provider_order:
                        provider = extra_body.setdefault("provider", {})
                        provider["order"] = provider_order
                        provider["allow_fallbacks"] = _as_bool(openrouter_settings.get("allow_fallbacks", True))

                    reasoning = {}
                    effective_reasoning_effort = str(
                        openrouter_settings.get("reasoning_effort", "") or ""
                    ).strip().lower()
                    reasoning_max_tokens = _as_int(openrouter_settings.get("reasoning_max_tokens", 0))
                    if effective_reasoning_effort:
                        try:
                            ReasoningEffort(effective_reasoning_effort)
                        except (TypeError, ValueError):
                            get_logger().warning(
                                f"Ignoring invalid openrouter.reasoning_effort '{effective_reasoning_effort}'. "
                                f"Valid values: {[effort.value for effort in ReasoningEffort]}."
                            )
                            effective_reasoning_effort = ""
                    if not effective_reasoning_effort:
                        if reasoning_max_tokens > 0 and openrouter_reasoning_effort:
                            if openrouter_reasoning_effort == "none":
                                get_logger().warning(
                                    f"Ignoring config.reasoning_effort='{openrouter_reasoning_effort}' because "
                                    "openrouter.reasoning_max_tokens takes precedence."
                                )
                            else:
                                get_logger().info(
                                    "Using openrouter.reasoning_max_tokens over"
                                    f" config.reasoning_effort='{openrouter_reasoning_effort}'."
                                )
                        elif reasoning_max_tokens <= 0:
                            effective_reasoning_effort = openrouter_reasoning_effort or ""

                    # Preserve explicit disablement; otherwise keep effort and
                    # max_tokens mutually exclusive by preferring the token budget.
                    if effective_reasoning_effort == "none":
                        if reasoning_max_tokens > 0:
                            get_logger().warning(
                                "Ignoring openrouter.reasoning_max_tokens because "
                                "openrouter.reasoning_effort='none' disables reasoning."
                            )
                        reasoning["enabled"] = False
                    elif reasoning_max_tokens > 0:
                        if effective_reasoning_effort:
                            get_logger().warning(
                                f"Ignoring openrouter.reasoning_effort='{effective_reasoning_effort}' because "
                                "openrouter.reasoning_max_tokens takes precedence."
                            )
                        reasoning["max_tokens"] = reasoning_max_tokens
                    elif effective_reasoning_effort:
                        # OpenRouter uses xhigh for the max alias; extra_body bypasses
                        # LiteLLM's OpenRouter parameter mapping.
                        reasoning["effort"] = (
                            "xhigh" if effective_reasoning_effort == "max" else effective_reasoning_effort
                        )
                    if reasoning:
                        get_logger().info(f"Adding OpenRouter reasoning {reasoning} to model {model}.")
                        extra_body["reasoning"] = reasoning

                    if extra_body:
                        kwargs["extra_body"] = extra_body

                    max_tokens = _as_int(openrouter_settings.get("max_tokens", 0))
                    if max_tokens > 0:
                        existing = _as_int(kwargs.get("max_tokens", 0))
                        kwargs["max_tokens"] = min(existing, max_tokens) if existing > 0 else max_tokens
                    effective_max_tokens = _as_int(kwargs.get("max_tokens", 0))
                    effective_reasoning_max_tokens = _as_int(reasoning.get("max_tokens", 0))
                    if (
                        model.startswith("openrouter/anthropic/")
                        and effective_reasoning_max_tokens > 0
                        and 0 < effective_max_tokens <= effective_reasoning_max_tokens
                    ):
                        get_logger().warning(
                            f"OpenRouter Anthropic max_tokens ({effective_max_tokens}) must be greater than "
                            f"reasoning_max_tokens ({effective_reasoning_max_tokens}) to leave output headroom."
                        )

                get_logger().debug("Prompts", artifact={"system": system, "user": user})

                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"\nSystem prompt:\n{system}")
                    get_logger().info(f"\nUser prompt:\n{user}")

                # Inject api_key to the call. This key is populated during init by providers
                # like Groq, SambaNova, XAI, Azure AD, and OpenRouter. Skip if None or placeholder.
                # Databricks authenticates via the DATABRICKS_API_KEY/DATABRICKS_API_BASE env vars,
                # so don't override it with another provider's key in multi-provider configs.
                if (litellm.api_key and litellm.api_key != DUMMY_LITELLM_API_KEY
                        and not is_databricks):
                    kwargs["api_key"] = litellm.api_key

                # Optional fixed provider override, so a raw hosted model id reaches the
                # provider unchanged instead of being rewritten by LiteLLM's prefix inference.
                custom_llm_provider = str(
                    getattr(get_settings().litellm, "custom_llm_provider", "") or ""
                ).strip().lower()
                if custom_llm_provider:
                    kwargs["custom_llm_provider"] = custom_llm_provider

                # Get completion with automatic streaming detection
                resp, finish_reason, response_obj = await self._get_completion(**kwargs)

            except openai.RateLimitError as e:
                get_logger().error(f"Rate limit error during LLM inference: {e}")
                raise
            except openai.APIError as e:
                if _bedrock_imds and not self._aws_imds_fell_back and self._aws_static_creds:
                    self._activate_static_aws_fallback()
                    # Retry immediately while still holding the lock so that the
                    # env-var swap is fully visible to this call. Letting @retry
                    # handle the retry would release the lock between attempts,
                    # allowing a concurrent coroutine to overwrite os.environ.
                    resp, finish_reason, response_obj = await self._get_completion(**kwargs)
                else:
                    get_logger().warning(f"Error during LLM inference: {e}")
                    raise
            except Exception as e:
                get_logger().warning(f"Unknown error during LLM inference: {e}")
                raise openai.APIError(
                    str(e),
                    request=httpx.Request("POST", model),
                    body=None,
                ) from e

        # Post-response bookkeeping happens outside the Bedrock IMDS lock above: it
        # touches no os.environ credentials, and in IMDS mode the lock serializes
        # every concurrent call, so holding it through logging and cost pricing
        # would make each waiting coroutine pay for them serially.
        get_logger().debug(f"\nAI response:\n{resp}")

        # log the full response for debugging
        response_log = self.prepare_logs(response_obj, system, user, resp, finish_reason)
        get_logger().debug("Full_response", artifact=response_log)

        # for CLI debugging
        if get_settings().config.verbosity_level >= 2:
            get_logger().info(f"\nAI response:\n{resp}")

        self._record_completion_metadata(response_obj, model=model, display_model=user_model)

        return resp, finish_reason

    async def _get_completion(self, **kwargs):
        """
        Wrapper that automatically handles streaming for required models.
        """
        model = kwargs["model"]
        custom_llm_provider = str(kwargs.get("custom_llm_provider") or "").strip().lower()
        api_base_value = kwargs.get("api_base")
        api_base = api_base_value.strip().lower() if isinstance(api_base_value, str) else ""
        force_streaming = (
            bool(custom_llm_provider)
            and custom_llm_provider == self.force_streaming_provider
            and bool(self.force_streaming_api_base_substrings)
            and any(substring in api_base for substring in self.force_streaming_api_base_substrings)
        )

        # Some OpenAI-compatible endpoints can return an empty-string
        # finish_reason on non-streaming responses, which LiteLLM rejects during
        # response normalization. Streaming avoids that conversion path.
        if model in self.streaming_required_models or force_streaming:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            if force_streaming and model not in self.streaming_required_models:
                get_logger().info(
                    f"Using streaming mode for model {model} "
                    "due to OpenAI-compatible endpoint compatibility"
                )
            else:
                get_logger().info(f"Using streaming mode for model {model}")
            response = await acompletion(**kwargs)
            return await _handle_streaming_response(response, model=model)
        else:
            response = await acompletion(**kwargs)
            if response is None or len(response["choices"]) == 0:
                raise openai.APIError(
                    f"No choices in model response from {model}",
                    request=httpx.Request("POST", model),
                    body=None,
                )
            content = response["choices"][0]['message']['content']
            finish_reason = response["choices"][0]["finish_reason"]
            if not content:
                get_logger().warning(
                    f"Empty content in model response, finish_reason: {finish_reason}")
                raise openai.APIError(
                    f"Empty content in model response (finish_reason: {finish_reason})",
                    request=httpx.Request("POST", model),
                    body=None,
                )
            return content, finish_reason, response
