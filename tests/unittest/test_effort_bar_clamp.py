"""The effort bar is a 1-5 scale and must stay one whatever the model returns."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2


def _bars(value):
    out = convert_to_markdown_v2({"review": {"estimated_effort_to_review_[1-5]": value}},
                                 gfm_supported=True)
    return out.count("\U0001f535"), out.count("\u26aa")


@pytest.mark.parametrize("value, expected", [("1", (1, 4)), ("3", (3, 2)), ("5", (5, 0))])
def test_render_in_range_scores_unchanged(value, expected):
    """Scores inside the scale keep their existing rendering."""
    assert _bars(value) == expected


def test_clamp_a_score_above_the_scale():
    """A score above 5 must not produce a bar longer than the scale."""
    blue, white = _bars("8")

    assert blue == 5
    assert white == 0


def test_clamp_a_score_below_the_scale():
    """A score below 1 must not produce a negative-length bar."""
    blue, white = _bars("0")

    assert blue == 1
    assert white == 4


def test_the_bar_is_always_five_segments():
    """Total segments stay constant so the bar reads as a 1-5 scale."""
    for value in ("0", "1", "3", "5", "8", "99"):
        blue, white = _bars(value)
        assert blue + white == 5, value
