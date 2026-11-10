import warnings
from types import SimpleNamespace

from litellm import ModelResponse

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ai_handlers.litellm_helpers import MockResponse


def _model_response():
    return ModelResponse(
        model="gpt-4o",
        choices=[{"message": {"content": "hi"}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )


def test_prepare_logs_with_model_response_emits_no_dict_deprecation():
    # prepare_logs only reads main_pr_language from self (optionally), so a
    # SimpleNamespace stands in for a handler instance.
    handler = SimpleNamespace()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        log = LiteLLMAIHandler.prepare_logs(handler, _model_response(), "sys", "usr", "hi", "stop")
    assert not [w for w in caught if "dict" in str(w.message) and "deprecat" in str(w.message).lower()]
    assert log["output"] == "hi"
    assert log["finish_reason"] == "stop"
    assert log["system"] == "sys"
    assert log["user"] == "usr"
    assert log["main_pr_language"] == "unknown"


def test_prepare_logs_with_mock_response_still_logs():
    handler = SimpleNamespace(main_pr_language="Python")
    mock = MockResponse("hi", "stop", usage=None, model="gpt-4o")
    log = LiteLLMAIHandler.prepare_logs(handler, mock, "sys", "usr", "hi", "stop")
    assert log["output"] == "hi"
    assert log["finish_reason"] == "stop"
    assert log["main_pr_language"] == "Python"
