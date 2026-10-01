"""Deprecated alias of committable_code_suggestions"""

from unittest.mock import MagicMock

import pytest
from loguru import logger as loguru_logger

from pr_agent.config_loader import get_settings
from pr_agent.config_security import PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION
from pr_agent.git_providers import utils as git_utils
from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
from pr_agent.tools.pr_code_suggestions import get_committable_code_suggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

CANONICAL = "pr_code_suggestions.committable_code_suggestions"
DEPRECATED = "pr_code_suggestions.commitable_code_suggestions"
TRACKED = (CANONICAL, DEPRECATED)


@pytest.fixture(autouse=True)
def clean_spellings():
    """Isolate both spellings and the process-wide warn-once flag between tests."""
    snapshot = snapshot_settings(TRACKED)
    pr_code_suggestions_module._deprecated_spellings_warned = False
    yield
    restore_settings(snapshot)
    pr_code_suggestions_module._deprecated_spellings_warned = False


def _warnings(monkeypatch):
    """Capture logger warnings emitted by the resolver, and clear the warn-once flag."""
    logger = MagicMock()
    monkeypatch.setattr("pr_agent.tools.pr_code_suggestions.get_logger", lambda: logger)
    return logger


def test_repo_toml_with_deprecated_spelling_warns_and_enables(monkeypatch):

    # apply_repo_settings() reads repo config through git_provider.get_repo_settings(), so the
    # deprecated key has to travel that path to prove the merge keeps an unknown key.
    repo_settings = "[pr_code_suggestions]\ncommitable_code_suggestions = true\n"

    def fake_git_provider_with_context(url):
        provider = MagicMock()
        provider.get_repo_settings.return_value = repo_settings
        return provider

    monkeypatch.setattr(
        "pr_agent.git_providers.utils.get_git_provider_with_context",
        fake_git_provider_with_context,
    )

    captured = []
    sink_id = loguru_logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        git_utils.apply_repo_settings("https://github.com/org/repo/pull/1")
        assert get_committable_code_suggestions() is True
    finally:
        loguru_logger.remove(sink_id)

    deprecation_lines = [line for line in captured if "commitable_code_suggestions" in line]
    assert len(deprecation_lines) == 1, f"expected exactly one deprecation warning, got {deprecation_lines}"
    assert "deprecated" in deprecation_lines[0]
    assert "will be removed in 1.0" in deprecation_lines[0]


def test_deprecated_spelling_is_host_only_per_directory():
    keys = PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION["pr_code_suggestions"]
    assert "committable_code_suggestions" in keys
    assert "commitable_code_suggestions" in keys


def test_canonical_true_enables_without_deprecation_warning(monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(DEPRECATED, False)
    get_settings().set(CANONICAL, True)

    assert get_committable_code_suggestions() is True
    logger.warning.assert_not_called()


def test_deprecated_alias_alone_still_enables_and_warns(monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(CANONICAL, False)
    get_settings().set(DEPRECATED, True)

    assert get_committable_code_suggestions() is True
    logger.warning.assert_called_once()
    message = str(logger.warning.call_args[0][0])
    assert message == (
        "pr_code_suggestions.commitable_code_suggestions is deprecated and will be removed in 1.0; "
        "rename it to pr_code_suggestions.committable_code_suggestions"
    )


def test_deprecation_warning_fires_once_per_process(monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(CANONICAL, False)
    get_settings().set(DEPRECATED, True)

    for _ in range(5):
        assert get_committable_code_suggestions() is True
    logger.warning.assert_called_once()


def test_canonical_true_takes_priority_over_deprecated_false(monkeypatch):
    _warnings(monkeypatch)
    get_settings().set(CANONICAL, True)
    get_settings().set(DEPRECATED, False)

    assert get_committable_code_suggestions() is True


def test_deprecated_true_takes_priority_when_canonical_is_false(monkeypatch):
    _warnings(monkeypatch)
    get_settings().set(CANONICAL, False)
    get_settings().set(DEPRECATED, True)

    assert get_committable_code_suggestions() is True


def test_both_spellings_true_enables_without_warning(monkeypatch):
    logger = _warnings(monkeypatch)
    get_settings().set(CANONICAL, True)
    get_settings().set(DEPRECATED, True)

    assert get_committable_code_suggestions() is True
    logger.warning.assert_not_called()


@pytest.mark.parametrize("canonical", [True, False])
def test_disabled_when_neither_spelling_is_set(canonical, monkeypatch):
    logger = _warnings(monkeypatch)
    settings = get_settings()
    settings.set(CANONICAL, canonical)
    restore_settings({DEPRECATED: snapshot_settings(TRACKED)[DEPRECATED]})

    assert get_committable_code_suggestions() is canonical
    logger.warning.assert_not_called()
