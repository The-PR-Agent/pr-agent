"""Read the /improve score thresholds whatever numeric form the settings file uses."""
import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import get_dual_publishing_score_threshold, get_suggestions_score_threshold


@pytest.fixture
def restore_thresholds():
    settings = get_settings(use_context=False)
    originals = {key: settings.get(key, None) for key in
                 ("pr_code_suggestions.suggestions_score_threshold",
                  "pr_code_suggestions.dual_publishing_score_threshold")}
    yield settings
    for key, value in originals.items():
        settings.set(key, value)


@pytest.mark.parametrize("value, expected", [(5, 5), ("5", 5), (0, 1), ("0", 1), (-3, 1)])
def test_read_the_suggestions_score_threshold(restore_thresholds, value, expected):
    """A quoted number is what TOML yields for suggestions_score_threshold = "5"."""
    restore_thresholds.set("pr_code_suggestions.suggestions_score_threshold", value)

    assert get_suggestions_score_threshold() == expected


@pytest.mark.parametrize("value", ["high", None, [5]])
def test_fall_back_for_an_unusable_suggestions_score_threshold(restore_thresholds, value):
    """Fall back to the lowest threshold rather than failing the whole /improve run."""
    restore_thresholds.set("pr_code_suggestions.suggestions_score_threshold", value)

    assert get_suggestions_score_threshold() == 1


@pytest.mark.parametrize("value, expected", [(7, 7), ("7", 7), (0, 0)])
def test_read_the_dual_publishing_score_threshold(restore_thresholds, value, expected):
    """Dual publishing is off at 0, so the coerced value must preserve that."""
    restore_thresholds.set("pr_code_suggestions.dual_publishing_score_threshold", value)

    assert get_dual_publishing_score_threshold() == expected


@pytest.mark.parametrize("value", ["always", None])
def test_an_unusable_dual_publishing_threshold_disables_it(restore_thresholds, value):
    """An unreadable value must not silently enable a publishing mode."""
    restore_thresholds.set("pr_code_suggestions.dual_publishing_score_threshold", value)

    assert get_dual_publishing_score_threshold() == 0
