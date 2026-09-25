import asyncio
import contextvars
import os
import re
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from threading import Lock

from jinja2 import Environment, StrictUndefined
from tiktoken import encoding_for_model, get_encoding

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def _await_coroutine(coro):
    """Run a coroutine to completion from a synchronous call site.

    ``asyncio.run`` cannot be called from a running event loop, and the accurate
    token-count path is invoked synchronously from tools that run inside one, so
    an active loop runs the coroutine on a dedicated worker loop instead. The
    caller's contextvars are copied into the worker so request-scoped settings
    (e.g. starlette ``context``) stay visible to the token-count coroutine.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    worker_loop = asyncio.new_event_loop()
    context = contextvars.copy_context()
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="token-count") as executor:
            return executor.submit(context.run, worker_loop.run_until_complete, coro).result()
    finally:
        worker_loop.close()


class ModelTypeValidator:
    @staticmethod
    def is_openai_model(model_name: str) -> bool:
        return 'gpt' in model_name or re.match(r"^o[1-9](-mini|-preview)?$", model_name)


class TokenEncoder:
    _encoder_instance = None
    _model = None
    _lock = Lock()  # Create a lock object

    @classmethod
    def get_token_encoder(cls, model=None):
        configured_model = get_settings().config.model
        model = model or configured_model

        # Use a fresh tokenizer for explicit fallback models without replacing
        # the cached tokenizer for the configured primary model.
        if model != configured_model:
            return cls._create_encoder(model)

        if cls._encoder_instance is None or model != cls._model:  # Check without acquiring the lock for performance
            with cls._lock:  # Lock acquisition to ensure thread safety
                if cls._encoder_instance is None or model != cls._model:
                    cls._model = model
                    cls._encoder_instance = cls._create_encoder(cls._model)
        return cls._encoder_instance

    @staticmethod
    def _create_encoder(model):
        try:
            return encoding_for_model(model) if "gpt" in model else get_encoding("o200k_base")
        except Exception:
            return get_encoding("o200k_base")


class TokenHandler:
    """
    A class for handling tokens in the context of a pull request.

    Attributes:
    - encoder: An object of the encoding_for_model class from the tiktoken module. Used to encode strings and count the
      number of tokens in them.
    - limit: The maximum number of tokens allowed for the given model, as defined in the MAX_TOKENS dictionary in the
      pr_agent.algo module.
    - prompt_tokens: The number of tokens in the system and user strings, as calculated by the _get_system_user_tokens
      method.
    """

    # Constants
    CLAUDE_MAX_CONTENT_SIZE = 9_000_000 # Maximum allowed content size (9MB) for Claude API

    def __init__(self, pr=None, vars: dict | None = None, system="", user="", model=None):
        """
        Initializes the TokenHandler object.

        Args:
        - pr: The pull request object.
        - vars: A dictionary of variables.
        - system: The system string.
        - user: The user string.
        - model: Optional model name whose tokenizer should be used.
        """
        if vars is None:
            vars = {}
        self.model = model or get_settings().config.model
        self.pr = pr
        self.vars = vars
        self.system = system
        self.user = user
        self.prompt_tokens = 0
        self.encoder = TokenEncoder.get_token_encoder(self.model)

        if pr is not None:
            self.prompt_tokens = self._get_system_user_tokens(pr, self.encoder, vars, system, user)

    def for_model(self, model: str):
        """Return a handler bound to ``model`` without mutating this handler."""
        if model == self.model:
            return self
        return TokenHandler(self.pr, self.vars, self.system, self.user, model=model)

    def _get_system_user_tokens(self, pr, encoder, vars: dict, system, user):
        """
        Calculates the number of tokens in the system and user strings.

        Args:
        - pr: The pull request object.
        - encoder: An object of the encoding_for_model class from the tiktoken module.
        - vars: A dictionary of variables.
        - system: The system string.
        - user: The user string.

        Returns:
        The sum of the number of tokens in the system and user strings.
        """
        try:
            environment = Environment(undefined=StrictUndefined)
            system_prompt = environment.from_string(system).render(vars)
            user_prompt = environment.from_string(user).render(vars)
            system_prompt_tokens = len(encoder.encode(system_prompt, disallowed_special=()))
            user_prompt_tokens = len(encoder.encode(user_prompt, disallowed_special=()))
            return system_prompt_tokens + user_prompt_tokens
        except Exception as e:
            get_logger().error(f"Error in _get_system_user_tokens: {e}")
            return 0

    def _provider_from_model(self) -> str | None:
        """Return the litellm provider key for the configured model, when inferable.

        Mirrors how ``litellm.acount_tokens`` itself resolves the provider from
        the model string: an explicit ``provider/`` prefix wins, then well-known
        bare model names. Cloud providers such as bedrock or vertex rely on
        ambient credentials (set up by PR-Agent for its own requests) and do not
        need a settings key here.
        """
        if "/" in self.model:
            return self.model.split("/", 1)[0].lower()
        model_lower = self.model.lower()
        if "claude" in model_lower:
            return "anthropic"
        if ModelTypeValidator.is_openai_model(model_lower):
            return "openai"
        return None

    def _token_count_api_params(self) -> tuple[str | None, str | None]:
        """Return the request-local (api_key, api_base) for the configured model.

        Reuses the same provider-to-settings mapping that LiteLLMAIHandler uses for
        normal requests, so settings-only keys (which litellm cannot see via process
        environment) reach the provider's token counter.
        """
        from pr_agent.algo.ai_handlers.litellm_ai_handler import PROVIDER_SETTING_PATHS

        provider = self._provider_from_model()
        if provider is None:
            return None, None
        settings = get_settings(use_context=False)
        setting_paths = PROVIDER_SETTING_PATHS.get(provider)
        api_key = settings.get(setting_paths.get("api_key"), None) if setting_paths else None
        api_base = settings.get(setting_paths.get("api_base"), None) if setting_paths else None
        if provider == "openai":
            api_key = api_key or settings.get("OPENAI.KEY", None)
            api_base = (
                api_base
                or settings.get("OPENAI.API_BASE", None)
                or os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("OPENAI_API_BASE")
            )
        return api_key, api_base

    async def _acount_tokens(self, patch: str) -> int:
        """Count tokens through LiteLLM's provider-native counter.

        Uses the configured model (self.model) instead of a hardcoded id, routes
        to the provider-native counter for Anthropic, Bedrock/Vertex Claude,
        Gemini, OpenAI and other keyed providers, and returns 0 when only a
        local estimate would be produced (tokenizer_type == "local_tokenizer")
        or on any error; the caller then applies the estimate factor.
        """
        if len(patch.encode('utf-8')) > self.CLAUDE_MAX_CONTENT_SIZE:
            get_logger().warning(
                "Content too large for provider token counting API, falling back to local estimate"
            )
            return 0

        try:
            import litellm

            api_key, api_base = self._token_count_api_params()
            response = await asyncio.wait_for(
                litellm.acount_tokens(
                    model=self.model,
                    messages=[{
                        "role": "user",
                        "content": patch
                    }],
                    system="system",
                    api_key=api_key,
                    api_base=api_base,
                ),
                timeout=get_settings().get("config.ai_timeout", 120),
            )
        except Exception as e:
            get_logger().error(f"Error in LiteLLM token counting: {e}")
            return 0

        if getattr(response, "tokenizer_type", "local_tokenizer") == "local_tokenizer":
            get_logger().debug(
                f"litellm produced a local token estimate for {self.model}; "
                "applying model_token_count_estimate_factor"
            )
            return 0
        return response.total_tokens

    def _apply_estimation_factor(self, model_name: str, default_estimate: int) -> int:
        raw_factor = get_settings().get("config.model_token_count_estimate_factor", 0)
        try:
            factor = 1 + float(raw_factor)
        except (TypeError, ValueError, OverflowError):
            factor = None
        if factor is None or isinstance(raw_factor, bool) or not factor > 0:
            get_logger().warning(
                f"model_token_count_estimate_factor is not a usable number ({raw_factor!r}), using 1")
            factor = 1
        get_logger().warning(f"{model_name}'s token count cannot be accurately estimated. Using factor of {factor}")

        try:
            return ceil(factor * default_estimate)
        except (OverflowError, ValueError):
            get_logger().warning(
                f"model_token_count_estimate_factor is too large ({raw_factor!r}), using the estimate as is")
            return default_estimate

    def _get_token_count_by_model_type(self, patch: str, default_estimate: int) -> int:
        """
        Get token count based on model type.

        Args:
            patch: The text to count tokens for.
            default_estimate: The default token count estimate.

        Returns:
            int: The calculated token count.
        """
        model_name = str(getattr(self, "model", None) or get_settings().config.model).lower()

        accurate_count = _await_coroutine(self._acount_tokens(patch))
        if accurate_count > 0:
            return accurate_count

        return self._apply_estimation_factor(model_name, default_estimate)

    def count_tokens(self, patch: str, force_accurate: bool = False) -> int:
        """
        Counts the number of tokens in a given patch string.

        Args:
        - patch: The patch string.
        - force_accurate: If True, uses a more precise calculation method.

        Returns:
        The number of tokens in the patch string.
        """
        encoder_estimate = len(self.encoder.encode(patch, disallowed_special=()))

        # If an estimate is enough (for example, where the maximal allowed tokens is
        # way below the known limits), return it.
        if not force_accurate:
            return encoder_estimate

        return self._get_token_count_by_model_type(patch, encoder_estimate)
