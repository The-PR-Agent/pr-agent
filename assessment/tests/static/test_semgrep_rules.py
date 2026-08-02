from __future__ import annotations

import sys
from pathlib import Path

import pytest

from assessment.ai_reviewer.contracts import ReviewRequest
from assessment.ai_reviewer.static_scan import scan


def test_repository_rules_detect_static_and_security_fixtures() -> None:
    executable = Path(sys.executable).with_name(
        "semgrep.exe" if sys.platform == "win32" else "semgrep"
    )
    if not executable.is_file():
        pytest.skip("pinned Semgrep executable is not installed")
    files = (
        "assessment/tests/fixtures/categories/static/python_bare_except.py",
        "assessment/tests/fixtures/categories/security/python_shell_true.py",
    )
    request = ReviewRequest(
        repository="owner/repo",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Semgrep smoke",
        body="Synthetic request",
        diff="synthetic diff",
        changed_lines={
            files[0]: frozenset({4}),
            files[1]: frozenset({5}),
        },
        changed_files=files,
    )

    findings, warnings = scan(
        request,
        Path.cwd(),
        executable=str(executable),
    )

    assert warnings == []
    assert {finding.category for finding in findings} == {
        "static",
        "security",
    }
