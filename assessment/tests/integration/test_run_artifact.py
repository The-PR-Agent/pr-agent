from __future__ import annotations

from pathlib import Path

from assessment.ai_reviewer.contracts import ReviewResult
from assessment.ai_reviewer.run_artifact import save_artifact
from assessment.tests.integration.fixtures import load_result


def test_artifact_is_atomic_bounded_and_redacted(tmp_path: Path) -> None:
    fixture = load_result()
    fake_key = "sk-" + "X" * 32
    result = ReviewResult(
        run_id=fixture.run_id,
        model=fixture.model,
        status=fixture.status,
        started_at=fixture.started_at,
        finished_at=fixture.finished_at,
        duration_seconds=fixture.duration_seconds,
        findings=fixture.findings,
        errors=(
            f"Authorization: Bearer {fake_key}",
            "token=not-a-real-token C:\\Users\\Example\\secret.txt",
        ),
        trace_summary=(
            {"api_key": fake_key, "path": "/home/example/private.py"},
        ),
    )

    artifact = save_artifact(result, tmp_path)
    text = artifact.read_text(encoding="utf-8")

    assert "[REDACTED]" in text
    assert fake_key not in text
    assert "C:\\Users\\Example" not in text
    assert "/home/example" not in text
    assert not list(tmp_path.glob("*.tmp"))
