"""Keep prompts and responses (the PR diff) out of OpenAIHandler's default logs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
import pr_agent.algo.ai_handlers.openai_ai_handler as openai_handler
from pr_agent.log import get_logger

SYSTEM_SECRET = "system-prompt-sentinel"
USER_SECRET = "user-prompt-sentinel"
RESPONSE_SECRET = "model-response-sentinel"
SECRETS = (SYSTEM_SECRET, USER_SECRET, RESPONSE_SECRET)


class FakeSettings:
    def __init__(self, opt_out):
        self.openai = SimpleNamespace(key="test-key")
        self._values = {"LITELLM.TURN_OFF_MESSAGE_LOGGING": opt_out}

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture
def handler_logs(monkeypatch):
    def make_handler(opt_out):
        settings = FakeSettings(opt_out)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(openai_handler, "get_settings", lambda: settings)
        monkeypatch.setattr(litellm_handler, "global_settings", FakeSettings(False))
        message = SimpleNamespace(content=RESPONSE_SECRET)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        response = SimpleNamespace(choices=[choice], usage=None)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
        )
        monkeypatch.setattr(openai_handler, "AsyncOpenAI", lambda: client)
        return openai_handler.OpenAIHandler()

    records = []
    sink = get_logger().add(
        records.append,
        filter=lambda record: record["name"] == openai_handler.__name__,
        level="DEBUG",
        format="{message}",
        serialize=True,
    )
    yield make_handler, records
    get_logger().remove(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_out", [False, "false"])
async def test_prompts_and_response_are_logged_at_debug_only(handler_logs, opt_out):
    make_handler, records = handler_logs

    resp, _ = await make_handler(opt_out).chat_completion(model="gpt-test", system=SYSTEM_SECRET, user=USER_SECRET)

    assert resp == RESPONSE_SECRET
    for secret in SECRETS:
        levels = {r.record["level"].name for r in records if secret in r}
        assert levels == {"DEBUG"}


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_out", [True, "true"])
async def test_message_logging_opt_out_keeps_prompts_and_response_out_of_logs(handler_logs, opt_out):
    make_handler, records = handler_logs

    resp, _ = await make_handler(opt_out).chat_completion(model="gpt-test", system=SYSTEM_SECRET, user=USER_SECRET)

    assert resp == RESPONSE_SECRET
    assert records, "the handler should still log that the call completed"
    for secret in SECRETS:
        assert not any(secret in r for r in records)
