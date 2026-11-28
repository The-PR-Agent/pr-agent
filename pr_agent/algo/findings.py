"""Shared internal representation for review findings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pr_agent.algo.output_models import finding_severity
from pr_agent.algo.utils import SEVERITY_BADGES, _as_line, finding_severity_label, get_require_finding_severity

_SUGGESTION_FENCE_RE = re.compile(r"```[ \t]*suggestion\b", re.IGNORECASE)
_BADGES = "|".join(SEVERITY_BADGES.values())
# The label text is translated, so match the badge and any short label after it.
_SEVERITY_BEFORE_HEADER_RE = re.compile(rf"\*\*(?:{_BADGES}) [^*\n:]{{1,40}}: ")
_SEVERITY_ONLY_HEADER_RE = re.compile(rf"\*\*(?:{_BADGES}) [^*\n:]{{1,40}}\*\*\n\n")


def strip_severity_label(body: str) -> str:
    """Return a published finding body without the severity label that display_body added."""
    if match := _SEVERITY_BEFORE_HEADER_RE.match(body):
        return "**" + body[match.end():]
    if match := _SEVERITY_ONLY_HEADER_RE.match(body):
        return body[match.end():]
    return body


@dataclass(frozen=True)
class Finding:
    path: str
    body: str
    line_start: int | None = None
    line_end: int | None = None
    header: str | None = None
    label: str | None = None
    existing_code: str | None = None
    improved_code: str | None = None
    score: int | float | str | None = None
    confidence: int | None = None
    severity: str | None = None
    reply: str | None = None

    @property
    def rendered_body(self) -> str:
        if self.header:
            return f"**{self.header}**\n\n{self.body}"
        return self.body

    @property
    def display_body(self) -> str:
        # rendered_body identifies the finding across runs, so the severity label is only added here.
        label = finding_severity_label(self.severity) if get_require_finding_severity() else ""
        if not label:
            return self.rendered_body
        header = f"{label}: {self.header}" if self.header else label
        return f"**{header}**\n\n{self.body}"

    @classmethod
    def from_review_issue(cls, issue: object) -> Finding | None:
        if not isinstance(issue, Mapping):
            return None
        path = str(issue.get("relevant_file") or issue.get("path") or "").strip()
        body = _SUGGESTION_FENCE_RE.sub(
            "```text", str(issue.get("issue_content") or issue.get("body") or "").strip()
        )
        header = str(issue.get("issue_header") or "").strip()
        if header.lower() == "possible bug":
            header = "Possible Issue"
        if not path or not body:
            return None

        start = _as_line(issue.get("start_line"))
        end = (_as_line(issue.get("end_line")) or start) if start is not None else None
        confidence = issue.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int):
            confidence = None
        existing_code = issue.get("existing_code")
        improved_code = issue.get("improved_code")
        return cls(
            path=path,
            body=body,
            line_start=start,
            line_end=end,
            header=header or None,
            existing_code=existing_code if isinstance(existing_code, str) else None,
            improved_code=improved_code if isinstance(improved_code, str) else None,
            confidence=confidence,
            severity=finding_severity(issue),
        )


    def to_inline_comment(
        self,
        body: str,
        *,
        fallback_to_pr_comment: bool | None = None,
        original_suggestion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        comment = {
            "body": body,
            "relevant_file": self.path,
            "relevant_lines_start": self.line_start,
            "relevant_lines_end": self.line_end,
        }
        if fallback_to_pr_comment is not None:
            comment["fallback_to_pr_comment"] = fallback_to_pr_comment
        if original_suggestion is not None:
            comment["original_suggestion"] = original_suggestion
        return comment
