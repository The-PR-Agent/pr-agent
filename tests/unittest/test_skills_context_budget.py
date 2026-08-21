"""The skills context is injected into a prompt, so it must respect its token budget."""
import pytest

from pr_agent.algo.skills_loader import Skill, format_skills_context
from pr_agent.algo.token_handler import TokenEncoder


def _tokens(text):
    return len(TokenEncoder.get_token_encoder().encode(text))


BIG = Skill(name="s", description="d", body="word " * 5000)


@pytest.mark.parametrize("budget", [20, 50, 200, 1000])
def test_the_truncated_context_stays_within_budget(budget):
    """The truncation marker is appended after clipping, so it must be accounted for."""
    out = format_skills_context([BIG], budget)

    assert _tokens(out) <= budget


def test_the_truncation_marker_is_still_present():
    """Truncation must remain visible to the model."""
    assert "[truncated]" in format_skills_context([BIG], 50)


def test_a_skill_within_budget_is_not_truncated():
    """A small skill is emitted whole."""
    small = Skill(name="s", description="d", body="short body")

    out = format_skills_context([small], 1000)

    assert "[truncated]" not in out
    assert "short body" in out


def test_no_skills_produces_no_context():
    """An empty skill list still returns an empty string."""
    assert format_skills_context([], 100) == ""
