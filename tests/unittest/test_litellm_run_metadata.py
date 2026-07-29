from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ai_handlers.litellm_helpers import MockResponse
from pr_agent.algo.run_metadata import get_run_metadata, init_run_metadata
from tests.unittest._run_metadata_test_helpers import isolate_run_metadata  # noqa: F401


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _Response:
    """Minimal stand-in for a litellm response object."""

    def __init__(self, usage):
        self.usage = usage

    def dict(self):
        return {"choices": [{"message": {"content": "resp"}, "finish_reason": "stop"}]}


def test_record_completion_metadata_accumulates_usage():
    init_run_metadata()

    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(100, 10, 110)))
    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(50, 5, 55)))

    metadata = get_run_metadata()
    assert metadata.num_ai_calls == 2
    assert metadata.prompt_tokens == 150
    assert metadata.completion_tokens == 15
    assert metadata.total_tokens == 165


def test_record_completion_metadata_counts_streaming_calls_without_tokens():
    init_run_metadata()

    LiteLLMAIHandler._record_completion_metadata(MockResponse("resp", "stop"))

    metadata = get_run_metadata()
    assert metadata.num_ai_calls == 1
    assert metadata.has_token_usage is False


def test_record_completion_metadata_tolerates_missing_response():
    init_run_metadata()

    LiteLLMAIHandler._record_completion_metadata(None)

    metadata = get_run_metadata()
    assert metadata.num_ai_calls == 1
    assert metadata.has_token_usage is False
