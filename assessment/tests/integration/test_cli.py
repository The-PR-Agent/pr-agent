from __future__ import annotations

from pathlib import Path

from assessment.ai_reviewer.cli import PipelineDependencies, run_pipeline
from assessment.ai_reviewer.contracts import Finding, ReviewResult
from assessment.ai_reviewer.github_runtime import load_event
from assessment.ai_reviewer.publisher import PublishReport
from assessment.tests.integration.fixtures import load_json


class FakeAdapter:
    def unified_diff(self) -> str:
        return Path(
            "assessment/tests/integration/fixtures/sample.diff"
        ).read_text(encoding="utf-8")


def test_full_pipeline_integrates_static_and_agent_results(
    tmp_path: Path,
) -> None:
    event_path = Path(
        "assessment/tests/integration/fixtures/pull_request_event.json"
    )
    static_finding = Finding.from_dict(load_json("static_findings.json")[0])
    agent_finding = Finding.from_dict(load_json("agent_findings.json")[1])
    saved: list[ReviewResult] = []
    published: list[tuple[Finding, ...]] = []

    def static_scan(request, repo_root, timeout_seconds):
        assert timeout_seconds == 90
        return [static_finding], []

    def agent_review(request, static_findings, repo_root, deadline):
        assert static_findings == [static_finding]
        return ReviewResult(
            run_id="agent-run",
            model="deepseek-v4-pro",
            status="success",
            started_at="2026-07-29T10:00:00Z",
            finished_at="2026-07-29T10:00:01Z",
            duration_seconds=1.0,
            findings=(agent_finding,),
            errors=(),
            trace_summary=({"stage": "return", "status": "completed"},),
        )

    def publisher(adapter, request, findings):
        published.append(findings)
        return PublishReport(published=len(findings), skipped=0)

    def artifact_saver(result, directory):
        saved.append(result)
        return Path(directory) / "result.json"

    dependencies = PipelineDependencies(
        event_loader=lambda _: load_event(event_path),
        adapter_factory=lambda event, token: FakeAdapter(),
        publisher=publisher,
        artifact_saver=artifact_saver,
        monotonic=lambda: 100.0,
    )

    result = run_pipeline(
        event_path=event_path,
        artifact_dir=tmp_path,
        repo_root=Path.cwd(),
        token="test-token",
        model="deepseek-v4-pro",
        mode="full",
        static_scan=static_scan,
        agent_review=agent_review,
        dependencies=dependencies,
    )

    assert result.status == "success"
    assert result.findings == (agent_finding, static_finding)
    assert published == [result.findings]
    assert saved == [result]
