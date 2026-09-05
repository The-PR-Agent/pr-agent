"""Persist review finding state across runs."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from pr_agent.algo.inline_comment_dedup import key_issue_fingerprint

STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_RESOLVED_FINDINGS = 20
_STATE_MARKER_RE = re.compile(
    r"<!-- pr-agent-review-state:v(?P<version>\d+)\n(?P<payload>.*?)\n-->",
    re.DOTALL,
)
_STATE_MARKER_NAMESPACE = "<!-- pr-agent-review-state"
_WHITESPACE_RE = re.compile(r"\s+")
_VALID_STATES = {"ACTIVE", "RESOLVED"}


@dataclass(frozen=True)
class ParsedReviewState:
    state: dict[str, Any] | None
    present: bool
    valid: bool


@dataclass(frozen=True)
class ReconciliationResult:
    state: dict[str, Any]
    changed: bool
    resolved_ids: tuple[str, ...]
    reopened_ids: tuple[str, ...]


def _timestamp(value: str | None) -> str:
    if value:
        return value
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_line(value: Any) -> int | None:
    try:
        line = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


def normalize_finding(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the stable, display-oriented fields needed for reconciliation."""
    if not isinstance(finding, Mapping):
        return None

    path = str(finding.get("path") or finding.get("relevant_file") or "").strip()
    path = path.strip().strip(chr(96)).lstrip("/")
    body = str(
        finding.get("body")
        or finding.get("issue_content")
        or finding.get("description")
        or ""
    ).strip()
    if not path or not body:
        return None

    body = _WHITESPACE_RE.sub(" ", body)
    finding_id = key_issue_fingerprint(path, body.lower())
    start = _as_line(
        finding.get("line_start")
        or finding.get("relevant_lines_start")
        or finding.get("start_line")
    )
    end = _as_line(
        finding.get("line_end")
        or finding.get("relevant_lines_end")
        or finding.get("end_line")
    )
    if start is not None and end is None:
        end = start
    if start is not None and end is not None and end < start:
        end = start

    normalized = {
        "finding_id": finding_id,
        "state": "ACTIVE",
        "body": body,
        "path": path,
    }
    if start is not None:
        normalized["line_start"] = start
    if end is not None:
        normalized["line_end"] = end
    return normalized


def normalize_findings(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and de-duplicate current structured findings deterministically."""
    by_id: dict[str, dict[str, Any]] = {}
    for finding in findings or []:
        normalized = normalize_finding(finding)
        if normalized is not None:
            by_id.setdefault(normalized["finding_id"], normalized)
    return [by_id[finding_id] for finding_id in sorted(by_id)]


def _is_valid_state(state: Any) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        return False
    if not isinstance(state.get("findings"), list) or not isinstance(state.get("last_run"), dict):
        return False
    finding_ids = set()
    for finding in state["findings"]:
        if not isinstance(finding, dict):
            return False
        if finding.get("state") not in _VALID_STATES:
            return False
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id or finding_id in finding_ids:
            return False
        finding_ids.add(finding_id)
        reopened_count = finding.get("reopened_count", 0)
        if type(reopened_count) is not int or reopened_count < 0:
            return False
        if not finding.get("path") or not finding.get("body"):
            return False
    return True


def parse_review_state(comment_body: str) -> ParsedReviewState:
    """Parse the versioned state marker, treating malformed state as unsafe."""
    body = comment_body or ""
    namespace_count = body.count(_STATE_MARKER_NAMESPACE)
    if namespace_count == 0:
        return ParsedReviewState(None, present=False, valid=True)
    if namespace_count != 1:
        return ParsedReviewState(None, present=True, valid=False)
    matches = list(_STATE_MARKER_RE.finditer(body))
    if len(matches) != 1:
        return ParsedReviewState(None, present=True, valid=False)
    match = matches[0]
    try:
        version = int(match.group("version"))
        state = json.loads(match.group("payload"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ParsedReviewState(None, present=True, valid=False)
    if version != STATE_SCHEMA_VERSION or not _is_valid_state(state):
        return ParsedReviewState(None, present=True, valid=False)
    return ParsedReviewState(state, present=True, valid=True)


def serialize_review_state(state: Mapping[str, Any]) -> str:
    """Serialize state deterministically so repeated updates are diffable."""
    if not _is_valid_state(state):
        raise ValueError("Invalid review finding state")
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"<!-- pr-agent-review-state:v{STATE_SCHEMA_VERSION}\n{payload}\n-->"


def _retained_findings(
    findings: Iterable[dict[str, Any]],
    max_resolved_findings: int,
) -> list[dict[str, Any]]:
    active = [finding for finding in findings if finding["state"] == "ACTIVE"]
    resolved = [finding for finding in findings if finding["state"] == "RESOLVED"]
    resolved.sort(
        key=lambda finding: (
            str(finding.get("resolved_at") or ""),
            str(finding["finding_id"]),
        ),
        reverse=True,
    )
    return sorted(
        active + resolved[:max(0, max_resolved_findings)],
        key=lambda finding: finding["finding_id"],
    )


def reconcile_review_findings(
    previous_state: Mapping[str, Any] | None,
    current_findings: Iterable[Mapping[str, Any]],
    *,
    allow_resolution: bool,
    excluded_files: Iterable[str] | None = None,
    head_sha: str = "",
    run_id: str = "",
    timestamp: str | None = None,
    max_resolved_findings: int = DEFAULT_MAX_RESOLVED_FINDINGS,
) -> ReconciliationResult:
    """Reconcile current structured findings against the previous state.

    Resolution is deliberately conservative. The caller must only pass
    allow_resolution=True for a successful, complete full review, and the
    previous and current reviewed HEADs must both be known and different.
    """
    now = _timestamp(timestamp)
    current = normalize_findings(current_findings)
    previous_findings = list((previous_state or {}).get("findings", []))
    previous_last_run = (previous_state or {}).get("last_run", {})
    previous_head_sha = (
        previous_last_run.get("head_sha", "")
        if isinstance(previous_last_run, Mapping)
        else ""
    )
    resolution_allowed = (
        allow_resolution
        and isinstance(previous_head_sha, str)
        and bool(previous_head_sha.strip())
        and isinstance(head_sha, str)
        and bool(head_sha.strip())
        and previous_head_sha != head_sha
    )
    previous_by_id = {finding["finding_id"]: finding for finding in previous_findings}
    current_by_id = {finding["finding_id"]: finding for finding in current}
    reconciled: dict[str, dict[str, Any]] = {}
    resolved_ids: list[str] = []
    reopened_ids: list[str] = []
    changed = previous_state is None and bool(current)

    for finding_id, current_finding in current_by_id.items():
        previous = previous_by_id.get(finding_id)
        if previous is None:
            record = dict(current_finding)
            record.update(first_seen=now, last_seen=now)
            changed = True
        else:
            record = copy.deepcopy(previous)
            old_state = record.get("state")
            record.update(current_finding)
            record["state"] = "ACTIVE"
            record["last_seen"] = now
            if old_state == "RESOLVED":
                record["reopened_at"] = now
                record["reopened_count"] = int(record.get("reopened_count", 0)) + 1
                reopened_ids.append(finding_id)
            if record != previous:
                changed = True
        if head_sha:
            record["last_seen_head_sha"] = head_sha
        reconciled[finding_id] = record

    for finding_id, previous in previous_by_id.items():
        if finding_id in current_by_id:
            continue
        record = copy.deepcopy(previous)
        if record.get("state") == "ACTIVE" and resolution_allowed:
            record["state"] = "RESOLVED"
            record["resolved_at"] = now
            if head_sha:
                record["resolved_head_sha"] = head_sha
            if run_id:
                record["resolution_run_id"] = run_id
            resolved_ids.append(finding_id)
            changed = True
        reconciled[finding_id] = record

    excluded = sorted({str(path) for path in (excluded_files or []) if path})
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "findings": _retained_findings(reconciled.values(), max_resolved_findings),
        "last_run": {
            "complete": bool(allow_resolution),
            "excluded_files": excluded,
            "head_sha": head_sha,
            "kind": "full" if allow_resolution else "partial",
            "run_id": run_id,
        },
    }
    if previous_state is not None and state["findings"] != previous_state.get("findings", []):
        changed = True
    return ReconciliationResult(
        state=state,
        changed=changed,
        resolved_ids=tuple(sorted(resolved_ids)),
        reopened_ids=tuple(sorted(reopened_ids)),
    )


def _render_resolved_section(state: Mapping[str, Any]) -> str:
    resolved = [finding for finding in state.get("findings", []) if finding.get("state") == "RESOLVED"]
    if not resolved:
        return ""
    resolved.sort(
        key=lambda finding: (
            str(finding.get("resolved_at") or ""),
            str(finding.get("finding_id") or ""),
        ),
        reverse=True,
    )
    lines = [
        "<details>",
        "<summary>✅ Resolved findings</summary>",
        "",
    ]
    for finding in resolved:
        location = finding["path"]
        if finding.get("line_start"):
            location += f":{finding['line_start']}"
            if finding.get("line_end") and finding["line_end"] != finding["line_start"]:
                location += f"-{finding['line_end']}"
        lines.extend([f"### {location}", "", finding["body"], ""])
    lines.extend(["</details>", ""])
    return "\n".join(lines).rstrip()


def append_review_state(
    review_body: str,
    state: Mapping[str, Any],
    max_chars: int | None = None,
) -> str:
    """Append the resolved section and hidden marker within an optional limit.

    The optional limit is reserved for the complete hidden marker.
    """
    raw_body = review_body or ""
    namespace_count = raw_body.count(_STATE_MARKER_NAMESPACE)
    if namespace_count == 1:
        body = _STATE_MARKER_RE.sub("", raw_body).rstrip()
        if _STATE_MARKER_NAMESPACE in body:
            body = body.split(_STATE_MARKER_NAMESPACE, 1)[0].rstrip()
    elif namespace_count > 1:
        body = raw_body.split(_STATE_MARKER_NAMESPACE, 1)[0].rstrip()
    else:
        body = raw_body.rstrip()
    human_body = "\n\n".join(
        section
        for section in (body, _render_resolved_section(state))
        if section
    )
    marker = serialize_review_state(state)
    if max_chars is not None:
        if not isinstance(max_chars, int) or max_chars < len(marker) + 1:
            raise ValueError(
                "Comment limit is too small for the persistent "
                "review state marker"
            )
        human_budget = max_chars - len(marker) - 3
        if len(human_body) > human_budget:
            if human_budget <= 0:
                human_body = ""
            elif human_budget < 3:
                human_body = human_body[:human_budget]
            else:
                human_body = human_body[: human_budget - 3] + "..."
    sections = [section for section in (human_body, marker) if section]
    return "\n\n".join(sections).rstrip() + "\n"
