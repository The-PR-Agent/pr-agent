from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from assessment.ai_reviewer import Finding, ReviewRequest, ReviewResult
from assessment.ai_reviewer.contracts import CATEGORIES, SOURCES
from assessment.tests.integration.fixtures import (
    load_json,
    load_request,
    load_result,
)


def test_request_fixture_round_trips_without_changing_json_shape() -> None:
    raw_request = load_json("review_request.json")

    request = load_request()

    assert request.repository == "JiXia830/pr-agent"
    assert request.changed_lines["src/calculator.py"] == frozenset(
        {10, 11, 12}
    )
    assert request.to_dict() == raw_request


def test_finding_fixtures_cover_frozen_categories_and_sources() -> None:
    raw_findings = load_json("static_findings.json") + load_json(
        "agent_findings.json"
    )
    findings = tuple(Finding.from_dict(item) for item in raw_findings)

    assert {finding.category for finding in findings} == CATEGORIES == {
        "static",
        "business_logic",
        "logic",
        "memory",
        "security",
        "architecture",
    }
    assert {finding.source for finding in findings} == SOURCES == {
        "agent",
        "semgrep",
        "syntax",
    }


def test_finding_rejects_unknown_category() -> None:
    raw_finding = load_json("static_findings.json")[0]

    with pytest.raises(ValueError, match="category"):
        Finding.from_dict({**raw_finding, "category": "style"})


def test_finding_rejects_confidence_above_one() -> None:
    raw_finding = load_json("static_findings.json")[0]

    with pytest.raises(ValueError, match="confidence"):
        Finding.from_dict({**raw_finding, "confidence": 1.1})


def test_contracts_are_frozen_and_defensively_copy_input_containers() -> None:
    changed_line_values = [10, 11]
    changed_lines = {"src/calculator.py": changed_line_values}
    changed_files = ["src/calculator.py"]
    request = ReviewRequest(
        repository="JiXia830/pr-agent",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Handle zero divisors",
        body="Synthetic contract fixture.",
        diff="synthetic diff",
        changed_lines=changed_lines,
        changed_files=changed_files,
    )
    evidence = ["Initial evidence."]
    finding = Finding(
        path="src/calculator.py",
        line=10,
        category="logic",
        severity="medium",
        confidence=0.8,
        title="Synthetic finding",
        evidence=evidence,
        impact="Synthetic impact.",
        suggestion="Synthetic suggestion.",
        source="agent",
    )
    finding_values = [finding]
    errors = ["initial error"]
    trace_summary = [
        {
            "stage": "review",
            "details": {"tags": ["initial", {"state": "kept"}]},
        }
    ]
    result = ReviewResult(
        run_id="00000000-0000-4000-8000-000000000017",
        model="glm-5.2",
        status="success",
        started_at="2026-07-29T10:00:00Z",
        finished_at="2026-07-29T10:00:02.500000Z",
        duration_seconds=2.5,
        findings=finding_values,
        errors=errors,
        trace_summary=trace_summary,
    )

    changed_line_values.append(12)
    changed_lines["other.py"] = [1]
    changed_files.append("other.py")
    evidence.append("Late evidence.")
    finding_values.clear()
    errors.append("late error")
    trace_summary[0]["stage"] = "mutated"
    trace_summary[0]["details"]["tags"].append("late")
    trace_summary[0]["details"]["tags"][1]["state"] = "mutated"

    assert request.changed_lines == {"src/calculator.py": frozenset({10, 11})}
    assert request.changed_files == ("src/calculator.py",)
    assert finding.evidence == ("Initial evidence.",)
    assert result.findings == (finding,)
    assert result.errors == ("initial error",)
    assert result.to_dict()["trace_summary"] == [
        {
            "stage": "review",
            "details": {"tags": ["initial", {"state": "kept"}]},
        }
    ]

    with pytest.raises(FrozenInstanceError):
        request.title = "Changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        finding.severity = "high"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.trace_summary[0]["stage"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        tags = result.trace_summary[0]["details"]["tags"]
        tags[1]["state"] = "changed"  # type: ignore[index]


def test_review_result_fixture_round_trips_with_success_status() -> None:
    raw_result = load_json("review_result.json")

    result = load_result()

    assert result.status == "success"
    assert result.to_dict() == raw_result
