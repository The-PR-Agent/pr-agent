import json

from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    render_carried_section,
)

A = {"path": "lib/a.dart", "line_start": 1, "line_end": 2, "body": "**A**\n\nfirst issue"}
B = {"path": "lib/b.dart", "line_start": 5, "line_end": 6, "body": "**B**\n\nsecond issue"}


def _state_with_two_then_one():
    s1 = reconcile_review_findings(None, [A, B], allow_resolution=False, head_sha="s1", run_id="r1").state
    return reconcile_review_findings(s1, [A], allow_resolution=False, head_sha="s2", run_id="r2",
                                     fully_reviewed_files=["lib/a.dart"])


def test_carried_section_lists_findings_not_in_current_run():
    result = _state_with_two_then_one()
    current_ids = {f["finding_id"] for f in result.state["findings"] if f["path"] == "lib/a.dart"}
    section = render_carried_section(result.state, current_ids, fully_reviewed_files=["lib/a.dart"])
    assert "Carried from earlier runs" in section
    assert "lib/b.dart" in section and "not re-reviewed this run" in section
    assert "lib/a.dart" not in section


def test_visible_count_equals_active_plus_unconfirmed():
    result = _state_with_two_then_one()
    section = render_carried_section(result.state, set(), fully_reviewed_files=[])
    assert section.count("- **") == 2


def test_budget_drops_resolved_before_truncating_human_text():
    s1 = reconcile_review_findings(None, [A, B], allow_resolution=False, head_sha="s1", run_id="r1").state
    s2 = reconcile_review_findings(s1, [A], allow_resolution=True, head_sha="s2", run_id="r2",
                                   fully_reviewed_files=["lib/a.dart", "lib/b.dart"]).state
    assert any(f["state"] == "RESOLVED" for f in s2["findings"])
    body = "x" * 400
    out = append_review_state(body, s2, max_chars=len(body) + 60 + len(json.dumps(s2)))
    parsed = parse_review_state(out)
    assert out.startswith(body)  # human text intact
    assert all(f["state"] != "RESOLVED" for f in parsed.state["findings"])  # resolved dropped first


def test_current_ids_excludes_fuzzy_matched_finding_under_new_wording():
    original = {"path": "lib/a.dart", "line_start": 10, "line_end": 12,
                "body": "**Null check missing**\n\nThe value can be null here."}
    s1 = reconcile_review_findings(None, [original], allow_resolution=False, head_sha="s1", run_id="r1")
    old_id = s1.state["findings"][0]["finding_id"]

    reworded = {"path": "lib/a.dart", "line_start": 10, "line_end": 12,
                "body": "**Null check missing**\n\nThis line can dereference a null value without checking."}
    s2 = reconcile_review_findings(s1.state, [reworded], allow_resolution=False, head_sha="s2", run_id="r2")

    # The fuzzy match retains the previous id; it must not equal a fresh fingerprint of the reworded body.
    assert s2.state["findings"][0]["finding_id"] == old_id
    assert s2.state["findings"][0]["body"] == reworded["body"]

    # current_ids must list the retained id, so the reworded recurrence is not shown as carried.
    assert old_id in s2.current_ids

    section = render_carried_section(s2.state, set(s2.current_ids), fully_reviewed_files=[])
    assert section == ""
