from pr_agent.algo.pr_processing import _get_all_models
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


def test_get_all_models_ignores_empty_comma_separated_fallbacks():
    snapshot = snapshot_settings(["config.model", "config.fallback_models"])
    settings = get_settings(use_context=False)
    try:
        settings.set("config.model", "primary-model")
        settings.set("config.fallback_models", " fallback-a, ,fallback-b, ")

        assert _get_all_models() == ["primary-model", "fallback-a", "fallback-b"]
    finally:
        restore_settings(snapshot)


def test_get_all_models_ignores_empty_list_fallbacks():
    snapshot = snapshot_settings(["config.model", "config.fallback_models"])
    settings = get_settings(use_context=False)
    try:
        settings.set("config.model", "primary-model")
        settings.set("config.fallback_models", ["fallback-a", "", " ", "fallback-b"])

        assert _get_all_models() == ["primary-model", "fallback-a", "fallback-b"]
    finally:
        restore_settings(snapshot)
