import json
import pytest
from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    serialize_review_state,
)


def _finding(body="The lock is never released.", path="app.py", start=10, end=10):
    return {
        "body": body,
        "path": path,
        "line_start": start,
        "line_end": end,
    }


def test_finding_identity_ignores_small_text_normalization_changes():
    first = reconcile_review_findings(
        None,
        [_finding("The   lock is never released.")],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    )
    second = reconcile_review_findings(
        first.state,
        [_finding("the lock is never released.")],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    )

    assert second.state["findings"][0]["finding_id"] == first.state["findings"][0]["finding_id"]
    assert second.state["findings"][0]["state"] == "ACTIVE"


def test_first_run_marks_current_findings_active_without_resolving_anything():
    result = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        head_sha="head-1",
        timestamp="2026-01-01T00:00:00Z",
    )

    assert result.changed is True
    assert result.resolved_ids == ()
    assert result.state["findings"][0]["state"] == "ACTIVE"
    assert result.state["findings"][0]["first_seen"] == "2026-01-01T00:00:00Z"


def test_complete_full_review_marks_absent_active_finding_resolved():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state

    result = reconcile_review_findings(
        previous,
        [],
        allow_resolution=True,
        head_sha="head-2",
        run_id="run-2",
        timestamp="2026-01-01T00:01:00Z",
    )

    finding = result.state["findings"][0]
    assert finding["state"] == "RESOLVED"
    assert finding["resolved_head_sha"] == "head-2"
    assert finding["resolution_run_id"] == "run-2"
    assert result.resolved_ids == (finding["finding_id"],)


def test_incremental_absence_is_not_negative_evidence():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state

    result = reconcile_review_findings(
        previous,
        [],
        allow_resolution=False,
        timestamp="2026-01-01T00:01:00Z",
    )

    assert result.state["findings"][0]["state"] == "ACTIVE"
    assert result.resolved_ids == ()


def test_token_budget_exclusion_is_not_negative_evidence():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state

    result = reconcile_review_findings(
        previous,
        [],
        allow_resolution=False,
        excluded_files=["app.py"],
        timestamp="2026-01-01T00:01:00Z",
    )

    assert result.state["findings"][0]["state"] == "ACTIVE"
    assert result.state["last_run"]["excluded_files"] == ["app.py"]


def test_resolved_finding_reappearing_becomes_active_with_reopen_metadata():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    previous = reconcile_review_findings(
        previous,
        [],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    ).state

    result = reconcile_review_findings(
        previous,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:02:00Z",
    )

    finding = result.state["findings"][0]
    assert finding["state"] == "ACTIVE"
    assert finding["reopened_count"] == 1
    assert "reopened_at" in finding
    assert result.reopened_ids == (finding["finding_id"],)


def test_multiple_findings_reconcile_independently():
    finding_a = _finding("A", "a.py")
    finding_b = _finding("B", "b.py")
    previous = reconcile_review_findings(
        None,
        [finding_a, finding_b],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state

    result = reconcile_review_findings(
        previous,
        [finding_a, _finding("C", "c.py")],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    )
    states = {item["path"]: item["state"] for item in result.state["findings"]}

    assert states == {"a.py": "ACTIVE", "b.py": "RESOLVED", "c.py": "ACTIVE"}


def test_state_marker_round_trips_deterministically():
    state = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state

    body = serialize_review_state(state)
    parsed = parse_review_state(f"review\n{body}")

    assert parsed.present is True
    assert parsed.valid is True
    assert parsed.state == state
    assert serialize_review_state(parsed.state) == body


def test_missing_state_marker_is_a_valid_cold_start():
    parsed = parse_review_state("## PR Reviewer Guide\n\nCurrent review")

    assert parsed.present is False
    assert parsed.valid is True
    assert parsed.state is None


@pytest.mark.parametrize(
    "body",
    [
        "review\n<!-- pr-agent-review-state:v1\nnot-json\n-->",
        "review\n<!-- pr-agent-review-state:v1\n{\"schema_version\":1",
        "review\n<!-- pr-agent-review-state:v2\n{}\n-->",
        "review\n<!-- pr-agent-review-state:v1\n{}\n-->\n<!-- pr-agent-review-state:v1\n{}\n-->",
    ],
)
def test_invalid_state_marker_fails_closed(body):
    parsed = parse_review_state(body)

    assert parsed.present is True
    assert parsed.valid is False
    assert parsed.state is None


def test_unknown_finding_state_fails_closed():
    payload = {
        "schema_version": 1,
        "findings": [{
            "finding_id": "abc",
            "state": "UNKNOWN",
            "body": "body",
            "path": "app.py",
        }],
        "last_run": {},
    }
    parsed = parse_review_state(
        f"<!-- pr-agent-review-state:v1\n{json.dumps(payload)}\n-->"
    )

    assert parsed.present is True
    assert parsed.valid is False
    assert parsed.state is None


def test_resolved_render_is_collapsed_and_state_marker_is_hidden():
    previous = reconcile_review_findings(
        None,
        [_finding()],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    resolved = reconcile_review_findings(
        previous,
        [],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
    ).state

    body = append_review_state("## PR Reviewer Guide\n\nCurrent review", resolved)

    assert "<details>" in body
    assert "Resolved findings" in body
    assert "The lock is never released." in body
    assert "<!-- pr-agent-review-state:v1" in body


def test_resolved_retention_never_drops_active_findings():
    active = _finding("active", "active.py")
    old = [
        _finding(f"resolved-{index}", f"resolved-{index}.py")
        for index in range(25)
    ]
    previous = reconcile_review_findings(
        None,
        [active, *old],
        allow_resolution=True,
        timestamp="2026-01-01T00:00:00Z",
    ).state
    previous = reconcile_review_findings(
        previous,
        [active],
        allow_resolution=True,
        timestamp="2026-01-01T00:01:00Z",
        max_resolved_findings=20,
    ).state

    paths = {item["path"] for item in previous["findings"]}
    resolved = [item for item in previous["findings"] if item["state"] == "RESOLVED"]

    assert "active.py" in paths
    assert len(resolved) == 20


def test_invalid_reopen_metadata_fails_closed():
    payload = {
        "schema_version": 1,
        "findings": [{
            "finding_id": "abc",
            "state": "RESOLVED",
            "body": "body",
            "path": "app.py",
            "reopened_count": "not-an-int",
        }],
        "last_run": {},
    }
    parsed = parse_review_state(f"<!-- pr-agent-review-state:v1\n{json.dumps(payload)}\n-->")
    assert parsed.present is True
    assert parsed.valid is False
    assert parsed.state is None
