import json

from pr_agent.algo.review_finding_state import (
    append_review_state,
    append_review_state_paginated,
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


def test_overflow_carried_entries_appear_whole_in_continuation():
    """When the primary only has room for body+marker, carried entries move whole to continuation."""
    state = _single_active_state()
    marker = serialize_review_state(state)
    body = "x" * 400
    entry = "- **B** — `other.py:1` · first seen 2026-01-01 · not re-reviewed this run"
    carried = f"### Carried from earlier runs\n\n{entry}"
    # One spare char beyond the exact fit: not enough for the "\n\n" separator plus any carried text.
    max_chars = len(body) + 1 + len(marker) + 3

    primary, continuation = append_review_state_paginated(
        body, state, max_chars=max_chars, carried_section=carried
    )

    assert len(primary) <= max_chars + 1
    assert primary.startswith(body)
    assert "..." not in primary
    assert "Carried from earlier runs" not in primary.split("<!-- pr-agent-review-state", 1)[0]
    assert parse_review_state(primary).valid is True
    assert "### Carried from earlier runs (continued)" in continuation
    assert "Continued from the primary review comment." in continuation
    assert entry in continuation
    assert "<!-- pr-agent-review-state" not in continuation


def test_partial_budget_keeps_whole_entries_and_paginates_the_rest():
    """Enough slack for some whole entries: those stay on primary; overflow is not ellipsis-cut."""
    state = _single_active_state()
    marker = serialize_review_state(state)
    body = "x" * 400
    entries = [
        f"- **F{i}** — `f{i}.py:1` · first seen 2026-01-01 · not re-reviewed this run"
        for i in range(5)
    ]
    carried = "### Carried from earlier runs\n\n" + "\n".join(entries)
    # Room for heading + blank line + one whole entry, but not the full section.
    one_entry_section = "### Carried from earlier runs\n\n" + entries[0]
    carried_room = len(one_entry_section) + 5
    assert carried_room < len(carried)
    max_chars = len(body) + 2 + carried_room + len(marker) + 3

    primary, continuation = append_review_state_paginated(
        body, state, max_chars=max_chars, carried_section=carried
    )

    assert len(primary) <= max_chars + 1
    assert primary.startswith(body)
    parsed = parse_review_state(primary)
    assert parsed.valid is True
    human_only = primary.split("<!-- pr-agent-review-state", 1)[0]
    assert "..." not in human_only
    primary_entries = [line for line in human_only.splitlines() if line.startswith("- **")]
    assert primary_entries  # at least one whole entry fitted
    assert all(entry in entries for entry in primary_entries)
    cont_entries = [line for line in continuation.splitlines() if line.startswith("- **")]
    assert cont_entries
    assert all(entry in entries for entry in cont_entries)
    assert set(primary_entries) | set(cont_entries) == set(entries)
    assert set(primary_entries).isdisjoint(cont_entries)
    assert "<!-- pr-agent-review-state" not in continuation


def test_six_finding_state_visible_count_survives_tight_budget_via_continuation():
    findings = [
        {"path": f"lib/f{i}.dart", "line_start": i + 1, "line_end": i + 1,
         "body": f"**Issue {i}**\n\ndetail {i}"}
        for i in range(6)
    ]
    state = reconcile_review_findings(
        None, findings, allow_resolution=False, head_sha="s1", run_id="r1"
    ).state
    # Treat all six as carried (none reported this run).
    carried = render_carried_section(state, current_ids=set(), fully_reviewed_files=[])
    assert carried.count("- **") == 6
    marker = serialize_review_state(state)
    body = "review body"
    # Tight: body + marker + room for about two entries.
    first_entry = [line for line in carried.splitlines() if line.startswith("- **")][0]
    max_chars = len(body) + 2 + len(first_entry) * 2 + 20 + len(marker) + 3

    primary, continuation = append_review_state_paginated(
        body, state, max_chars=max_chars, carried_section=carried
    )

    visible = primary.count("- **") + continuation.count("- **")
    assert visible == 6
    assert parse_review_state(primary).valid is True
    assert "<!-- pr-agent-review-state" not in continuation
