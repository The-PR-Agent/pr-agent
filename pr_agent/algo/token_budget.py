from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.token_handler import TokenEncoder, TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

MESSAGE_FRAMING_TOKEN_ALLOWANCE = 16
REPLY_FRAMING_TOKEN_ALLOWANCE = 16
DEFAULT_TRUNCATION_MARKER = "\n...(truncated)\n"


class FallbackEligibleError(ValueError):
    """Represent a model-specific output or fit failure that another model may resolve."""


def _positive_int(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _non_negative_int(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _as_int(value, default: int = 0) -> int:
    """Coerce a settings value to int, tolerating the quoted numbers TOML allows."""
    try:
        return int(value)
    except (TypeError, ValueError):
        get_logger().warning(f"Expected a number in configuration, got {value!r}; using {default}")
        return default


def get_max_tokens(model, ignore_max_model_tokens=False):
    """
    Get the maximum number of tokens allowed for a model.
    logic:
    (1) If the model is in './pr_agent/algo/__init__.py', use the value from there.
    (2) else if 'config.custom_model_max_tokens' is set to a positive value, use it.
    (3) else if it is a GPT-5.x _thinking alias registered under its base name, use that value.
    (4) else, query LiteLLM for provider-qualified and bare alias bases before the original model.
    (5) else, raise an error.

    For all cases, we further limit the number of tokens to 'config.max_model_tokens' if it is set.
    This aims to improve the algorithmic quality, as the AI model degrades in performance when the input is too long.
    Pass ignore_max_model_tokens=True to keep the unreduced value, for sites that deliberately use the
    raw model context size rather than the conservative clamp.
    """
    settings = get_settings()
    custom_max_tokens = _as_int(settings.config.custom_model_max_tokens)
    # Resolve GPT-6 Astra aliases before diff token accounting, just as the handler does.
    # Preserve explicit custom limits for provider aliases that were not in the registry.
    model_base = model
    while model_base.startswith(("openai/", "azure/")):
        model_base = model_base.removeprefix("openai/").removeprefix("azure/")
    if custom_max_tokens <= 0 and model_base.removesuffix("_thinking") == "gpt-6-astra":
        model = "gpt-6-astra"
    # Normalize GPT-5.x _thinking aliases before token-limit lookup to match
    # LiteLLMAIHandler request normalization.
    model_for_max_tokens = model
    litellm_lookup_models = (model,)
    if isinstance(model, str):
        tmp = model
        while tmp.startswith(("openai/", "azure/")):
            tmp = tmp.removeprefix("openai/").removeprefix("azure/")
        if tmp.startswith("gpt-5") and "_thinking" in tmp:
            model_for_max_tokens = tmp.replace("_thinking", "")
            settings_get = getattr(settings, "get", None)
            azure_mode = callable(settings_get) and (
                settings_get("OPENAI.API_TYPE", None) == "azure"
                or bool(settings_get("AZURE_AD.CLIENT_ID", None))
            )
            if azure_mode or model.startswith("azure/"):
                provider_prefix = "azure/"
            else:
                provider_prefix = "openai/"
            provider_model = provider_prefix + model_for_max_tokens
            litellm_lookup_models = tuple(dict.fromkeys((provider_model, model_for_max_tokens, model)))
    if model in MAX_TOKENS:
        max_tokens_model = MAX_TOKENS[model]
    elif custom_max_tokens > 0:
        max_tokens_model = custom_max_tokens
    elif model_for_max_tokens in MAX_TOKENS:
        max_tokens_model = MAX_TOKENS[model_for_max_tokens]
    else:
        # Fallback: ask LiteLLM for the model's metadata before giving up.
        max_tokens_model = 0
        import litellm
        # Try provider-qualified and bare bases before the raw alias.
        for lookup_model in litellm_lookup_models:
            try:
                model_info = litellm.get_model_info(lookup_model)
            except Exception:
                get_logger().debug(f"litellm.get_model_info could not resolve model '{lookup_model}'")
                model_info = None
            if model_info:
                litellm_max = model_info.get("max_input_tokens")
                try:
                    parsed_max_tokens = int(litellm_max)
                except (TypeError, ValueError, OverflowError):
                    continue
                if parsed_max_tokens > 0:
                    max_tokens_model = parsed_max_tokens
                    get_logger().debug(
                        f"Resolved max_input_tokens for '{model}' from litellm "
                        f"(lookup '{lookup_model}'): {max_tokens_model}"
                    )
                    break

        if max_tokens_model <= 0:
            get_logger().error(
                f"Model {model} is not defined in MAX_TOKENS in ./pr_agent/algo/__init__.py"
                f" and no custom_model_max_tokens is set"
            )
            raise Exception(
                f"Ensure {model} is defined in MAX_TOKENS in ./pr_agent/algo/__init__.py"
                f" or set a positive value for it in config.custom_model_max_tokens"
            )

    max_model_tokens = _as_int(settings.config.max_model_tokens) if settings.config.max_model_tokens else 0
    if max_model_tokens > 0 and not ignore_max_model_tokens:
        max_tokens_model = min(max_model_tokens, max_tokens_model)
    return max_tokens_model


def clip_tokens(text: str, max_tokens: int, add_three_dots=True, num_input_tokens=None, delete_last_line=False) -> str:
    """
    Clip the number of tokens in a string to a maximum number of tokens.

    This function limits text to a specified token count by calculating the approximate
    character-to-token ratio and truncating the text accordingly. A safety factor of 0.9
    (10% reduction) is applied to ensure the result stays within the token limit.

    Args:
        text (str): The string to clip. If empty or None, returns the input unchanged.
        max_tokens (int): The maximum number of tokens allowed in the string.
                         If negative, returns an empty string.
        add_three_dots (bool, optional): Whether to add "\\n...(truncated)" at the end
                                       of the clipped text to indicate truncation.
                                       Defaults to True.
        num_input_tokens (int, optional): Pre-computed number of tokens in the input text.
                                        If provided, skips token encoding step for efficiency.
                                        If None, tokens will be counted using TokenEncoder.
                                        Defaults to None.
        delete_last_line (bool, optional): Whether to remove the last line from the
                                         clipped content before adding truncation indicator.
                                         Useful for ensuring clean breaks at line boundaries.
                                         Defaults to False.

    Returns:
        str: The clipped string. Returns original text if:
             - Text is empty/None
             - Token count is within limit
             - An error occurs during processing

             Returns empty string if max_tokens <= 0.

    Examples:
        Basic usage:
        >>> text = "This is a sample text that might be too long"
        >>> result = clip_tokens(text, max_tokens=10)
        >>> print(result)
        This is a sample...
        (truncated)

        Without truncation indicator:
        >>> result = clip_tokens(text, max_tokens=10, add_three_dots=False)
        >>> print(result)
        This is a sample

        With pre-computed token count:
        >>> result = clip_tokens(text, max_tokens=5, num_input_tokens=15)
        >>> print(result)
        This...
        (truncated)

        With line deletion:
        >>> multiline_text = "Line 1\\nLine 2\\nLine 3"
        >>> result = clip_tokens(multiline_text, max_tokens=3, delete_last_line=True)
        >>> print(result)
        Line 1
        Line 2
        ...
        (truncated)

    Notes:
        The function uses a safety factor of 0.9 (10% reduction) to ensure the
        result stays within the token limit, as character-to-token ratios can vary.
        If token encoding fails, the original text is returned with a warning logged.
    """
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError, OverflowError):
        get_logger().warning(
            f"clip_tokens got a non-numeric max_tokens ({max_tokens!r}); returning the text "
            f"unclipped, which may exceed the model's context window")
        return text

    if not text:
        return text
    if max_tokens <= 0:
        return ""

    try:
        if num_input_tokens is None:
            encoder = TokenEncoder.get_token_encoder()
            num_input_tokens = len(encoder.encode(text, disallowed_special=()))
        if num_input_tokens <= max_tokens:
            return text

        # calculate the number of characters to keep
        num_chars = len(text)
        chars_per_token = num_chars / num_input_tokens
        factor = 0.9  # reduce by 10% to be safe
        num_output_chars = int(factor * chars_per_token * max_tokens)

        # clip the text
        if num_output_chars > 0:
            clipped_text = text[:num_output_chars]
            if delete_last_line:
                clipped_text = clipped_text.rsplit("\n", 1)[0]
            if add_three_dots:
                clipped_text += "\n...(truncated)"
        else:  # text is empty
            clipped_text = ""

        return clipped_text
    except Exception as e:
        get_logger().warning(f"Failed to clip tokens: {e}")
        return text


@dataclass(frozen=True)
class FittedPrompt:
    """Represent a normalized prompt pair whose optional text fits one model attempt."""

    optional_text: str
    system_prompt: str
    user_prompt: str
    input_tokens: int


@dataclass(frozen=True)
class AttemptTokenBudget:
    """Track token accounting and output headroom for one attempted model."""

    model: str
    source_token_handler: object
    token_handler: object
    context_window: int
    output_token_reserve: Callable[[str, int], int] | None = None

    @classmethod
    def for_attempt(
        cls,
        model: str,
        token_handler,
        *,
        output_token_reserve=None,
        ignore_max_model_tokens: bool = False,
    ) -> AttemptTokenBudget:
        """Create an immutable budget whose tokenizer and window belong to ``model``."""
        bound_token_handler = (
            token_handler.for_model(model) if isinstance(token_handler, TokenHandler) else token_handler
        )
        return cls(
            model=model,
            source_token_handler=token_handler,
            token_handler=bound_token_handler,
            context_window=get_max_tokens(model, ignore_max_model_tokens=ignore_max_model_tokens),
            output_token_reserve=output_token_reserve,
        )

    @classmethod
    def for_prompt_attempt(
        cls,
        model: str,
        pr,
        variables: dict,
        system_template: str,
        user_template: str,
        *,
        ai_handler,
        image_path: str | None = None,
        output_token_reserve=None,
        ignore_max_model_tokens: bool = False,
    ) -> AttemptTokenBudget:
        """Create a model-bound budget from the exact fixed request fields."""
        token_handler = TokenHandler(
            pr,
            variables,
            system_template,
            user_template,
            model=model,
        )
        budget = cls.for_attempt(
            model,
            token_handler,
            output_token_reserve=output_token_reserve,
            ignore_max_model_tokens=ignore_max_model_tokens,
        )
        system_prompt, user_prompt = budget.render_prompt_templates(variables)
        prepared = budget.prepare_request(
            ai_handler,
            system_prompt,
            user_prompt,
            image_path=image_path,
        )
        token_handler.prompt_tokens = prepared.input_tokens
        return budget

    @property
    def prompt_tokens(self) -> int:
        prompt_tokens = getattr(self.token_handler, "prompt_tokens", 0)
        if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) and prompt_tokens >= 0:
            return prompt_tokens
        return 0

    def resolve_output_reserve(
        self,
        default_output_tokens: int,
        *,
        preserve_minimum: bool = False,
    ) -> int:
        """Resolve output headroom without flattening consumer-specific minimums."""
        resolved = None
        if callable(self.output_token_reserve):
            try:
                resolved = _positive_int(self.output_token_reserve(self.model, default_output_tokens))
            except Exception as error:
                get_logger().debug(f"Failed to resolve the output token reserve for {self.model}: {error}")

        if resolved is None:
            resolved = default_output_tokens
        if preserve_minimum:
            resolved = max(resolved, default_output_tokens)
        return resolved

    def available_tokens(
        self,
        default_output_tokens: int,
        *,
        preserve_minimum: bool = False,
        prompt_tokens: int | None = None,
        clamp: bool = True,
    ) -> int:
        """Return remaining input capacity; raw mode preserves packers' strict hard stops."""
        fixed_prompt_tokens = self.prompt_tokens if prompt_tokens is None else prompt_tokens
        output_reserve = self.resolve_output_reserve(
            default_output_tokens,
            preserve_minimum=preserve_minimum,
        )
        available = self.context_window - output_reserve - fixed_prompt_tokens
        return max(available, 0) if clamp else available

    def input_token_limit(
        self,
        default_output_tokens: int,
        *,
        preserve_minimum: bool = False,
        additional_input_reserve: int = 0,
    ) -> int:
        """Return the complete request-input limit for this attempt."""
        output_reserve = self.resolve_output_reserve(
            default_output_tokens,
            preserve_minimum=preserve_minimum,
        )
        if not isinstance(additional_input_reserve, int) or isinstance(additional_input_reserve, bool):
            additional_input_reserve = 0
        return max(self.context_window - output_reserve - max(additional_input_reserve, 0), 0)

    def require_input_capacity(
        self,
        default_output_tokens: int,
        *,
        preserve_minimum: bool = False,
    ) -> int:
        """Return remaining input capacity or fail this model attempt."""
        available = self.available_tokens(
            default_output_tokens,
            preserve_minimum=preserve_minimum,
            clamp=False,
        )
        if available <= 0:
            raise FallbackEligibleError(f"The required prompt leaves no input capacity for {self.model}")
        return available

    def count_tokens(self, text: str, *, force_accurate: bool = False) -> int:
        """Count text with the tokenizer bound to this attempt."""
        if force_accurate:
            return self.token_handler.count_tokens(text, force_accurate=True)
        return self.token_handler.count_tokens(text)

    def normalize_request_prompts(
        self,
        ai_handler,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, str]:
        """Return the prompt strings that the active handler will dispatch."""
        normalize = getattr(ai_handler, "normalize_request_prompts", None)
        if not callable(normalize):
            return system_prompt, user_prompt
        try:
            normalized = normalize(self.model, system_prompt, user_prompt)
        except Exception as error:
            get_logger().debug(f"Failed to normalize prompts for {self.model}: {error}")
            return system_prompt, user_prompt
        if (
            isinstance(normalized, tuple)
            and len(normalized) == 2
            and all(isinstance(prompt, str) for prompt in normalized)
        ):
            return normalized
        get_logger().debug(f"Ignoring unusable prompt normalization result for {self.model}")
        return system_prompt, user_prompt

    def render_prompt_templates(self, variables: dict) -> tuple[str, str]:
        """Render this attempt handler's templates with the supplied variables."""
        # Preserve plain-text model input in a sandbox; HTML escaping would corrupt code and diffs.
        environment = SandboxedEnvironment(undefined=StrictUndefined)
        system_prompt = environment.from_string(self.token_handler.system).render(variables)
        user_prompt = environment.from_string(self.token_handler.user).render(variables)
        return system_prompt, user_prompt

    def count_request_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image_path: str | None = None,
        messages: list[dict] | None = None,
    ) -> int:
        """Count the final request with the attempt tokenizer, framing, and image input."""
        if messages is None:
            user_content = user_prompt
            if image_path:
                user_content = [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_path}},
                ]
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        content_tokens = 0
        image_count = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                content_tokens += self.count_tokens(content)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    content_tokens += self.count_tokens(block["text"])
                elif block.get("type") == "image_url":
                    image_url = block.get("image_url")
                    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                        content_tokens += self.count_tokens(image_url["url"])
                    image_count += 1
        request_tokens = (
            content_tokens
            + MESSAGE_FRAMING_TOKEN_ALLOWANCE * len(messages)
            + REPLY_FRAMING_TOKEN_ALLOWANCE
        )
        if image_count:
            raw_allowance = get_settings().get("config.image_input_token_allowance")
            image_allowance = _non_negative_int(raw_allowance)
            if image_allowance is None:
                raise ValueError(
                    "config.image_input_token_allowance must be a non-negative integer"
                )
            request_tokens += image_count * image_allowance
        return request_tokens

    def prepare_request(
        self,
        ai_handler,
        system_prompt: str,
        user_prompt: str,
        *,
        optional_text: str = "",
        image_path: str | None = None,
    ) -> FittedPrompt:
        """Normalize and count the exact prompt pair that will be dispatched."""
        system_prompt, user_prompt = self.normalize_request_prompts(
            ai_handler,
            system_prompt,
            user_prompt,
        )
        messages = None
        build_messages = getattr(ai_handler, "build_request_messages", None)
        if callable(build_messages):
            messages = build_messages(
                self.model,
                system_prompt,
                user_prompt,
                image_path=image_path,
            )
        return FittedPrompt(
            optional_text=optional_text,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            input_tokens=self.count_request_tokens(
                system_prompt,
                user_prompt,
                image_path=image_path,
                messages=messages,
            ),
        )

    def fit_optional_text(
        self,
        optional_text: str,
        render: Callable[[str], tuple[str, str]],
        *,
        ai_handler,
        default_output_tokens: int,
        preserve_minimum: bool = False,
        additional_input_reserve: int = 0,
        image_path: str | None = None,
        keep: Literal["prefix", "suffix"] = "prefix",
        truncation_marker: str = DEFAULT_TRUNCATION_MARKER,
    ) -> FittedPrompt:
        """Fit one optional prompt field and verify the exact normalized request."""
        if keep not in {"prefix", "suffix"}:
            raise ValueError(f"Unsupported optional-text retention policy: {keep}")

        input_limit = self.input_token_limit(
            default_output_tokens,
            preserve_minimum=preserve_minimum,
            additional_input_reserve=additional_input_reserve,
        )

        def prepare(candidate: str) -> FittedPrompt:
            system_prompt, user_prompt = render(candidate)
            return self.prepare_request(
                ai_handler,
                system_prompt,
                user_prompt,
                optional_text=candidate,
                image_path=image_path,
            )

        full_prompt = prepare(optional_text)
        if full_prompt.input_tokens <= input_limit:
            return full_prompt

        empty_prompt = prepare("")
        if empty_prompt.input_tokens > input_limit:
            raise FallbackEligibleError(f"The required prompt exceeds the token limit for {self.model}")
        if not optional_text:
            return empty_prompt

        marker_prompt = prepare(truncation_marker)
        if marker_prompt.input_tokens > input_limit:
            raise FallbackEligibleError(
                f"The truncation marker does not fit the token limit for {self.model}"
            )
        best_prompt = marker_prompt

        encoder = getattr(self.token_handler, "encoder", None)
        encode = getattr(encoder, "encode", None)
        decode = getattr(encoder, "decode", None)
        if callable(encode) and callable(decode):
            encoded = encode(optional_text, disallowed_special=())

            def retain(count: int) -> str:
                retained = encoded[-count:] if keep == "suffix" else encoded[:count]
                return decode(retained)
        else:
            encoded = list(optional_text)

            def retain(count: int) -> str:
                retained = encoded[-count:] if keep == "suffix" else encoded[:count]
                return "".join(retained)

        # Start near the available token capacity, then use each exact count to
        # reduce the retained token slice. Avoid assuming that rendered BPE counts
        # are monotonic in the number of source characters.
        marker_capacity = max(input_limit - marker_prompt.input_tokens, 0)
        keep_tokens = min(len(encoded) - 1, marker_capacity)
        while keep_tokens > 0:
            retained_text = retain(keep_tokens)
            if keep == "suffix":
                candidate = truncation_marker + retained_text.lstrip()
            else:
                candidate = retained_text.rstrip() + truncation_marker
            candidate_prompt = prepare(candidate)
            if candidate_prompt.input_tokens <= input_limit:
                best_prompt = candidate_prompt
                break
            candidate_cost = max(
                candidate_prompt.input_tokens - marker_prompt.input_tokens,
                1,
            )
            scaled_keep = keep_tokens * marker_capacity // candidate_cost
            keep_tokens = min(keep_tokens - 1, scaled_keep)

        if best_prompt.input_tokens > input_limit:
            raise FallbackEligibleError(f"Failed to fit the optional prompt text for {self.model}")
        return best_prompt

    def fit_prompt_variable(
        self,
        variables: dict,
        variable_name: str,
        optional_text: str,
        *,
        ai_handler,
        default_output_tokens: int,
        preserve_minimum: bool = False,
        additional_input_reserve: int = 0,
        image_path: str | None = None,
        keep: Literal["prefix", "suffix"] = "prefix",
        truncation_marker: str = DEFAULT_TRUNCATION_MARKER,
    ) -> FittedPrompt:
        """Fit one prompt variable without mutating retry-shared state."""
        environment = SandboxedEnvironment(undefined=StrictUndefined)
        system_template = environment.from_string(self.token_handler.system)
        user_template = environment.from_string(self.token_handler.user)

        def render(candidate: str) -> tuple[str, str]:
            attempt_variables = variables.copy()
            attempt_variables[variable_name] = candidate
            return system_template.render(attempt_variables), user_template.render(attempt_variables)

        return self.fit_optional_text(
            optional_text,
            render,
            ai_handler=ai_handler,
            default_output_tokens=default_output_tokens,
            preserve_minimum=preserve_minimum,
            additional_input_reserve=additional_input_reserve,
            image_path=image_path,
            keep=keep,
            truncation_marker=truncation_marker,
        )

    def matches(self, model: str, source_token_handler: object) -> bool:
        """Return whether prepared data belongs to the same model and source handler."""
        return self.model == model and (
            source_token_handler is self.source_token_handler or source_token_handler is self.token_handler
        )
