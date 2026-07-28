"""Per-run metadata collected while a PR-Agent command executes.

The data is held in a ``ContextVar`` so that the AI handler can record token
usage without changing ``chat_completion``'s return signature. Context vars are
copied into ``asyncio`` child tasks while still referencing the same mutable
``RunMetadata`` object, so concurrent AI calls accumulate into one instance and
stay isolated between concurrent requests.
"""

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

_run_metadata: ContextVar[Optional["RunMetadata"]] = ContextVar(
    "pr_agent_run_metadata", default=None
)


@dataclass
class RunMetadata:
    model_used: Optional[str] = None
    fallback_used: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    num_ai_calls: int = 0
    start_time: float = field(default_factory=time.monotonic)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.start_time)

    @property
    def has_token_usage(self) -> bool:
        return (
            self.total_tokens > 0
            or self.prompt_tokens > 0
            or self.completion_tokens > 0
        )


def init_run_metadata() -> RunMetadata:
    """Install a fresh collector for the current run and return it."""
    metadata = RunMetadata()
    _run_metadata.set(metadata)
    return metadata


def get_run_metadata() -> Optional[RunMetadata]:
    """Return the collector for the current run, or None if not initialized."""
    return _run_metadata.get()


def record_model_used(model: str, is_fallback: bool) -> None:
    """Record the model that produced a successful completion."""
    metadata = get_run_metadata()
    if metadata is None:
        return
    metadata.model_used = model
    if is_fallback:
        # sticky: later primary success must not hide that a fallback ran
        metadata.fallback_used = True


def _read_token_field(usage, name: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    return value if isinstance(value, int) else 0


def add_token_usage(usage) -> None:
    """Accumulate token counts from a litellm usage object or dict."""
    metadata = get_run_metadata()
    if metadata is None or usage is None:
        return
    prompt_tokens = _read_token_field(usage, "prompt_tokens")
    completion_tokens = _read_token_field(usage, "completion_tokens")
    total_tokens = _read_token_field(usage, "total_tokens") or (
        prompt_tokens + completion_tokens
    )
    metadata.prompt_tokens += prompt_tokens
    metadata.completion_tokens += completion_tokens
    metadata.total_tokens += total_tokens


def record_ai_call(usage=None) -> None:
    """Count one AI call and accumulate token usage when available."""
    metadata = get_run_metadata()
    if metadata is None:
        return
    metadata.num_ai_calls += 1
    add_token_usage(usage)
