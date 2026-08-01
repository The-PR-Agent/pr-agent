"""Details of a single PR-Agent run, collected while the command executes.

The data is held in a ``ContextVar`` so that the AI handler can record token
usage without changing ``chat_completion``'s return signature. Context vars are
copied into ``asyncio`` child tasks while still referencing the same mutable
``RunDetails`` object, so concurrent AI calls accumulate into one instance and
stay isolated between concurrent requests.
"""

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

_run_details: ContextVar[Optional["RunDetails"]] = ContextVar(
    "pr_agent_run_details", default=None
)


@dataclass
class RunDetails:
    """Counters and identifiers accumulated over a single command run.

    Every field is filled opportunistically: whatever the provider does not report
    stays at its default, and the renderer omits the corresponding line rather than
    displaying a zero.
    """

    # Model that produced the answer, which differs from `config.model` when a fallback
    # took over. Stays None when no prediction succeeded, which the renderer reads as
    # "nothing worth showing".
    model_used: Optional[str] = None
    # Sticky: once a fallback has won, a later success on the primary model must not
    # clear this, or the comment would hide that a fallback ran at all.
    fallback_used: bool = False
    # Input/output tokens summed over every AI call of the run. Named after litellm's
    # normalized usage object, which is what the collector reads. Both stay 0 when no usage
    # reaches the collector, e.g. streaming responses or the langchain handler.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Provider-reported total when available, otherwise derived from prompt + completion.
    # Counts failed fallback attempts as well, so it reflects what the run really cost,
    # while `model_used` names only the model behind the final answer.
    total_tokens: int = 0
    # Successful LLM invocations, counted even when their token usage is unavailable.
    num_ai_calls: int = 0
    # Monotonic reference taken when the collector is installed, i.e. at the top of the
    # tool's run(). Monotonic so that wall-clock adjustments cannot yield a negative duration.
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


def init_run_details() -> RunDetails:
    """Install a fresh collector for the current run and return it."""
    details = RunDetails()
    _run_details.set(details)
    return details


def get_run_details() -> Optional[RunDetails]:
    """Return the collector for the current run, or None if not initialized."""
    return _run_details.get()


def record_model_used(model: str, is_fallback: bool) -> None:
    """Record the model that produced a successful completion."""
    details = get_run_details()
    if details is None:
        return
    details.model_used = model
    if is_fallback:
        # sticky: later primary success must not hide that a fallback ran
        details.fallback_used = True


def _read_token_field(usage, name: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    return value if isinstance(value, int) else 0


def add_token_usage(usage) -> None:
    """Accumulate token counts from a litellm usage object or dict."""
    details = get_run_details()
    if details is None or usage is None:
        return
    prompt_tokens = _read_token_field(usage, "prompt_tokens")
    completion_tokens = _read_token_field(usage, "completion_tokens")
    total_tokens = _read_token_field(usage, "total_tokens") or (
        prompt_tokens + completion_tokens
    )
    details.prompt_tokens += prompt_tokens
    details.completion_tokens += completion_tokens
    details.total_tokens += total_tokens


def record_ai_call(usage=None) -> None:
    """Count one AI call and accumulate token usage when available."""
    details = get_run_details()
    if details is None:
        return
    details.num_ai_calls += 1
    if usage is not None:
        add_token_usage(usage)
