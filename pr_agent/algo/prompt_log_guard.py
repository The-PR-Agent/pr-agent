"""Share request-scoped suppression of instruction-bearing logs."""

from contextvars import ContextVar

_PROMPT_LOGS_WITHHELD: ContextVar[bool] = ContextVar("pr_agent_prompt_logs_withheld", default=False)


def prompt_logs_withheld() -> bool:
    """Report whether request-local prompt and response details must be withheld."""
    return _PROMPT_LOGS_WITHHELD.get()


def sanitized_exception_location(error: BaseException) -> str:
    """Identify a failure's code location without exposing its message or traceback locals."""
    tb = error.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    frame = tb.tb_frame
    return f"{frame.f_globals.get('__name__', 'unknown')}.{frame.f_code.co_name}:{tb.tb_lineno}"


def sanitized_exception_summary(error: BaseException) -> str:
    """Describe an exception without exposing its message or traceback details."""
    return f"{type(error).__name__} at {sanitized_exception_location(error)} (details withheld)"


_SAFE_PROVIDER_ERROR_CODES = frozenset({
    "authentication_error",
    "context_length_exceeded",
    "insufficient_quota",
    "invalid_api_key",
    "invalid_request_error",
    "model_not_found",
    "rate_limit_exceeded",
})


def sanitized_provider_error(error: BaseException) -> str:
    """Expose safe structured provider details without disclosing error messages."""
    parts = [type(error).__name__]
    status_code = getattr(error, "status_code", None)
    if type(status_code) is int and 100 <= status_code <= 599:
        parts.append(f"HTTP {status_code}")
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _SAFE_PROVIDER_ERROR_CODES:
        parts.append(f"code={code}")
    return ", ".join(parts)
