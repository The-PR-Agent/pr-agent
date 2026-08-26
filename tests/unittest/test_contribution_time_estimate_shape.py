"""Drop only the malformed time-estimate row, never the whole review."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2

WELL_FORMED = {"best_case": "30m", "average_case": "1h", "worst_case": "2h"}


@pytest.mark.parametrize("value", ["30m", None, ["30m"], {"best_case": "30m"}])
def test_skip_a_malformed_estimate_without_raising(value):
    """Render without raising when the model returns the wrong shape."""
    out = convert_to_markdown_v2({"review": {"contribution_time_cost_estimate": value,
                                             "score": "80"}}, gfm_supported=True)

    assert "Contribution time estimate" not in out


def test_the_rest_of_the_review_still_renders():
    """Keep the remaining fields when the estimate is malformed."""
    out = convert_to_markdown_v2({"review": {"contribution_time_cost_estimate": "30m",
                                             "score": "80"}}, gfm_supported=True)

    assert "80" in out


def test_a_well_formed_estimate_still_renders():
    """Keep the existing output for a correctly shaped estimate."""
    out = convert_to_markdown_v2({"review": {"contribution_time_cost_estimate": WELL_FORMED}},
                                 gfm_supported=True)

    assert "Contribution time estimate" in out
    assert "30 minutes" in out


@pytest.mark.parametrize("estimate", [
    {"best_case": None, "average_case": "1h", "worst_case": "2h"},
    {"best_case": 30, "average_case": "1h", "worst_case": "2h"},
    {"best_case": "10m", "average_case": "1h"},
])
def test_a_malformed_case_value_is_skipped(estimate):
    """Skip the row when a case is present but is not a string the renderer can expand."""
    out = convert_to_markdown_v2({"review": {"contribution_time_cost_estimate": estimate,
                                             "score": "80"}}, gfm_supported=True)

    assert "Contribution time estimate" not in out
    assert "80" in out
