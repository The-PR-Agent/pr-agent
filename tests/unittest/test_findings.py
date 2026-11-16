from pr_agent.algo.findings import Finding
from pr_agent.algo.inline_comment_dedup import key_issue_fingerprint
from pr_agent.algo.review_finding_state import normalize_finding, reconcile_review_findings


def test_review_finding_conversion_preserves_legacy_fingerprint_and_confidence():
    finding = Finding.from_review_issue(
        {
            "relevant_file": "src/app.py",
            "issue_header": "Possible Bug",
            "issue_content": "The lock is never released:\n```suggestion\nlock.release()\n```",
            "start_line": 4,
            "end_line": 5,
            "confidence": 8,
        }
    )

    normalized = normalize_finding(finding)

    assert finding.confidence == 8
    assert normalized["body"] == (
        "**Possible Issue**\n\nThe lock is never released:\n```text\nlock.release()\n```"
    )
    assert normalized["finding_id"] == key_issue_fingerprint(
        "src/app.py",
        "**possible issue** the lock is never released: ```text lock.release() ```",
    )


def test_code_suggestion_finding_can_be_resolved_and_reopened():
    finding = Finding.from_code_suggestion(
        {
            "relevant_file": "src/app.py",
            "relevant_lines_start": 12,
            "relevant_lines_end": 13,
            "suggestion_content": "Handle the missing value.",
            "label": "possible bug",
            "existing_code": "return value",
            "improved_code": "return value or default",
            "score": 8,
        }
    )
    assert finding is not None
    assert (finding.path, finding.line_start, finding.line_end) == ("src/app.py", 12, 13)
    assert finding.label == "possible bug"
    assert finding.existing_code == "return value"
    assert finding.improved_code == "return value or default"
    assert finding.score == 8

    first = reconcile_review_findings(
        None, [finding], allow_resolution=False, head_sha="head-1"
    )
    resolved = reconcile_review_findings(
        first.state, [], allow_resolution=True, head_sha="head-2"
    )
    reopened = reconcile_review_findings(
        resolved.state, [finding], allow_resolution=False, head_sha="head-3"
    )

    assert first.state["findings"][0]["body"] == "Handle the missing value."
    assert resolved.state["findings"][0]["state"] == "RESOLVED"
    assert reopened.state["findings"][0]["state"] == "ACTIVE"
    assert reopened.state["findings"][0]["reopened_count"] == 1


def test_from_review_issue_carries_fix():
    issue = {
        "relevant_file": "src/app.py",
        "issue_header": "Possible Bug",
        "issue_content": "The lock is never released.",
        "start_line": 4,
        "end_line": 5,
        "existing_code": "lock.acquire()",
        "improved_code": "with lock:\n    pass",
    }

    finding = Finding.from_review_issue(issue)
    without_fix = Finding.from_review_issue({**issue, "existing_code": 3, "improved_code": None})

    assert (finding.existing_code, finding.improved_code) == ("lock.acquire()", "with lock:\n    pass")
    assert (without_fix.existing_code, without_fix.improved_code) == (None, None)
