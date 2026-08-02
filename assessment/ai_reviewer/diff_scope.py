from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath, PureWindowsPath

from unidiff import PatchSet

from .contracts import Finding

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def safe_diff_path(path: str) -> str:
    value = path.replace("\\", "/")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or ".." in candidate.parts
        or value in {"", "/dev/null"}
    ):
        raise ValueError(f"unsafe diff path: {path}")
    return candidate.as_posix()


def parse_changed_scope(
    diff: str,
) -> tuple[tuple[str, ...], dict[str, frozenset[int]]]:
    files: list[str] = []
    lines: dict[str, set[int]] = {}
    patch = PatchSet(diff.splitlines(keepends=True))
    for patched_file in patch:
        raw_path = (
            patched_file.target_file
            if patched_file.target_file != "/dev/null"
            else patched_file.source_file
        )
        path = safe_diff_path(raw_path)
        files.append(path)
        target_lines = lines.setdefault(path, set())
        for hunk in patched_file:
            target_lines.update(
                line.target_line_no
                for line in hunk
                if line.is_added and line.target_line_no is not None
            )
    unique_files = tuple(dict.fromkeys(files))
    return unique_files, {
        path: frozenset(value)
        for path, value in lines.items()
    }


def filter_publishable(
    findings: Iterable[Finding],
    changed_lines: Mapping[str, frozenset[int]],
    limit: int = 8,
) -> tuple[Finding, ...]:
    if limit < 0:
        raise ValueError("limit must not be negative")
    valid = [
        finding
        for finding in findings
        if finding.path in changed_lines
        and finding.line in changed_lines[finding.path]
        and any(item.strip() for item in finding.evidence)
        and bool(finding.impact.strip())
        and bool(finding.suggestion.strip())
    ]
    valid.sort(
        key=lambda item: (
            SEVERITY_ORDER[item.severity],
            -item.confidence,
            item.path,
            item.line,
            item.title,
        )
    )
    return tuple(valid[:limit])
