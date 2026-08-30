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
from decimal import Decimal, InvalidOperation
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
    # Accumulate costs only when cost output is enabled and LiteLLM can synchronously
    # price a successful response with a positive amount. Use the known-call count to
    # distinguish priced calls from missing pricing data. Retain per-model totals to
    # keep fallback and multi-call runs auditable.
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    known_cost_call_count: int = 0
    model_costs_usd: dict[str, Decimal] = field(default_factory=dict)
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

    @property
    def cost_status(self) -> str:
        """Return whether every, some, or none of the successful calls were priced."""
        if self.known_cost_call_count == 0:
            return "unavailable"
        if self.known_cost_call_count == self.num_ai_calls:
            return "complete"
        return "partial"


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


def _as_decimal_cost(cost_usd) -> Optional[Decimal]:
    """Normalize a positive finite USD value without introducing float math.

    Zero is rejected on purpose: litellm.completion_cost returns 0.0 both for
    zero-priced model entries (e.g. local/ollama models) and for usage without
    billable tokens, so a zero here means "could not be priced", not "free" —
    recording it would render a false "$0.00" with cost status complete.
    """
    if cost_usd is None or isinstance(cost_usd, bool):
        return None
    try:
        cost = cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not cost.is_finite() or cost <= 0:
        return None
    return cost


def record_ai_call(usage=None, model: Optional[str] = None, cost_usd=None) -> None:
    """Count one successful AI call and accumulate usage and known cost."""
    details = get_run_details()
    if details is None:
        return
    details.num_ai_calls += 1
    if usage is not None:
        add_token_usage(usage)
    cost = _as_decimal_cost(cost_usd)
    if cost is not None:
        details.total_cost_usd += cost
        details.known_cost_call_count += 1
        model_name = model or "unknown"
        details.model_costs_usd[model_name] = details.model_costs_usd.get(model_name, Decimal("0")) + cost
