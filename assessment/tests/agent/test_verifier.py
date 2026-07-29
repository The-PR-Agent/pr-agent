from __future__ import annotations

from assessment.ai_reviewer.contracts import Finding
from assessment.ai_reviewer.verifier import verify_findings
from assessment.tests.integration.fixtures import load_json, load_request


def test_verifier_rejects_out_of_scope_and_empty_evidence() -> None:
    request = load_request()
    raw = load_json("agent_findings.json")[0]

    findings, warnings = verify_findings(
        request,
        (
            {**raw, "line": 99},
            {**raw, "evidence": []},
        ),
    )

    assert findings == ()
    assert len(warnings) == 2


def test_verifier_drops_protected_and_static_duplicate_findings() -> None:
    request = load_request()
    raw_agent = load_json("agent_findings.json")[0]
    static = Finding.from_dict(load_json("static_findings.json")[0])
    duplicate = {
        **raw_agent,
        "category": static.category,
        "path": static.path,
        "line": static.line,
    }

    findings, warnings = verify_findings(
        request,
        (
            {**raw_agent, "protected_by_existing_logic": True},
            duplicate,
        ),
        (static,),
    )

    assert findings == ()
    assert warnings == (
        "candidate 0 already has protection",
        "candidate 1 duplicates a static finding",
    )
