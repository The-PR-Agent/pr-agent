from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any

CATEGORIES = frozenset(
    {
        "static",
        "business_logic",
        "logic",
        "memory",
        "security",
        "architecture",
    }
)
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
SOURCES = frozenset({"agent", "semgrep", "syntax"})
STATUSES = frozenset({"success", "failed", "timeout"})


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _reject_unexpected_fields(
    contract: type[Any],
    data: Mapping[str, Any],
) -> None:
    expected = {field.name for field in fields(contract)}
    unexpected = sorted(set(data) - expected)
    if unexpected:
        message = f"unexpected fields for {contract.__name__}: {unexpected}"
        raise ValueError(message)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen = {key: _freeze_json(item) for key, item in value.items()}
        if not all(isinstance(key, str) for key in frozen):
            raise TypeError("trace_summary object keys must be strings")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("trace_summary numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("trace_summary must contain only JSON-compatible values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    title: str
    body: str
    diff: str
    changed_lines: Mapping[str, frozenset[int]]
    changed_files: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repository",
            _required_text(self.repository, "repository"),
        )
        object.__setattr__(
            self,
            "base_sha",
            _required_text(self.base_sha, "base_sha"),
        )
        object.__setattr__(
            self,
            "head_sha",
            _required_text(self.head_sha, "head_sha"),
        )
        if (
            not isinstance(self.pr_number, int)
            or isinstance(self.pr_number, bool)
            or self.pr_number <= 0
        ):
            raise ValueError("pr_number must be a positive integer")

        copied_lines: dict[str, frozenset[int]] = {}
        for path, lines in self.changed_lines.items():
            normalized_path = _required_text(path, "changed_lines path")
            normalized_lines: set[int] = set()
            for line in lines:
                if (
                    not isinstance(line, int)
                    or isinstance(line, bool)
                    or line <= 0
                ):
                    raise ValueError(
                        "changed_lines values must be positive integers"
                    )
                normalized_lines.add(line)
            copied_lines[normalized_path] = frozenset(normalized_lines)
        copied_files = tuple(
            _required_text(path, "changed_files path")
            for path in self.changed_files
        )
        object.__setattr__(
            self,
            "changed_lines",
            MappingProxyType(copied_lines),
        )
        object.__setattr__(self, "changed_files", copied_files)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewRequest:
        _reject_unexpected_fields(cls, data)
        return cls(
            repository=data["repository"],
            pr_number=data["pr_number"],
            base_sha=data["base_sha"],
            head_sha=data["head_sha"],
            title=data["title"],
            body=data["body"],
            diff=data["diff"],
            changed_lines=data["changed_lines"],
            changed_files=data["changed_files"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "title": self.title,
            "body": self.body,
            "diff": self.diff,
            "changed_lines": {
                path: sorted(lines)
                for path, lines in self.changed_lines.items()
            },
            "changed_files": list(self.changed_files),
        }


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    category: str
    severity: str
    confidence: float
    title: str
    evidence: tuple[str, ...]
    impact: str
    suggestion: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _required_text(self.path, "path"))
        object.__setattr__(
            self,
            "title",
            _required_text(self.title, "title"),
        )
        if (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line <= 0
        ):
            raise ValueError("line must be a positive integer")
        if self.category not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(SEVERITIES)}")
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {sorted(SOURCES)}")

        if isinstance(self.confidence, bool):
            raise ValueError("confidence must be numeric, not boolean")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and between 0 and 1")
        if isinstance(self.evidence, (str, bytes)):
            raise TypeError("evidence must be a sequence of strings")
        normalized_evidence: list[str] = []
        for item in self.evidence:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("evidence entries must be non-empty strings")
            normalized_evidence.append(item.strip())
        if not normalized_evidence:
            raise ValueError("evidence must not be empty")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence", tuple(normalized_evidence))
        object.__setattr__(
            self,
            "impact",
            _required_text(self.impact, "impact"),
        )
        object.__setattr__(
            self,
            "suggestion",
            _required_text(self.suggestion, "suggestion"),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Finding:
        _reject_unexpected_fields(cls, data)
        return cls(
            path=data["path"],
            line=data["line"],
            category=data["category"],
            severity=data["severity"],
            confidence=data["confidence"],
            title=data["title"],
            evidence=data["evidence"],
            impact=data["impact"],
            suggestion=data["suggestion"],
            source=data["source"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "title": self.title,
            "evidence": list(self.evidence),
            "impact": self.impact,
            "suggestion": self.suggestion,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ReviewResult:
    run_id: str
    model: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    findings: tuple[Finding, ...]
    errors: tuple[str, ...]
    trace_summary: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            _required_text(self.run_id, "run_id"),
        )
        object.__setattr__(
            self,
            "model",
            _required_text(self.model, "model"),
        )
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")

        duration_seconds = float(self.duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError(
                "duration_seconds must be finite and non-negative"
            )
        if not all(isinstance(item, Finding) for item in self.findings):
            raise TypeError("findings entries must be Finding instances")
        if not all(isinstance(error, str) for error in self.errors):
            raise TypeError("errors entries must be strings")
        frozen_trace = tuple(_freeze_json(item) for item in self.trace_summary)
        if not all(isinstance(item, Mapping) for item in frozen_trace):
            raise TypeError("trace_summary entries must be objects")
        object.__setattr__(
            self,
            "duration_seconds",
            duration_seconds,
        )
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "trace_summary", frozen_trace)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewResult:
        _reject_unexpected_fields(cls, data)
        return cls(
            run_id=data["run_id"],
            model=data["model"],
            status=data["status"],
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            duration_seconds=data["duration_seconds"],
            findings=tuple(
                Finding.from_dict(item) for item in data["findings"]
            ),
            errors=tuple(data["errors"]),
            trace_summary=tuple(data["trace_summary"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "findings": [finding.to_dict() for finding in self.findings],
            "errors": list(self.errors),
            "trace_summary": [_thaw_json(item) for item in self.trace_summary],
        }
