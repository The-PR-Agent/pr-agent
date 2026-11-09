"""The 'since the last review' delta rendered from reconciliation results (#4777)."""

from pr_agent.algo.findings import Finding
from pr_agent.algo.review_finding_state import (
    ReconciliationResult,
    normalize_findings,
    reconcile_review_findings,
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
        "- ✅ 1 addressed by the pushes since then: `src/app.py:7-9`",
        "- ↩️ 1 reappeared after an earlier fix: `old.py:3`",
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
        "**Since the last review** — 1 finding was open on the previous HEAD",
        "",
        "- ✚ 2 newly found in this run",
    ])


def test_summary_from_real_reconcile_output_does_not_count_resolved_records_as_open():
    # reconcile_review_findings keeps RESOLVED records in the state for the resolved
    # section, so "still open" must count ACTIVE records only: with two findings open and
    # one addressed, the section says 1 still open, not 2.
    previous = {
        "findings": normalize_findings([
            Finding(path="a.py", body="alpha issue"),
            Finding(path="b.py", body="beta issue"),
        ]),
        "last_run": {"head_sha": "old"},
    }
    result = reconcile_review_findings(
        previous,
        [Finding(path="a.py", body="alpha issue")],
        allow_resolution=True,
        head_sha="new",
    )

    assert any(f.get("state") == "RESOLVED" for f in result.state["findings"])
    assert render_lifecycle_summary(previous, result) == "\n".join([
        "**Since the last review** — 2 findings were open on the previous HEAD",
        "",
        "- ✅ 1 addressed by the pushes since then: `b.py`",
        "- ⏳ 1 still open",
    ])


def test_reappearance_alone_shows_a_section_without_an_open_count_header():
    earlier = reconcile_review_findings(
        None, [Finding(path="c.py", body="gamma issue")], allow_resolution=False, head_sha="h1"
    )
    resolved_state = reconcile_review_findings(
        earlier.state, [], allow_resolution=True, head_sha="h2"
    )
    result = reconcile_review_findings(
        resolved_state.state,
        [Finding(path="c.py", body="gamma issue")],
        allow_resolution=True,
        head_sha="h3",
    )

    assert result.reopened_ids
    assert render_lifecycle_summary(resolved_state.state, result) == (
        "- ↩️ 1 reappeared after an earlier fix: `c.py`"
    )


def test_locations_with_a_newline_or_the_state_marker_are_neutralized():
    # _markdown_code_span escapes newlines so a crafted path cannot leave the span, and
    # the state-marker opener is broken up so append_review_state never mistakes a path
    # for the hidden marker.
    previous = {
        "findings": normalize_findings([
            Finding(path="a.py\n恶意.rb", body="newline path"),
            Finding(path="evil<!-- pr-agent-review-state: bad", body="marker path"),
        ]),
        "last_run": {"head_sha": "old"},
    }
    result = reconcile_review_findings(
        previous, [Finding(path="c.py", body="new issue")], allow_resolution=True, head_sha="new"
    )

    summary = render_lifecycle_summary(previous, result)
    assert "\n" in summary and "\n恶意.rb" not in summary
    assert "<\!" in summary and "<!-- pr-agent-review-state" not in summary
