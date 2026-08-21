"""A single ticket returned as a mapping must still be rendered."""
import pytest

from pr_agent.algo.utils import ticket_markdown_logic

TICKET = {
    "ticket_url": "https://github.com/o/r/issues/7",
    "fully_compliant_requirements": "- does the thing",
    "not_compliant_requirements": "",
    "requires_further_human_verification": "",
}


def test_render_a_single_ticket_given_as_a_mapping():
    """One ticket returned as a dict rather than a one-element list must not vanish."""
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
    """Other shapes still render nothing, as before."""
    assert ticket_markdown_logic("T", "", value, True) == ""
