"""Respect the token budget of the skills context, which is injected into a prompt."""
import pytest

from pr_agent.algo.skills_loader import Skill, format_skills_context
from pr_agent.algo.token_handler import TokenEncoder


def _tokens(text):
    return len(TokenEncoder.get_token_encoder().encode(text))


BIG = Skill(name="s", description="d", body="word " * 5000)


@pytest.mark.parametrize("budget", [20, 50, 200, 1000])
def test_the_truncated_context_stays_within_budget(budget):
    """Account for the truncation marker, which is appended after clipping."""
    out = format_skills_context([BIG], budget)

    assert _tokens(out) <= budget


@pytest.mark.parametrize("budget", [20, 50, 200, 1000])
def test_the_truncated_context_still_carries_the_skill(budget):
    """Keep the skill and its truncation marker, so shrinking cannot degenerate to nothing."""
    out = format_skills_context([BIG], budget)

    assert "[truncated]" in out
    assert "word" in out


def test_a_skill_within_budget_is_not_truncated():
    """Emit a small skill whole."""
    small = Skill(name="s", description="d", body="short body")

    out = format_skills_context([small], 1000)

    assert "[truncated]" not in out
    assert "short body" in out


def test_no_skills_produces_no_context():
    """Return an empty string for an empty skill list."""
    assert format_skills_context([], 100) == ""
