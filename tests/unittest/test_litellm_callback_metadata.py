import litellm
import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.log import get_logger

OTHER_PR = "https://github.com/other-org/other-repo/pull/999"


@pytest.fixture
def langfuse_callback():
    original = litellm.success_callback
    litellm.success_callback = ["langfuse"]
    yield
    litellm.success_callback = original


def test_metadata_is_not_taken_from_a_concurrent_request(langfuse_callback):
    """Ignore a concurrent request's log record when building this call's trace metadata:
    add_litellm_callbacks attaches a global loguru sink that sees every request."""
    fired = {"n": 0}

    def foreign_request(message):
        if fired["n"] == 0 and "litellm callbacks" in message.record["message"]:
            fired["n"] = 1
            with get_logger().contextualize(command="describe", pr_url=OTHER_PR):
                get_logger().debug("log line emitted by another in-flight request")

    handler_id = get_logger().add(foreign_request)
    try:
        kwargs = LiteLLMAIHandler.add_litellm_callbacks(object.__new__(LiteLLMAIHandler), {})
    finally:
        get_logger().remove(handler_id)

    assert fired["n"] == 1, "the foreign request never logged inside the capture window"
    trace_metadata = (kwargs.get("metadata") or {}).get("trace_metadata") or {}
    assert trace_metadata.get("pr_url", "unknown") == "unknown"
    assert trace_metadata.get("command", "unknown") == "unknown"


def test_metadata_uses_this_requests_own_context(langfuse_callback):
    with get_logger().contextualize(command="review", pr_url="https://github.com/o/r/pull/1"):
        kwargs = LiteLLMAIHandler.add_litellm_callbacks(object.__new__(LiteLLMAIHandler), {})

    trace_metadata = (kwargs.get("metadata") or {}).get("trace_metadata") or {}
    assert trace_metadata.get("command") == "review"
    assert trace_metadata.get("pr_url") == "https://github.com/o/r/pull/1"
