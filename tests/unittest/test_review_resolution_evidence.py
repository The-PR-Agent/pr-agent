from pr_agent.algo.review_finding_state import parse_review_state, reconcile_review_findings, serialize_review_state

F = {"path": "lib/a.dart", "line_start": 10, "line_end": 12, "body": "**Bug**\n\nnull deref on empty list"}


def _first():
    return reconcile_review_findings(None, [F], allow_resolution=False, head_sha="s1", run_id="r1").state


def test_absent_finding_in_unreviewed_file_becomes_unconfirmed():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2",
                                       fully_reviewed_files=["lib/other.dart"])
    assert result.state["findings"][0]["state"] == "UNCONFIRMED"
    assert result.resolved_ids == ()


def test_absent_finding_in_fully_reviewed_file_resolves():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2",
                                       fully_reviewed_files=["lib/a.dart"])
    assert result.state["findings"][0]["state"] == "RESOLVED"
    assert result.resolved_ids != ()


def test_no_reviewed_files_given_never_resolves():
    result = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2")
    assert result.state["findings"][0]["state"] == "UNCONFIRMED"


def test_unconfirmed_reemitted_returns_to_active():
    unconfirmed = reconcile_review_findings(_first(), [], allow_resolution=True, head_sha="s2", run_id="r2").state
    result = reconcile_review_findings(unconfirmed, [F], allow_resolution=False, head_sha="s3", run_id="r3")
    assert result.state["findings"][0]["state"] == "ACTIVE"
    assert result.reopened_ids == ()  # UNCONFIRMED -> ACTIVE is not a reopen


def test_v1_marker_still_parses_and_upgrades():
    v1 = dict(_first(), schema_version=1)
    body = "review\n\n" + serialize_review_state(v1)
    parsed = parse_review_state(body)
    assert parsed.state is not None and parsed.state["schema_version"] == 1
    result = reconcile_review_findings(parsed.state, [F], allow_resolution=False, head_sha="s2", run_id="r2")
    assert result.state["schema_version"] == 2

    # A real pre-upgrade comment has "v1" in the wire-format prefix itself, not just in the
    # payload's schema_version field - serialize_review_state always writes the current global
    # version, so simulate the legacy prefix a genuinely old release would have written.
    legacy = body.replace("pr-agent-review-state:v2", "pr-agent-review-state:v1", 1)
    assert parse_review_state(legacy).valid is True
