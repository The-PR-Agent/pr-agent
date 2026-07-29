from __future__ import annotations

import json
import subprocess
from pathlib import Path

from assessment.ai_reviewer.contracts import ReviewRequest
from assessment.ai_reviewer.static_scan import scan


def test_scan_passes_only_changed_files_and_filters_lines(
    tmp_path: Path,
) -> None:
    changed = tmp_path / "src" / "changed.py"
    changed.parent.mkdir()
    changed.write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "src" / "unchanged.py").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    request = _request(
        changed_files=("src/changed.py",),
        changed_lines={"src/changed.py": frozenset({2})},
    )
    captured: list[str] = []

    def runner(command, **kwargs):
        captured.extend(command)
        payload = {
            "results": [
                _result("src/changed.py", 1),
                _result("src/changed.py", 2),
            ],
            "errors": [],
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    findings, warnings = scan(request, tmp_path, runner=runner)

    assert not warnings
    assert [finding.line for finding in findings] == [2]
    assert "src/changed.py" in captured
    assert "src/unchanged.py" not in captured


def test_timeout_returns_warning_instead_of_finding(tmp_path: Path) -> None:
    target = tmp_path / "changed.py"
    target.write_text("value = 1\n", encoding="utf-8")
    request = _request(
        changed_files=("changed.py",),
        changed_lines={"changed.py": frozenset({1})},
    )

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    findings, warnings = scan(request, tmp_path, runner=runner)

    assert findings == []
    assert warnings == ["semgrep exceeded the 90-second timeout"]


def _request(
    changed_files: tuple[str, ...],
    changed_lines: dict[str, frozenset[int]],
) -> ReviewRequest:
    return ReviewRequest(
        repository="owner/repo",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Static smoke",
        body="Synthetic request",
        diff="synthetic diff",
        changed_lines=changed_lines,
        changed_files=changed_files,
    )


def _result(path: str, line: int) -> dict:
    return {
        "check_id": "ai-review.synthetic",
        "path": path,
        "start": {"line": line},
        "extra": {
            "severity": "WARNING",
            "message": "Synthetic changed-line match",
            "metadata": {
                "title": "Synthetic static issue",
                "category": "static",
                "impact": "The changed code produces an invalid result.",
                "suggestion": "Correct the changed operation.",
            },
        },
    }
