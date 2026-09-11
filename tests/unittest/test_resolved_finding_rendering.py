"""A resolved finding must read the way it read while it was active.

The reviewer renders a key issue as a header, a blank line and the model's text, which often
contains a fenced code block. Normalisation collapsed every newline, so the resolved section
printed the whole finding - fence included - on one line.
"""
from pr_agent.algo.review_finding_state import (
    _render_resolved_section,
    normalize_finding,
    reconcile_review_findings,
)
from pr_agent.tools.pr_reviewer import PRReviewer

ISSUE = {
    "relevant_file": "src/app.py",
    "issue_header": "Possible Bug",
    "issue_content": "The loop never ends:\n```suggestion\nwhile attempts < 3:\n    attempts += 1\n```",
    "start_line": 3,
    "end_line": 4,
}


def _resolved_section_for(issue):
    finding = PRReviewer._review_finding_from_issue(issue)
    active = reconcile_review_findings(None, [finding], allow_resolution=False, head_sha="aaa111").state
    resolved = reconcile_review_findings(active, [], allow_resolution=True, head_sha="bbb222").state
    return _render_resolved_section(resolved)


def test_a_resolved_finding_keeps_its_code_block():
    rendered = _resolved_section_for(ISSUE)

    assert "```text\nwhile attempts < 3:\n    attempts += 1\n```" in rendered


def test_a_resolved_finding_keeps_its_header_on_its_own_line():
    rendered = _resolved_section_for(ISSUE)

    assert "**Possible Issue**\n\nThe loop never ends:" in rendered


def test_the_fingerprint_still_ignores_whitespace():
    """Re-wrapped prose is the same finding, so the lifecycle survives a reflow."""
    one_line = normalize_finding({"path": "src/app.py", "body": "The loop never   ends here."})
    wrapped = normalize_finding({"path": "src/app.py", "body": "The loop never\nends here."})

    assert one_line["finding_id"] == wrapped["finding_id"]


def test_the_body_keeps_its_own_line_breaks():
    finding = normalize_finding({"path": "src/app.py", "body": "First line.\n\nSecond line."})

    assert finding["body"] == "First line.\n\nSecond line."


def test_a_single_line_finding_is_unchanged():
    """Control: prose without line breaks is stored exactly as before."""
    finding = normalize_finding({"path": "src/app.py", "body": "The retry loop never ends."})

    assert finding["body"] == "The retry loop never ends."
