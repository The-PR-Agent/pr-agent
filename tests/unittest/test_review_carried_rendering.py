import json

from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    render_carried_section,
    serialize_review_state,
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


def _single_active_state():
    finding = {"path": "app.py", "line_start": 1, "line_end": 1, "body": "**A**\n\nfirst issue"}
    return reconcile_review_findings(None, [finding], allow_resolution=False, head_sha="s1", run_id="r1").state


def test_body_exact_fit_is_untouched_and_carried_is_dropped():
    """budget for (body + carried) equals len(body) exactly: no truncation, no "...", carried drops."""
    state = _single_active_state()
    marker = serialize_review_state(state)
    body = "x" * 400
    carried = "### Carried from earlier runs\n\n- **B** — `other.py:1` · first seen 2026-01-01 · " \
              "not re-reviewed this run"
    max_chars = len(body) + len(marker) + 3  # budget == len(body) exactly, zero slack for carried

    out = append_review_state(body, state, max_chars=max_chars, carried_section=carried)

    assert len(out) <= max_chars + 1  # +1 for append_review_state's trailing newline
    assert out.startswith(body)
    assert "..." not in out
    assert "Carried from earlier runs" not in out
    assert parse_review_state(out).valid is True


def test_carried_dropped_entirely_when_budget_only_covers_body_and_marker():
    """A little slack beyond the exact fit still isn't enough for even a shortened carried section."""
    state = _single_active_state()
    marker = serialize_review_state(state)
    body = "x" * 400
    carried = "### Carried from earlier runs\n\n- **B** — `other.py:1` · first seen 2026-01-01 · " \
              "not re-reviewed this run"
    # One spare char beyond the exact fit: not enough for the "\n\n" separator plus any carried text.
    max_chars = len(body) + 1 + len(marker) + 3

    out = append_review_state(body, state, max_chars=max_chars, carried_section=carried)

    assert len(out) <= max_chars + 1
    assert out.startswith(body)
    assert "..." not in out
    assert "Carried from earlier runs" not in out
    assert parse_review_state(out).valid is True


def test_carried_section_is_truncated_with_ellipsis_when_partially_over_budget():
    """Enough slack for part of the carried section: body stays whole, carried is shortened."""
    state = _single_active_state()
    marker = serialize_review_state(state)
    body = "x" * 400
    carried = "### Carried from earlier runs\n\n" + "- **B** — `other.py:1` · not re-reviewed this run " * 5
    carried_room = 20  # enough for a truncated "..." remainder, not the whole carried section
    assert carried_room < len(carried)
    max_chars = len(body) + 2 + carried_room + len(marker) + 3

    out = append_review_state(body, state, max_chars=max_chars, carried_section=carried)

    assert len(out) <= max_chars + 1
    assert out.startswith(body)
    parsed = parse_review_state(out)
    assert parsed.valid is True
    human_only = out.split("<!-- pr-agent-review-state", 1)[0]
    truncated_carried = human_only[len(body):].strip("\n")
    assert truncated_carried.endswith("...")
    assert len(truncated_carried) == carried_room
    assert truncated_carried != carried[:carried_room]  # confirms it was actually shortened, not coincidental
