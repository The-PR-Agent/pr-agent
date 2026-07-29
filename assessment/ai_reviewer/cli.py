from __future__ import annotations

import argparse
import hashlib
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .budget import Budget, BudgetExpired
from .contracts import Finding, ReviewRequest, ReviewResult
from .diff_scope import filter_publishable, parse_changed_scope
from .github_runtime import PullRequestEvent, load_event
from .publisher import PublishReport, publish_findings
from .run_artifact import save_artifact
from .upstream_adapter import UpstreamAdapter

StaticScan = Callable[
    [ReviewRequest, Path, int],
    tuple[list[Finding], list[str]],
]
AgentReview = Callable[
    [ReviewRequest, list[Finding], Path, float],
    ReviewResult,
]


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    event_loader: Callable[[str | Path], PullRequestEvent] = load_event
    adapter_factory: Callable[
        [PullRequestEvent, str],
        UpstreamAdapter,
    ] = UpstreamAdapter.from_token
    publisher: Callable[
        [UpstreamAdapter, ReviewRequest, tuple[Finding, ...]],
        PublishReport,
    ] = publish_findings
    artifact_saver: Callable[
        [ReviewResult, str | Path],
        Path,
    ] = save_artifact
    monotonic: Callable[[], float] = time.monotonic


def run_pipeline(
    *,
    event_path: str | Path,
    artifact_dir: str | Path,
    repo_root: str | Path,
    token: str,
    model: str,
    mode: str,
    static_scan: StaticScan | None,
    agent_review: AgentReview | None,
    dependencies: PipelineDependencies = PipelineDependencies(),
) -> ReviewResult:
    started_at = _utc_now()
    started_monotonic = dependencies.monotonic()
    run_id = str(uuid.uuid4())
    errors: list[str] = []
    trace: list[dict[str, object]] = []
    findings: list[Finding] = []
    request: ReviewRequest | None = None
    adapter: UpstreamAdapter | None = None
    status = "success"
    result_model = model
    budget = Budget(limit_seconds=540.0, clock=dependencies.monotonic)
    root = Path(repo_root).resolve(strict=True)

    try:
        event = dependencies.event_loader(event_path)
        if not event.same_repository:
            raise ValueError("external fork pull requests are not enabled")
        adapter = dependencies.adapter_factory(event, token)
        diff = adapter.unified_diff()
        changed_files, changed_lines = parse_changed_scope(diff)
        request = ReviewRequest(
            repository=event.repository,
            pr_number=event.pr_number,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            title=event.title,
            body=event.body,
            diff=diff,
            changed_lines=changed_lines,
            changed_files=changed_files,
        )
        trace.append({"stage": "analyze", "status": "completed"})

        static_findings: list[Finding] = []
        if mode == "probe":
            findings.append(_probe_finding(request))
            trace.append(
                {
                    "stage": "review",
                    "status": "completed",
                    "mode": "probe",
                }
            )
        elif mode == "full":
            if static_scan is None or agent_review is None:
                raise RuntimeError(
                    "full mode requires static and Agent modules"
                )
            try:
                budget.require_start("static scan")
                scan_timeout = max(
                    1,
                    min(90, int(budget.remaining_seconds())),
                )
                static_findings, static_warnings = static_scan(
                    request,
                    root,
                    scan_timeout,
                )
                findings.extend(static_findings)
                errors.extend(
                    f"static warning: {warning[:300]}"
                    for warning in static_warnings
                )
                trace.append(
                    {
                        "stage": "static_scan",
                        "status": "completed",
                        "finding_count": len(static_findings),
                        "warning_count": len(static_warnings),
                    }
                )
            except BudgetExpired:
                raise
            except Exception as error:
                status = "failed"
                errors.append(_error("static", error))
            try:
                budget.require_start("Agent review")
                agent_result = agent_review(
                    request,
                    static_findings,
                    root,
                    budget.deadline,
                )
                result_model = agent_result.model
                findings.extend(agent_result.findings)
                trace.extend(dict(item) for item in agent_result.trace_summary)
                errors.extend(agent_result.errors)
                if agent_result.status == "timeout":
                    status = "timeout"
                elif agent_result.status == "failed":
                    status = "failed"
            except BudgetExpired:
                raise
            except Exception as error:
                status = "failed"
                errors.append(_error("agent", error))
        else:
            raise ValueError(f"unsupported mode: {mode}")
    except BudgetExpired as error:
        status = "timeout"
        errors.append(_error("budget", error))
    except Exception as error:
        status = "failed"
        errors.append(_error("pipeline", error))

    publishable: tuple[Finding, ...] = ()
    if request is not None:
        publishable = filter_publishable(
            findings,
            request.changed_lines,
        )
        trace.append(
            {
                "stage": "verify",
                "status": "completed",
                "publishable_count": len(publishable),
            }
        )
    if adapter is not None and request is not None and publishable:
        try:
            report = dependencies.publisher(adapter, request, publishable)
            trace.append(
                {
                    "stage": "publish",
                    "status": "completed",
                    "published": report.published,
                    "skipped": report.skipped,
                }
            )
        except Exception as error:
            status = "failed"
            errors.append(_error("publisher", error))

    finished_at = _utc_now()
    result = ReviewResult(
        run_id=run_id,
        model=result_model,
        status=status,
        started_at=started_at.isoformat().replace("+00:00", "Z"),
        finished_at=finished_at.isoformat().replace("+00:00", "Z"),
        duration_seconds=max(
            0.0,
            dependencies.monotonic() - started_monotonic,
        ),
        findings=publishable,
        errors=tuple(errors),
        trace_summary=tuple(trace),
    )
    dependencies.artifact_saver(result, artifact_dir)
    return result


def _full_modules() -> tuple[StaticScan, AgentReview]:
    from .agent_loop import review
    from .static_scan import scan

    return scan, review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--mode",
        choices=("probe", "full"),
        default="full",
    )
    arguments = parser.parse_args()

    static_scan: StaticScan | None = None
    agent_review: AgentReview | None = None
    if arguments.mode == "full":
        try:
            static_scan, agent_review = _full_modules()
        except ImportError:
            static_scan = None
            agent_review = None

    result = run_pipeline(
        event_path=arguments.event,
        artifact_dir=arguments.artifact_dir,
        repo_root=arguments.repo_root,
        token=os.environ.get("GITHUB_TOKEN", ""),
        model=os.environ.get("AI_REVIEW_MODEL", "deepseek-v4-pro"),
        mode=arguments.mode,
        static_scan=static_scan,
        agent_review=agent_review,
    )
    return 0 if result.status == "success" else 1


def _probe_finding(request: ReviewRequest) -> Finding:
    for path in request.changed_files:
        lines = sorted(request.changed_lines.get(path, ()))
        if lines:
            return Finding(
                path=path,
                line=lines[0],
                category="static",
                severity="low",
                confidence=1.0,
                title="AI review pipeline probe",
                evidence=(
                    "The controlled workflow reached the verified "
                    "publication stage.",
                ),
                impact=(
                    "This synthetic finding validates the integration path "
                    "only."
                ),
                suggestion=(
                    "Use full mode after Agent and static modules pass "
                    "their smoke checks."
                ),
                source="syntax",
            )
    raise ValueError("probe requires at least one added head line")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error(stage: str, error: Exception) -> str:
    message = str(error)[:300]
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
    return f"{stage}: {type(error).__name__}; error_id={digest}; {message}"


if __name__ == "__main__":
    raise SystemExit(main())
