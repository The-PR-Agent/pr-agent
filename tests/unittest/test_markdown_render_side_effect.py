"""Rendering must not mutate the data the caller still uses afterwards."""
import copy

from pr_agent.algo.utils import convert_to_markdown_v2

DATA = {"review": {"todo_summary": "3 TODOs", "estimated_effort_to_review_[1-5]": "3",
                   "security_concerns": "No"}}


def test_the_callers_data_is_not_mutated():
    """pr_reviewer passes the same dict to set_review_labels after rendering."""
    data = copy.deepcopy(DATA)

    convert_to_markdown_v2(data, gfm_supported=True)

    assert data == DATA


def test_todo_summary_is_not_rendered_as_a_row():
    """todo_summary is deliberately excluded from the rendered table."""
    out = convert_to_markdown_v2(copy.deepcopy(DATA), gfm_supported=True)

    assert "Todo summary" not in out


def test_the_other_fields_still_render():
    """Excluding todo_summary must not drop the remaining rows."""
    out = convert_to_markdown_v2(copy.deepcopy(DATA), gfm_supported=True)

    assert "Estimated effort to review" in out
    assert "No security concerns identified" in out


def test_rendering_twice_gives_the_same_output():
    """A second render of the same dict must produce identical markdown."""
    data = copy.deepcopy(DATA)

    assert convert_to_markdown_v2(data, True) == convert_to_markdown_v2(data, True)
