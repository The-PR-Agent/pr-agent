"""Read huggingface.repetition_penalty without failing the handler constructor."""
import pytest

from pr_agent.algo.ai_handlers.litellm_helpers import get_repetition_penalty
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_penalty():
    settings = get_settings(use_context=False)
    original = settings.get("huggingface.repetition_penalty", None)
    yield settings
    settings.set("huggingface.repetition_penalty", original)


@pytest.mark.parametrize("value, expected", [(1.2, 1.2), ("1.2", 1.2), (2, 2.0)])
def test_read_a_numeric_repetition_penalty(restore_penalty, value, expected):
    """A quoted number is what TOML yields for repetition_penalty = "1.2"."""
    restore_penalty.set("huggingface.repetition_penalty", value)

    assert get_repetition_penalty() == expected


@pytest.mark.parametrize("value", ["1.2x", "aggressive", [1.2], float("nan"), float("inf")])
def test_ignore_an_unusable_repetition_penalty(restore_penalty, value):
    """Fall back to no penalty rather than raising inside LiteLLMAIHandler.__init__."""
    restore_penalty.set("huggingface.repetition_penalty", value)

    assert get_repetition_penalty() is None


def test_an_unset_repetition_penalty_is_ignored(restore_penalty):
    """Deployments that never set it keep behaving as before."""
    restore_penalty.set("huggingface.repetition_penalty", None)

    assert get_repetition_penalty() is None
