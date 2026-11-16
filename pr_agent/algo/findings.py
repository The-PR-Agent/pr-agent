"""Shared internal representation for review findings and code suggestions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pr_agent.algo.utils import _as_line

_SUGGESTION_FENCE_RE = re.compile(r"```[ \t]*suggestion\b", re.IGNORECASE)


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
    reply: str | None = None

    @property
    def rendered_body(self) -> str:
        if self.header:
            return f"**{self.header}**\n\n{self.body}"
        return self.body

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
        )

    @classmethod
    def from_code_suggestion(cls, suggestion: object) -> Finding | None:
        if not isinstance(suggestion, Mapping):
            return None
        path = str(suggestion.get("relevant_file") or "").strip()
        body = str(suggestion.get("suggestion_content") or "").rstrip()
        start = _as_line(suggestion.get("relevant_lines_start"))
        end = _as_line(suggestion.get("relevant_lines_end"))
        score = suggestion.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float, str)):
            score = None
        confidence = suggestion.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int):
            confidence = None
        existing_code = suggestion.get("existing_code")
        improved_code = suggestion.get("improved_code")
        return cls(
            path=path,
            body=body,
            line_start=start,
            line_end=end,
            label=str(suggestion.get("label") or "").strip(),
            existing_code=existing_code if isinstance(existing_code, str) else None,
            improved_code=improved_code if isinstance(improved_code, str) else None,
            score=score,
            confidence=confidence,
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
