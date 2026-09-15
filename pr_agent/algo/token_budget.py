from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import get_max_tokens
from pr_agent.log import get_logger


def _positive_int(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _configured_positive_int(value) -> int | None:
    try:
        output_tokens = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return output_tokens if output_tokens > 0 else None


@dataclass(frozen=True)
class AttemptTokenBudget:
    """Token accounting and output headroom for one attempted model."""

    model: str
    source_token_handler: object
    token_handler: object
    context_window: int
    output_token_reserve: Callable[[str, int], int] | None = None
    output_token_limit: Callable[[str], int] | None = None
    configured_output_tokens: object = None
    prompt_normalizer: Callable[[str, str, str], tuple[str, str]] | None = None

    @classmethod
    def for_attempt(
        cls,
        model: str,
        token_handler,
        *,
        output_token_reserve=None,
        output_token_limit=None,
        configured_output_tokens=None,
        prompt_normalizer=None,
        ignore_max_model_tokens: bool = False,
    ) -> AttemptTokenBudget:
        """Create an immutable budget whose tokenizer and window belong to ``model``."""
        bound_token_handler = (
            token_handler.for_model(model)
            if isinstance(token_handler, TokenHandler)
            else token_handler
        )
        return cls(
            model=model,
            source_token_handler=token_handler,
            token_handler=bound_token_handler,
            context_window=get_max_tokens(model, ignore_max_model_tokens=ignore_max_model_tokens),
            output_token_reserve=output_token_reserve,
            output_token_limit=output_token_limit,
            configured_output_tokens=configured_output_tokens,
            prompt_normalizer=prompt_normalizer,
        )

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

        if resolved is None and callable(self.output_token_limit):
            try:
                resolved = _positive_int(self.output_token_limit(self.model))
            except Exception as error:
                get_logger().debug(f"Failed to resolve the output token limit for {self.model}: {error}")

        if resolved is None:
            resolved = _configured_positive_int(self.configured_output_tokens)
        if resolved is None:
            resolved = default_output_tokens
        if preserve_minimum:
            resolved = max(resolved, default_output_tokens)
        return resolved

    def input_limit(self, default_output_tokens: int, *, preserve_minimum: bool = False) -> int:
        """Return the maximum complete input size for this attempt."""
        output_reserve = self.resolve_output_reserve(
            default_output_tokens,
            preserve_minimum=preserve_minimum,
        )
        return max(self.context_window - output_reserve, 0)

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
            default_output_tokens, preserve_minimum=preserve_minimum,
        )
        available = self.context_window - output_reserve - fixed_prompt_tokens
        return max(available, 0) if clamp else available

    def count_tokens(self, text: str, *, force_accurate: bool = False) -> int:
        """Count text with the tokenizer bound to this attempt."""
        if force_accurate:
            return self.token_handler.count_tokens(text, force_accurate=True)
        return self.token_handler.count_tokens(text)

    def normalize_and_count_messages(self, system: str, user: str) -> tuple[str, str, int]:
        """Return the exact normalized prompt pair and its model-attempt token count."""
        normalized_system, normalized_user = system, user
        if callable(self.prompt_normalizer):
            try:
                normalized = self.prompt_normalizer(self.model, system, user)
            except Exception as error:
                get_logger().debug(f"Failed to normalize prompts for {self.model}: {error}")
            else:
                if (
                    isinstance(normalized, tuple)
                    and len(normalized) == 2
                    and all(isinstance(prompt, str) for prompt in normalized)
                ):
                    normalized_system, normalized_user = normalized
                else:
                    get_logger().debug(f"Ignoring unusable prompt normalization result for {self.model}")

        count_messages = getattr(self.token_handler, "count_messages", None)
        if callable(count_messages):
            token_count = count_messages(normalized_system, normalized_user)
            if isinstance(token_count, int) and not isinstance(token_count, bool) and token_count > 0:
                return normalized_system, normalized_user, token_count

        token_count = TokenHandler(model=self.model).count_messages(normalized_system, normalized_user)
        return normalized_system, normalized_user, token_count

    def matches(self, model: str, source_token_handler: object) -> bool:
        """Return whether prepared data belongs to the same model and source handler."""
        return self.model == model and (
            source_token_handler is self.source_token_handler
            or source_token_handler is self.token_handler
        )
