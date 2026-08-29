"""Render a single ticket that the model returned as a mapping."""
import pytest

from pr_agent.algo.utils import ticket_markdown_logic

TICKET = {
    "ticket_url": "https://github.com/o/r/issues/7",
    "fully_compliant_requirements": "- does the thing",
    "not_compliant_requirements": "",
    "requires_further_human_verification": "",
}


def test_render_a_single_ticket_given_as_a_mapping():
    """Render one ticket returned as a dict rather than as a one-element list."""
    out = ticket_markdown_logic("T", "", TICKET, True)

    assert "Ticket compliance analysis" in out
    assert "issues/7" in out


def test_render_a_single_ticket_given_as_a_list():
    """Keep the existing behaviour for the list form."""
    out = ticket_markdown_logic("T", "", [TICKET], True)

    assert "Ticket compliance analysis" in out
    assert "issues/7" in out


@pytest.mark.parametrize("value", ["a string", 7, None])
def test_ignore_a_value_that_is_neither_a_mapping_nor_a_list(value):
    """Render nothing for other shapes, as before."""
    assert ticket_markdown_logic("T", "", value, True) == ""
