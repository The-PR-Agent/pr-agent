"""Respect the token budget of the skills context, which is injected into a prompt."""
import pytest

from pr_agent.algo.skills_loader import Skill, format_skills_context
from pr_agent.algo.token_budget import clip_tokens
from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.log import get_logger


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


def _capture_warnings(func):
    messages = []
    handler_id = get_logger().add(
        lambda msg: messages.append(msg.record["message"]),
        level="WARNING",
    )
    try:
        func()
    finally:
        get_logger().remove(handler_id)
    return messages


def test_over_budget_skills_log_warning_with_dropped_names():
    """An over-budget skill set produces a warning log containing the exact dropped skill names."""
    skill_a = Skill(name="skill_a", description="d1", body="body a " * 10)
    skill_b = Skill(name="skill_b", description="d2", body="body b " * 10)
    skill_c = Skill(name="skill_c", description="d3", body="body c " * 10)

    # Budget is enough for skill_a, but skill_b and skill_c will be dropped
    budget = _tokens(format_skills_context([skill_a], 1000)) + 5

    warnings = _capture_warnings(lambda: format_skills_context([skill_a, skill_b, skill_c], budget))

    assert len(warnings) == 1
    assert "dropping 2 skill(s): skill_b, skill_c" in warnings[0]


def test_first_skill_over_budget_log_warning():
    """First skill exceeding budget produces a warning with exact retained/full token counts excluding marker."""
    huge_skill = Skill(name="huge_skill", description="d1", body="word " * 500)
    skill_b = Skill(name="skill_b", description="d2", body="short body")

    max_tokens = 50
    formatted_full = format_skills_context([huge_skill], 100000)
    full_tokens = _tokens(formatted_full)

    truncate_marker = "\n\n[truncated]"
    marker_tokens = _tokens(truncate_marker)
    budget = max(1, max_tokens - marker_tokens)
    truncated = clip_tokens(formatted_full, budget, add_three_dots=False)
    while truncated and _tokens(truncated + truncate_marker) > max_tokens:
        truncated = truncated[: int(len(truncated) * 0.9)]
    expected_kept_tokens = _tokens(truncated)

    warnings = _capture_warnings(lambda: format_skills_context([huge_skill, skill_b], max_tokens))

    assert len(warnings) == 1
    msg = warnings[0]
    expected_count_str = f"({expected_kept_tokens}/{full_tokens} tokens kept)"
    assert f"First skill 'huge_skill' exceeded budget {expected_count_str}" in msg
    assert "truncated and dropped 1 skill(s): skill_b" in msg
    assert expected_kept_tokens < _tokens(truncated + truncate_marker)


def test_in_budget_skills_produce_no_warning():
    """An in-budget skill set produces no warning logs."""
    skill_a = Skill(name="skill_a", description="d1", body="short body a")
    skill_b = Skill(name="skill_b", description="d2", body="short body b")

    warnings = _capture_warnings(lambda: format_skills_context([skill_a, skill_b], 1000))

    assert warnings == []

