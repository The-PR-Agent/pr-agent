"""Deprecated self-review marker keys."""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_code_suggestions as module
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = (
    "pr_code_suggestions.approve_pr_on_self_review",
    "pr_code_suggestions.fold_suggestions_on_self_review",
)


@pytest.fixture(autouse=True)
def clean_self_review_keys():
    snapshot = snapshot_settings(KEYS)
    module._self_review_marker_keys_warned = False
    yield
    restore_settings(snapshot)
    module._self_review_marker_keys_warned = False


def _warnings(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_logger", lambda: logger)
    return logger


async def _add_self_review_text():
    return await PRCodeSuggestions.add_self_review_text(None, "body")


async def test_default_output_always_uses_fold_marker(monkeypatch):
    logger = _warnings(monkeypatch)

    body = await _add_self_review_text()

    assert body.endswith("<!-- fold suggestions self-review -->")
    logger.warning.assert_not_called()


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [True, False])
async def test_setting_either_key_warns_once(key, value, monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(key, value)

    for _ in range(3):
        body = await _add_self_review_text()

    assert body.endswith("<!-- fold suggestions self-review -->")
    logger.warning.assert_called_once()
    message = str(logger.warning.call_args[0][0])
    assert "deprecated" in message
    assert "approve_pr_on_self_review" in message
    assert "fold_suggestions_on_self_review" in message
