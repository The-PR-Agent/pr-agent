"""The 'since the last review' delta rendered from reconciliation results (#4777)."""

from pr_agent.algo.review_finding_state import (
    ReconciliationResult,
    render_lifecycle_summary,
)


def _finding(finding_id, path, state="ACTIVE", start=None, end=None):
    record = {"finding_id": finding_id, "state": state, "body": "body", "path": path}
    if start is not None:
        record["line_start"] = start
    if end is not None:
        record["line_end"] = end
    return record


def _result(current_findings, resolved=(), reopened=()):
    return ReconciliationResult(
        state={"findings": list(current_findings), "last_run": {"head_sha": "new"}},
        changed=True,
        resolved_ids=tuple(resolved),
        reopened_ids=tuple(reopened),
    )


def test_first_review_shows_no_section():
    previous = {"findings": [_finding("a", "a.py")], "last_run": {"head_sha": "old"}}
    assert render_lifecycle_summary(None, _result([_finding("a", "a.py")])) == ""
    assert render_lifecycle_summary(previous, None) == ""


def test_previous_state_with_no_active_findings_shows_no_section():
    previous = {"findings": [_finding("a", "a.py", state="RESOLVED")], "last_run": {"head_sha": "old"}}
    assert render_lifecycle_summary(previous, _result([])) == ""


def test_unchanged_review_shows_no_section():
    previous = {"findings": [_finding("a", "a.py"), _finding("b", "b.py")], "last_run": {"head_sha": "old"}}
    result = _result([_finding("a", "a.py"), _finding("b", "b.py")])
    assert render_lifecycle_summary(previous, result) == ""


def test_full_delta_lists_addressed_and_reappeared_with_locations():
    previous = {
        "findings": [
            _finding("a", "src/app.py", start=7, end=9),
            _finding("b", "lib.py"),
            _finding("c", "old.py", state="RESOLVED", start=3),
            _finding("d", "mod.py"),
        ],
        "last_run": {"head_sha": "old"},
    }
    result = _result(
        [_finding("b", "lib.py"), _finding("c", "old.py", start=3), _finding("d", "mod.py"),
         _finding("x", "new.py")],
        resolved=("a",),
        reopened=("c",),
    )

    assert render_lifecycle_summary(previous, result) == "\n".join([
        "**Since the last review** — 3 findings were open on the previous HEAD",
        "",
        "- ✅ 1 addressed by the pushes since then: src/app.py:7-9",
        "- ↩️ 1 reappeared after an earlier fix: old.py:3",
        "- ⏳ 2 still open",
        "- ✚ 1 newly found in this run",
    ])


def test_incremental_run_reports_only_what_it_can_know():
    # Resolution never fires on an incremental run, so the section reports the
    # carried-over and new findings without claiming anything was addressed.
    previous = {"findings": [_finding("a", "a.py"), _finding("b", "b.py")], "last_run": {"head_sha": "old"}}
    result = _result([_finding("a", "a.py"), _finding("n", "n.py")])

    assert render_lifecycle_summary(previous, result) == "\n".join([
        "**Since the last review** — 2 findings were open on the previous HEAD",
        "",
        "- ⏳ 1 still open",
        "- ✚ 1 newly found in this run",
    ])


def test_only_new_findings_still_announces_the_previous_count():
    previous = {"findings": [_finding("a", "a.py")], "last_run": {"head_sha": "old"}}
    result = _result([_finding("x", "x.py"), _finding("y", "y.py")])

    assert render_lifecycle_summary(previous, result) == "\n".join([
        "**Since the last review** — 1 findings were open on the previous HEAD",
        "",
        "- ✚ 2 newly found in this run",
    ])
