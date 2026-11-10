import pytest

from pr_agent.algo.auto_approval import approval_allowed

CLEAN = {
    "enable_auto_approval": True,
    "publish_output": True,
    "verdict": "no_concerns_found",
    "model_merge_recommendation": "no_concerns_found",
    "model_risk_level": "low",
    "is_incremental": False,
    "failed_chunk_count": 0,
    "partial_review_error": None,
    "state_blocked": False,
}


def test_all_conditions_clean_allows_approval():
    assert approval_allowed(**CLEAN) == (True, "all approval conditions passed")


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"enable_auto_approval": False}, "auto-approval is disabled"),
        ({"enable_auto_approval": "false"}, "auto-approval is disabled"),
        ({"enable_auto_approval": None}, "auto-approval is disabled"),
        ({"publish_output": False}, "output publishing is disabled"),
        ({"publish_output": "false"}, "output publishing is disabled"),
        ({"verdict": "needs_review"}, "computed verdict is needs_review"),
        ({"verdict": "changes_required"}, "computed verdict is changes_required"),
        ({"verdict": None}, "computed verdict is unavailable"),
        ({"model_merge_recommendation": None}, "both a merge recommendation and a risk level"),
        ({"model_risk_level": ""}, "both a merge recommendation and a risk level"),
        ({"is_incremental": True}, "incremental reviews"),
        ({"failed_chunk_count": 1}, "part of the diff was not reviewed"),
        ({"partial_review_error": RuntimeError("fallbacks exhausted")}, "part of the diff was not reviewed"),
        ({"state_blocked": True}, "review finding state could not be updated"),
    ],
)
def test_each_failing_condition_refuses_with_its_reason(override, reason):
    allowed, why = approval_allowed(**{**CLEAN, **override})
    assert allowed is False
    assert reason in why


def test_string_true_flag_is_honoured():
    assert approval_allowed(**{**CLEAN, "enable_auto_approval": "true"})[0] is True
