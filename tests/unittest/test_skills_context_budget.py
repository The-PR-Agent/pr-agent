"""Respect the token budget of the skills context, which is injected into a prompt."""
from contextlib import contextmanager

import pytest

from pr_agent.algo.skills_loader import Skill, format_skills_context
from pr_agent.algo.token_handler import TokenEncoder


def _tokens(text):
    return len(TokenEncoder.get_token_encoder().encode(text))


@contextmanager
def _capture_warnings():
    """Collect WARNING lines; pytest's caplog does not see pr-agent's loguru output."""
    from loguru import logger as loguru_logger

    lines = []
    sink_id = loguru_logger.add(lambda msg: lines.append(str(msg)), level="WARNING")
    try:
        yield lines
    finally:
        loguru_logger.remove(sink_id)


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


def test_dropped_skills_are_named_in_a_warning():
    """Report every dropped skill, so operators can see which guidance was lost."""
    skills = [Skill(name=f"s{i}", description="d", body="x " * 500) for i in range(5)]
    # Budget fits the first skill whole, so the tail is what gets dropped.
    budget = _tokens(format_skills_context([skills[0]], max_tokens=100_000)) + 10

    with _capture_warnings() as lines:
        out = format_skills_context(skills, max_tokens=budget)

    dropped = [s.name for s in skills if f"Skill: {s.name}" not in out]
    assert dropped, "budget must drop at least one skill for this test to be meaningful"
    assert len(lines) == 1
    assert f"dropping {len(dropped)} skill(s): {', '.join(dropped)}" in lines[0]


def test_clipped_first_skill_is_named_with_its_token_usage():
    """Report the clipped skill by name and show how much of it was kept."""
    skills = [
        Skill(name="huge", description="d", body="y " * 5000),
        Skill(name="other", description="d", body="small"),
    ]

    with _capture_warnings() as lines:
        format_skills_context(skills, max_tokens=50)

    assert len(lines) == 1
    assert "first skill huge clipped to " in lines[0]
    assert " tokens, dropping 1 skill(s): other" in lines[0]


def test_in_budget_skills_log_no_warning():
    """Keep a fully fitting set silent, so the warning only marks real loss."""
    skills = [Skill(name=f"s{i}", description="d", body="small body") for i in range(3)]

    with _capture_warnings() as lines:
        out = format_skills_context(skills, max_tokens=4000)

    assert "Skill: s2" in out
    assert lines == []
