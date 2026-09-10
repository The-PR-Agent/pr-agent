import re
from math import ceil
from threading import Lock

from jinja2 import Environment, StrictUndefined
from tiktoken import encoding_for_model, get_encoding

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


class ModelTypeValidator:
    @staticmethod
    def is_openai_model(model_name: str) -> bool:
        return 'gpt' in model_name or re.match(r"^o[1-9](-mini|-preview)?$", model_name)

    @staticmethod
    def is_anthropic_model(model_name: str) -> bool:
        return 'claude' in model_name


class TokenEncoder:
    _encoder_instance = None
    _model = None
    _lock = Lock()  # Create a lock object
    # Models already warned about, so a fallback chain does not repeat the notice per call.
    _warned_models: set[str] = set()
    # Models whose counts come from the o200k_base fallback rather than their own tokenizer.
    _approximate_models: set[str] = set()

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

    @classmethod
    def _create_encoder(cls, model):
        try:
            if "gpt" in model:
                return encoding_for_model(model)
        except Exception:
            get_logger().warning(f"No tiktoken encoding for '{model}', falling back to o200k_base")
        cls._approximate_models.add(model)
        cls._warn_approximate_tokenizer(model)
        return get_encoding("o200k_base")

    @classmethod
    def is_approximate(cls, model=None) -> bool:
        """Does this model's count come from the o200k_base fallback rather than its own tokenizer?

        Creates the encoder if it has not been created yet, so the answer does not depend on call
        order.
        """
        model = model or get_settings().config.model
        cls.get_token_encoder(model)
        return model in cls._approximate_models

    @classmethod
    def _warn_approximate_tokenizer(cls, model):
        """Note once that this model's token counts are approximations.

        Diff budgeting counts tokens through this encoder, and TokenHandler.count_tokens defaults
        to force_accurate=False, so config.model_token_count_estimate_factor never applies there.
        For a model whose vocabulary differs from o200k_base the budget can therefore run low, and
        an oversized prompt is silently truncated by some self-hosted servers rather than rejected,
        which loses findings with no error. TokenHandler now adds
        config.approximate_token_count_safety_factor of headroom on that path instead of leaving
        the shortfall for the operator to guess at, so this is a notice, not a to-do.
        """
        if model in cls._warned_models:
            return
        cls._warned_models.add(model)
        factor = get_settings().get("config.approximate_token_count_safety_factor", 0)
        get_logger().warning(
            f"Token counts for '{model}' are approximated with the o200k_base tokenizer; the diff "
            f"budget adds {factor} of headroom (config.approximate_token_count_safety_factor). "
            f"Lower config.max_model_tokens as well if the server still rejects or truncates.")


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
    CLAUDE_MODEL = "claude-3-7-sonnet-20250219"
    CLAUDE_MAX_CONTENT_SIZE = 9_000_000 # Maximum allowed content size (9MB) for Claude API

    def __init__(self, pr=None, vars: dict = {}, system="", user="", model=None):
        """
        Initializes the TokenHandler object.

        Args:
        - pr: The pull request object.
        - vars: A dictionary of variables.
        - system: The system string.
        - user: The user string.
        - model: Optional model name whose tokenizer should be used.
        """
        self.encoder = TokenEncoder.get_token_encoder(model)
        # An approximate encoder under-counts as easily as it over-counts, and the budgeting path
        # asks for a plain estimate, so the headroom has to be added here.
        self.approximate_encoder = TokenEncoder.is_approximate(model)

        if pr is not None:
            self.prompt_tokens = self._get_system_user_tokens(pr, self.encoder, vars, system, user)

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
            system_prompt_tokens = len(encoder.encode(system_prompt))
            user_prompt_tokens = len(encoder.encode(user_prompt))
            return system_prompt_tokens + user_prompt_tokens
        except Exception as e:
            get_logger().error(f"Error in _get_system_user_tokens: {e}")
            return 0

    def _calc_claude_tokens(self, patch: str) -> int:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=get_settings(use_context=False).get('anthropic.key'))

            if len(patch.encode('utf-8')) > self.CLAUDE_MAX_CONTENT_SIZE:
                get_logger().warning(
                    "Content too large for Anthropic token counting API, falling back to local tokenizer"
                )
                return 0

            response = client.messages.count_tokens(
                model=self.CLAUDE_MODEL,
                system="system",
                messages=[{
                    "role": "user",
                    "content": patch
                }],
            )
            return response.input_tokens

        except Exception as e:
            get_logger().error(f"Error in Anthropic token counting: {e}")
            return 0

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
        model_name = get_settings().config.model.lower()

        if ModelTypeValidator.is_openai_model(model_name) and get_settings(use_context=False).get('openai.key'):
            return default_estimate

        if ModelTypeValidator.is_anthropic_model(model_name) and get_settings(use_context=False).get('anthropic.key'):
            claude_count = self._calc_claude_tokens(patch)
            if claude_count > 0:
                return claude_count
            return self._apply_estimation_factor(model_name, default_estimate)

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

        # If an estimate is enough (for example, in cases where the maximal allowed tokens is way below the known limits), return it.
        if not force_accurate:
            return self._add_approximation_headroom(encoder_estimate)

        return self._get_token_count_by_model_type(patch, encoder_estimate)

    def _add_approximation_headroom(self, estimate: int) -> int:
        """Inflate an estimate produced by a tokenizer that is not the model's own.

        Only for the o200k_base fallback: a model with its own encoding is counted exactly, and
        inflating it would shrink the diff for no reason.
        """
        if not getattr(self, "approximate_encoder", False):
            return estimate
        raw_factor = get_settings().get("config.approximate_token_count_safety_factor", 0)
        try:
            factor = float(raw_factor)
        except (TypeError, ValueError, OverflowError):
            factor = 0
        if isinstance(raw_factor, bool) or not factor > 0:
            return estimate
        try:
            return ceil(estimate * (1 + factor))
        except (OverflowError, ValueError):
            return estimate
