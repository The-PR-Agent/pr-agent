"""A truncated /help_docs model response must fall back like the other commands."""

from types import SimpleNamespace

import pytest

from pr_agent.algo.pr_processing import FallbackEligibleError, retry_with_fallback_models
from pr_agent.commands.pr_help_docs import PredictionPreparator
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

PRIMARY = "gpt-4o"
BACKUP = "gpt-4o-mini"
ANSWER = "response: Enable automatic review in the repository settings.\n"


@pytest.fixture
def models():
    overrides = {
        "config.model": PRIMARY,
        "config.fallback_models": [BACKUP],
        "model_routing.enable": False,
        "openai.fallback_deployments": [],
    }
    # retry_with_fallback_models writes openai.deployment_id back when it finishes.
    snapshot = snapshot_settings([*overrides, "openai.deployment_id"])
    for key, value in overrides.items():
        get_settings().set(key, value)
    yield get_settings()
    restore_settings(snapshot)


def _preparator(chat_completion):
    vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I enable automatic review?",
        "snippets": "Automatic review runs on every new PR.",
    }
    return PredictionPreparator(
        SimpleNamespace(chat_completion=chat_completion),
        vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )


async def test_truncated_primary_answer_retries_backup(models):
    attempted = []

    async def chat_completion(*, model, **_kwargs):
        attempted.append(model)
        return ("response: Enable automatic", "length") if model == PRIMARY else (ANSWER, "stop")

    response = await retry_with_fallback_models(_preparator(chat_completion))

    assert attempted == [PRIMARY, BACKUP]
    assert response == ANSWER


async def test_truncated_last_answer_is_not_returned(models):
    models.set("config.fallback_models", [])

    async def chat_completion(**_kwargs):
        return "response: partial", "length"

    with pytest.raises(Exception) as error:
        await retry_with_fallback_models(_preparator(chat_completion))

    assert isinstance(error.value.__cause__, FallbackEligibleError)
