from pr_agent.algo.review_finding_state import reconcile_review_findings
from pr_agent.algo.review_merge import VOTE_LINE_TOLERANCE, same_finding_across_runs

DEBUG_A = {"path": "test/shell/themes_screen_test.dart", "line_start": 67, "line_end": 87,
           "body": "**Debug Leftovers**\n\nThe `tapSet` helper still calls `print(...)` and `debugDumpApp()` on every scroll-and-tap."}
# Same normalized header as DEBUG_A ("debug leftovers"), body reworded - the header equality path
# is what must catch this, since raw-body Jaccard between differently reworded findings can land
# well under VOTE_TEXT_SIMILARITY (see the R-6 fix-round-1 report for the measured numbers).
DEBUG_B = {"path": "test/shell/themes_screen_test.dart", "line_start": 68, "line_end": 88,
           "body": "**Debug Leftovers**\n\n`tapSet` contains leftover debugging statements including `debugDumpApp()` "
                   "and multiple `print` calls."}
# A second, independently-worded match for DEBUG_A (same header, different content) - used to prove
# two distinct current findings don't collapse onto one previous finding.
DEBUG_E = {"path": "test/shell/themes_screen_test.dart", "line_start": 66, "line_end": 86,
           "body": "**Debug Leftovers**\n\nA stray `debugPrint` call was left in `tapSet` alongside the "
                   "`debugDumpApp()` invocation."}
# Same header as DEBUG_A, chosen (by hash) to sort before DEBUG_A's finding_id, so it is processed
# first when a run reports it alongside a byte-identical DEBUG_A - the ordering that exposes the
# exact-vs-fuzzy collision.
DEBUG_F = {"path": "test/shell/themes_screen_test.dart", "line_start": 68, "line_end": 68,
           "body": "**Debug Leftovers**\n\n`tapSet` now also triggers a stray network call during teardown (2)"}
OTHER_NEARBY = {"path": "test/shell/themes_screen_test.dart", "line_start": 70, "line_end": 72,
                "body": "**Flaky Wait**\n\nThe test awaits a fixed Duration instead of pumpAndSettle, which is timing dependent."}

# Same header, same path, start lines exactly VOTE_LINE_TOLERANCE apart -> location gate passes.
SAME_HEADER_NEAR = dict(
    DEBUG_A,
    line_start=DEBUG_A["line_start"] + VOTE_LINE_TOLERANCE,
    line_end=DEBUG_A["line_end"] + VOTE_LINE_TOLERANCE,
    body="**Debug Leftovers**\n\nA different description of the same stray debug output problem entirely.",
)
# One line further apart than the tolerance allows -> location gate fails, regardless of header.
SAME_HEADER_FAR = dict(
    DEBUG_A,
    line_start=DEBUG_A["line_start"] + VOTE_LINE_TOLERANCE + 1,
    line_end=DEBUG_A["line_end"] + VOTE_LINE_TOLERANCE + 1,
    body="**Debug Leftovers**\n\nA different description of the same stray debug output problem entirely.",
)

# Overlapping-ish lines, meaningfully shared vocabulary (same function name, same domain words),
# but two genuinely distinct defects with different headers - must not merge. Whole-body Jaccard
# alone cannot separate these classes; header/location must do the work.
MISSING_TEST_COVERAGE = {"path": "lib/pricing.dart", "line_start": 10, "line_end": 12,
                         "body": "**Missing Test Coverage**\n\nThe new `calculateDiscount` function has no unit tests "
                                 "covering the edge cases for zero and negative values."}
MISSING_DOCSTRING = {"path": "lib/pricing.dart", "line_start": 11, "line_end": 13,
                     "body": "**Missing Docstring**\n\nThe new `calculateDiscount` function has no docstring "
                             "explaining the edge cases for zero and negative value handling."}
SQL_INJECTION = {"path": "lib/db.dart", "line_start": 30, "line_end": 32,
                 "body": "**SQL Injection**\n\nThe `getUserById` query concatenates raw user input directly into "
                         "the SQL string without parameterization or escaping."}
MISSING_INPUT_VALIDATION = {"path": "lib/db.dart", "line_start": 31, "line_end": 33,
                            "body": "**Missing Input Validation**\n\nThe `getUserById` handler does not validate or "
                                    "sanitize the raw user input before using it to build the SQL query string."}

# Different headers, same start line, but the bodies are near-identical (Jaccard >= 0.5 on the
# whole text) - the wording path must still catch this.
SAME_DEFECT_DIFFERENT_HEADER_A = {"path": "lib/sync.dart", "line_start": 40, "line_end": 42,
                                  "body": "**Race Condition**\n\nThe `syncState` method reads `cache.value` without "
                                          "holding the lock before writing `cache.timestamp`, allowing concurrent "
                                          "writers to interleave."}
SAME_DEFECT_DIFFERENT_HEADER_B = {"path": "lib/sync.dart", "line_start": 40, "line_end": 42,
                                  "body": "**Concurrency Bug**\n\nThe `syncState` method reads `cache.value` without "
                                          "holding the lock before writing `cache.timestamp`, letting concurrent "
                                          "writers interleave."}


def test_reworded_same_defect_matches():
    assert same_finding_across_runs(DEBUG_A, DEBUG_B)


def test_nearby_distinct_defect_does_not_match():
    assert not same_finding_across_runs(DEBUG_A, OTHER_NEARBY)


def test_same_wording_moved_far_does_not_match():
    moved = dict(DEBUG_A, line_start=400, line_end=420)
    assert not same_finding_across_runs(DEBUG_A, moved)


def test_same_header_start_lines_within_tolerance_matches():
    assert same_finding_across_runs(DEBUG_A, SAME_HEADER_NEAR)


def test_same_header_start_lines_beyond_tolerance_does_not_match():
    assert not same_finding_across_runs(DEBUG_A, SAME_HEADER_FAR)


def test_shared_vocabulary_different_headers_does_not_match_missing_coverage_vs_docstring():
    assert not same_finding_across_runs(MISSING_TEST_COVERAGE, MISSING_DOCSTRING)


def test_shared_vocabulary_different_headers_does_not_match_sql_injection_vs_input_validation():
    assert not same_finding_across_runs(SQL_INJECTION, MISSING_INPUT_VALIDATION)


def test_high_body_similarity_with_different_headers_matches():
    assert same_finding_across_runs(SAME_DEFECT_DIFFERENT_HEADER_A, SAME_DEFECT_DIFFERENT_HEADER_B)


def test_reconcile_keeps_one_record_for_reworded_finding():
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(first.state, [DEBUG_B], allow_resolution=False, head_sha="s2", run_id="r2")
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 1
    assert active[0]["finding_id"] == first.state["findings"][0]["finding_id"]
    assert "leftover debugging statements" in active[0]["body"].lower()  # latest wording kept, first id kept


def test_two_current_findings_do_not_both_claim_one_previous_finding():
    # DEBUG_B and DEBUG_E each independently match DEBUG_A (same header). Reported together in
    # the same run they must stay two distinct findings - only the first claims DEBUG_A's id.
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(
        first.state, [DEBUG_B, DEBUG_E], allow_resolution=False, head_sha="s2", run_id="r2"
    )
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 2
    assert len({f["finding_id"] for f in active}) == 2
    original_id = first.state["findings"][0]["finding_id"]
    assert original_id in {f["finding_id"] for f in active}


def test_exact_and_fuzzy_match_do_not_collide_on_one_previous_finding():
    # DEBUG_A recurs byte-for-byte (an exact-id match) in the same run as DEBUG_F, which only
    # fuzzy-matches DEBUG_A (same header). DEBUG_F's finding_id hash-sorts before DEBUG_A's, so it
    # is processed first; it must not steal DEBUG_A's previous record and cause DEBUG_A's own
    # exact match to overwrite it, silently dropping one finding.
    first = reconcile_review_findings(None, [DEBUG_A], allow_resolution=False, head_sha="s1", run_id="r1")
    second = reconcile_review_findings(
        first.state, [DEBUG_A, DEBUG_F], allow_resolution=False, head_sha="s2", run_id="r2"
    )
    active = [f for f in second.state["findings"] if f["state"] == "ACTIVE"]
    assert len(active) == 2
    assert len({f["finding_id"] for f in active}) == 2
    original_id = first.state["findings"][0]["finding_id"]
    assert original_id in {f["finding_id"] for f in active}
