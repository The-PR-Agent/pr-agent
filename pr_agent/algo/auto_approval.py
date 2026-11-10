"""Decide whether a `/review` run may approve the commit it reviewed (issue #4698)."""


def approval_allowed(*, review_completed, verdict, model_merge_recommendation, model_risk_level, is_incremental,
                     unreviewed_file_count, failed_chunk_count, partial_review_error,
                     state_blocked) -> tuple[bool, str]:
    """Return whether the run may approve, and the first reason it may not.

    Every input must pass. Anything missing or unexpected refuses, so the caller can dismiss
    an earlier approval whenever this returns False.
    """
    if not review_completed:
        return False, "the review did not complete"
    if verdict != "no_concerns_found":
        return False, f"computed verdict is {verdict or 'unavailable'}"
    if not model_merge_recommendation or not model_risk_level:
        return False, "the model did not return both a merge recommendation and a risk level"
    if is_incremental:
        return False, "incremental reviews do not cover the whole pull request"
    if unreviewed_file_count or failed_chunk_count or partial_review_error is not None:
        return False, "part of the diff was not reviewed"
    if state_blocked:
        return False, "review finding state could not be updated"
    return True, "all approval conditions passed"
