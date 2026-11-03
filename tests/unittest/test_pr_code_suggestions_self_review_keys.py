"""Deprecated self-review keys (approve_pr_on_self_review / fold_suggestions_on_self_review)"""

from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.config_security import PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION
from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions, _warn_on_deprecated_self_review_keys
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

APPROVE = "pr_code_suggestions.approve_pr_on_self_review"
FOLD = "pr_code_suggestions.fold_suggestions_on_self_review"
TRACKED = (APPROVE, FOLD)

EXPECTED_WARNING = (
    "pr_code_suggestions.approve_pr_on_self_review and "
    "pr_code_suggestions.fold_suggestions_on_self_review are deprecated and have no effect; "
    "they will be removed in 1.0"
)


@pytest.fixture(autouse=True)
def clean_self_review_keys():
    """Isolate both removed keys and the process-wide warn-once flag between tests."""
    snapshot = snapshot_settings(TRACKED)
    pr_code_suggestions_module._deprecated_self_review_keys_warned = False
    yield
    restore_settings(snapshot)
    pr_code_suggestions_module._deprecated_self_review_keys_warned = False


def _warnings(monkeypatch):
    """Capture logger warnings emitted by the module, and clear the warn-once flag."""
    logger = MagicMock()
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_logger", lambda: logger)
    return logger


def test_removed_keys_are_not_host_only_anymore():
    keys = PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION["pr_code_suggestions"]
    assert "approve_pr_on_self_review" not in keys
    assert "fold_suggestions_on_self_review" not in keys
    assert "demand_code_suggestions_self_review" in keys


@pytest.mark.parametrize("key", [APPROVE, FOLD])
def test_setting_a_removed_key_warns_once(key, monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(key, True)

    for _ in range(3):
        _warn_on_deprecated_self_review_keys()
    logger.warning.assert_called_once()
    assert str(logger.warning.call_args[0][0]) == EXPECTED_WARNING


def test_no_warning_when_removed_keys_are_unset(monkeypatch):
    logger = _warnings(monkeypatch)

    _warn_on_deprecated_self_review_keys()
    logger.warning.assert_not_called()


async def test_self_review_text_always_uses_the_fold_marker(monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(APPROVE, True)

    tool = object.__new__(PRCodeSuggestions)
    body = await tool.add_self_review_text("body")
    assert body.endswith(' <!-- fold suggestions self-review -->')
    logger.warning.assert_called_once()


async def test_self_review_text_default_output_is_unchanged(monkeypatch):
    logger = _warnings(monkeypatch)

    tool = object.__new__(PRCodeSuggestions)
    body = await tool.add_self_review_text("body")
    text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
    assert body == f"body\n\n- [ ]  {text} <!-- fold suggestions self-review -->"
    logger.warning.assert_not_called()
