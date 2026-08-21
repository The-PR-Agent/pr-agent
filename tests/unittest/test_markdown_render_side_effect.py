"""Render without mutating the data the caller still uses afterwards."""
import copy

from pr_agent.algo.utils import convert_to_markdown_v2

DATA = {"review": {"todo_summary": "3 TODOs", "estimated_effort_to_review_[1-5]": "3",
                   "security_concerns": "No"}}


def test_the_callers_data_is_not_mutated():
    """Leave the dict intact, since pr_reviewer passes it to set_review_labels next."""
    data = copy.deepcopy(DATA)

    convert_to_markdown_v2(data, gfm_supported=True)

    assert data == DATA


def test_todo_summary_is_not_rendered_as_a_row():
    """Exclude todo_summary from the rendered table, as before."""
    out = convert_to_markdown_v2(copy.deepcopy(DATA), gfm_supported=True)

    assert "Todo summary" not in out


def test_the_other_fields_still_render():
    """Keep the remaining rows while excluding todo_summary."""
    out = convert_to_markdown_v2(copy.deepcopy(DATA), gfm_supported=True)

    assert "Estimated effort to review" in out
    assert "No security concerns identified" in out


def test_rendering_twice_gives_the_same_output():
    """Produce identical markdown on a second render of the same dict."""
    data = copy.deepcopy(DATA)

    assert convert_to_markdown_v2(data, True) == convert_to_markdown_v2(data, True)
