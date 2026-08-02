from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .contracts import Finding, ReviewRequest

MAX_TIMEOUT_SECONDS = 90
SUPPORTED_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".ts",
    ".tsx",
}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "build",
    "dist",
    "generated",
    "node_modules",
    "vendor",
}
EXCLUDED_SUFFIXES = {".lock", ".map", ".min.js", ".min.css"}

Runner = Callable[..., subprocess.CompletedProcess[str]]


def scan(
    request: ReviewRequest,
    repo_root: Path,
    timeout_seconds: int = MAX_TIMEOUT_SECONDS,
    executable: str = "semgrep",
    runner: Runner = subprocess.run,
) -> tuple[list[Finding], list[str]]:
    root = repo_root.resolve(strict=True)
    timeout = min(max(1, int(timeout_seconds)), MAX_TIMEOUT_SECONDS)
    files, warnings = _eligible_changed_files(request, root)
    syntax_findings = _python_syntax_findings(request, root, files)
    if not files:
        return syntax_findings, warnings

    config_root = Path(__file__).parents[1] / "rules" / "semgrep"
    command = [
        executable,
        "scan",
        "--config",
        str(config_root),
        "--json",
        "--metrics=off",
        "--disable-version-check",
        "--",
        *files,
    ]
    try:
        completed = runner(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        warnings.append("semgrep executable is unavailable")
        return syntax_findings, warnings
    except subprocess.TimeoutExpired:
        warnings.append(f"semgrep exceeded the {timeout}-second timeout")
        return syntax_findings, warnings

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        warnings.append("semgrep returned invalid JSON")
        return syntax_findings, warnings
    if completed.returncode not in {0, 1}:
        warnings.append(f"semgrep exited with status {completed.returncode}")
    warnings.extend(_semgrep_errors(payload))
    semgrep_findings = _parse_results(payload, request, root)
    return _deduplicate([*syntax_findings, *semgrep_findings]), warnings


def _eligible_changed_files(
    request: ReviewRequest,
    root: Path,
) -> tuple[list[str], list[str]]:
    files: list[str] = []
    warnings: list[str] = []
    for value in request.changed_files:
        normalized = value.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            pure.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or ".." in pure.parts
        ):
            warnings.append(f"rejected unsafe changed path: {value}")
            continue
        lowered_parts = {part.lower() for part in pure.parts}
        lowered_name = pure.name.lower()
        if lowered_parts & EXCLUDED_PARTS:
            continue
        if any(lowered_name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        if pure.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            resolved = root.joinpath(*pure.parts).resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            warnings.append(f"changed file is unavailable or unsafe: {value}")
            continue
        if not resolved.is_file() or resolved.is_symlink():
            continue
        try:
            if b"\x00" in resolved.read_bytes()[:4096]:
                continue
        except OSError:
            warnings.append(f"changed file could not be read: {value}")
            continue
        files.append(resolved.relative_to(root).as_posix())
    return files, warnings


def _parse_results(
    payload: Mapping[str, Any],
    request: ReviewRequest,
    root: Path,
) -> list[Finding]:
    findings: list[Finding] = []
    results = payload.get("results", [])
    if not isinstance(results, list):
        return findings
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            path = _relative_result_path(str(item["path"]), root)
            line = int(item["start"]["line"])
            check_id = str(item["check_id"])
            extra = item.get("extra") or {}
            metadata = extra.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            category = str(metadata.get("category") or "static")
            severity = _severity(str(extra.get("severity") or "WARNING"))
            message = str(extra.get("message") or check_id).strip()
            title = str(metadata.get("title") or message).strip()
            impact = str(
                metadata.get("impact")
                or "The matched code can produce an unexpected runtime result."
            ).strip()
            suggestion = str(
                metadata.get("suggestion")
                or "Review the matched operation and apply a bounded fix."
            ).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if path not in request.changed_files:
            continue
        if line not in request.changed_lines.get(path, frozenset()):
            continue
        try:
            finding = Finding(
                path=path,
                line=line,
                category=category,
                severity=severity,
                confidence=0.95,
                title=title,
                evidence=(f"{check_id}: {message}",),
                impact=impact,
                suggestion=suggestion,
                source="semgrep",
            )
        except ValueError:
            continue
        findings.append(finding)
    return findings


def _python_syntax_findings(
    request: ReviewRequest,
    root: Path,
    files: Sequence[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for relative in files:
        if not relative.endswith(".py"):
            continue
        try:
            source = (root / relative).read_text(
                encoding="utf-8",
                errors="replace",
            )
            ast.parse(source, filename=relative)
        except SyntaxError as error:
            line = int(error.lineno or 1)
            if line not in request.changed_lines.get(relative, frozenset()):
                continue
            findings.append(
                Finding(
                    path=relative,
                    line=line,
                    category="static",
                    severity="high",
                    confidence=1.0,
                    title="Python syntax error on a changed line",
                    evidence=(f"Python parser: {error.msg}",),
                    impact=(
                        "The changed module cannot be imported or executed."
                    ),
                    suggestion="Correct the syntax before merging the change.",
                    source="syntax",
                )
            )
    return findings


def _relative_result_path(value: str, root: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve(strict=False)
        return resolved.relative_to(root).as_posix()
    normalized = Path(value.replace("\\", "/"))
    if ".." in normalized.parts:
        raise ValueError("semgrep result path escapes repo_root")
    return normalized.as_posix().removeprefix("./")


def _semgrep_errors(payload: Mapping[str, Any]) -> list[str]:
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        return []
    summaries: list[str] = []
    for item in errors[:10]:
        if isinstance(item, dict):
            error_type = str(item.get("type") or "scan error")
            summaries.append(f"semgrep warning: {error_type}")
    return summaries


def _severity(value: str) -> str:
    return {
        "ERROR": "high",
        "WARNING": "medium",
        "INFO": "low",
    }.get(value.upper(), "medium")


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    unique: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for finding in findings:
        root_cause = finding.evidence[0].split(":", 1)[0]
        key = (finding.path, finding.line, root_cause)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique
