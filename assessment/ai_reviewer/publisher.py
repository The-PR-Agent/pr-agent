from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .contracts import Finding, ReviewRequest

MAX_COMMENTS = 8


class StaleHeadError(RuntimeError):
    """Raised when publication would target an obsolete pull request head."""


class PublisherAdapter(Protocol):
    def current_head_sha(self) -> str: ...

    def existing_review_bodies(self) -> tuple[str, ...]: ...

    def create_review(
        self,
        head_sha: str,
        comments: list[dict[str, object]],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishReport:
    published: int
    skipped: int


def run_key(repository: str, pr_number: int, head_sha: str) -> str:
    return _digest(
        {
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": head_sha,
        }
    )


def finding_fingerprint(
    request: ReviewRequest,
    finding: Finding,
) -> str:
    return _digest(
        {
            "repository": request.repository,
            "pr_number": request.pr_number,
            "head_sha": request.head_sha,
            "path": finding.path,
            "line": finding.line,
            "category": finding.category,
            "source": finding.source,
            "title": " ".join(finding.title.split()).casefold(),
        }
    )


def marker(fingerprint: str) -> str:
    return f"<!-- ai-review:{fingerprint} -->"


def render_body(request: ReviewRequest, finding: Finding) -> str:
    evidence = "\n".join(
        f"- {_truncate(item, 1000)}"
        for item in finding.evidence[:5]
    )
    fingerprint = finding_fingerprint(request, finding)
    return (
        f"**{finding.severity.upper()} / {finding.category}**: "
        f"{_truncate(finding.title, 300)}\n\n"
        f"Impact: {_truncate(finding.impact, 1500)}\n\n"
        f"Evidence:\n{evidence}\n\n"
        f"Suggestion: {_truncate(finding.suggestion, 1500)}\n\n"
        f"{marker(fingerprint)}"
    )


def publish_findings(
    adapter: PublisherAdapter,
    request: ReviewRequest,
    findings: tuple[Finding, ...],
) -> PublishReport:
    if adapter.current_head_sha() != request.head_sha:
        raise StaleHeadError(
            "pull request head changed before publication"
        )

    existing = adapter.existing_review_bodies()
    comments: list[dict[str, object]] = []
    expected_markers: list[str] = []
    skipped = 0
    for finding in findings[:MAX_COMMENTS]:
        item_marker = marker(finding_fingerprint(request, finding))
        if any(item_marker in body for body in existing):
            skipped += 1
            continue
        expected_markers.append(item_marker)
        comments.append(
            {
                "path": finding.path,
                "line": finding.line,
                "side": "RIGHT",
                "body": render_body(request, finding),
            }
        )

    if not comments:
        return PublishReport(published=0, skipped=skipped)

    try:
        adapter.create_review(request.head_sha, comments)
    except Exception:
        reconciled = adapter.existing_review_bodies()
        if all(
            any(item_marker in body for body in reconciled)
            for item_marker in expected_markers
        ):
            return PublishReport(
                published=len(comments),
                skipped=skipped,
            )
        raise
    return PublishReport(published=len(comments), skipped=skipped)


def _digest(value: dict[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
