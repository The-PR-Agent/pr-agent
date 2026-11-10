"""Decide whether a `/review` run may approve the commit it reviewed (issue #4698)."""

from pr_agent.algo.setting_utils import _as_bool_setting


def approval_allowed(*, enable_auto_approval, publish_output, verdict, model_merge_recommendation,
                     model_risk_level, is_incremental, failed_chunk_count, partial_review_error,
                     state_blocked) -> tuple[bool, str]:
    """Return whether the run may approve, and the first reason it may not.

    Every input must pass. Anything missing or unexpected refuses, so the caller can dismiss
    an earlier approval whenever this returns False.
    """
    if not _as_bool_setting(enable_auto_approval, setting_name="config.enable_auto_approval"):
        return False, "auto-approval is disabled"
    if not _as_bool_setting(publish_output, setting_name="config.publish_output"):
        return False, "output publishing is disabled"
    if verdict != "no_concerns_found":
        return False, f"computed verdict is {verdict or 'unavailable'}"
    if not model_merge_recommendation or not model_risk_level:
        return False, "the model did not return both a merge recommendation and a risk level"
    if is_incremental:
        return False, "incremental reviews do not cover the whole pull request"
    if failed_chunk_count or partial_review_error is not None:
        return False, "part of the diff was not reviewed"
    if state_blocked:
        return False, "review finding state could not be updated"
    return True, "all approval conditions passed"
