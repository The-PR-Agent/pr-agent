from pr_agent.algo.review_finding_state import reconcile_review_findings
from pr_agent.algo.review_merge import same_finding_across_runs

DEBUG_A = {"path": "test/shell/themes_screen_test.dart", "line_start": 67, "line_end": 87,
           "body": "**Debug Leftovers**\n\nThe `tapSet` helper still calls `print(...)` and `debugDumpApp()` on every scroll-and-tap."}
DEBUG_B = {"path": "test/shell/themes_screen_test.dart", "line_start": 67, "line_end": 88,
           "body": "**Leftover Debug Code**\n\n`tapSet` contains leftover debugging statements including `debugDumpApp()` and multiple `print` calls."}
OTHER_NEARBY = {"path": "test/shell/themes_screen_test.dart", "line_start": 70, "line_end": 72,
                "body": "**Flaky Wait**\n\nThe test awaits a fixed Duration instead of pumpAndSettle, which is timing dependent."}
DEBUG_C = {"path": "test/shell/themes_screen_test.dart", "line_start": 69, "line_end": 89,
           "body": "**Leftover Debug Statements**\n\nThe `tapSet` method still contains leftover debugging output, "
                   "including `debugDumpApp()` and several `print` calls."}


def test_reworded_same_defect_matches():
    assert same_finding_across_runs(DEBUG_A, DEBUG_B)


def test_nearby_distinct_defect_does_not_match():
    assert not same_finding_across_runs(DEBUG_A, OTHER_NEARBY)


def test_same_wording_moved_far_does_not_match():
    moved = dict(DEBUG_A, line_start=400, line_end=420)
    assert not same_finding_across_runs(DEBUG_A, moved)


def test_reconcile_keeps_one_record_for_reworded_finding():
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(first.state, [DEBUG_B], allow_resolution=False, head_sha="s2", run_id="r2")
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 1
    assert active[0]["finding_id"] == first.state["findings"][0]["finding_id"]
    assert "Leftover Debug Code" in active[0]["body"]  # latest wording kept, first id kept


def test_two_current_findings_do_not_both_claim_one_previous_finding():
    # DEBUG_B and DEBUG_C each independently look like a reworded DEBUG_A. Reported together in
    # the same run they must stay two distinct findings - only the first claims DEBUG_A's id.
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(
        first.state, [DEBUG_B, DEBUG_C], allow_resolution=False, head_sha="s2", run_id="r2"
    )
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 2
    assert len({f["finding_id"] for f in active}) == 2
    original_id = first.state["findings"][0]["finding_id"]
    assert original_id in {f["finding_id"] for f in active}
