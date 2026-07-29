from __future__ import annotations

from pathlib import Path

import pytest

from assessment.ai_reviewer.contracts import Finding
from assessment.ai_reviewer.diff_scope import (
    filter_publishable,
    parse_changed_scope,
    safe_diff_path,
)
from assessment.tests.integration.fixtures import (
    load_json,
    load_request,
)


def test_sample_diff_uses_only_target_added_lines() -> None:
    diff = Path(
        "assessment/tests/integration/fixtures/sample.diff"
    ).read_text(encoding="utf-8")

    files, changed_lines = parse_changed_scope(diff)

    assert files == ("src/calculator.py",)
    assert changed_lines == {
        "src/calculator.py": frozenset({10, 11, 12})
    }


@pytest.mark.parametrize(
    "path",
    (
        "../../secret.py",
        "b/../secret.py",
        "/absolute.py",
        "C:\\absolute.py",
        "/dev/null",
    ),
)
def test_diff_path_rejects_traversal_and_absolute_paths(path: str) -> None:
    with pytest.raises(ValueError, match="unsafe diff path"):
        safe_diff_path(path)


def test_publishable_guard_filters_and_sorts() -> None:
    request = load_request()
    raw = load_json("agent_findings.json")
    findings = [Finding.from_dict(item) for item in raw]
    out_of_scope = Finding.from_dict({**raw[0], "line": 99})

    publishable = filter_publishable(
        [out_of_scope, *findings],
        request.changed_lines,
    )

    assert out_of_scope not in publishable
    assert publishable[0].severity == "high"
