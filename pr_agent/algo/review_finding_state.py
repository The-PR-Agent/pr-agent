"""Persist review finding state across runs."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from pr_agent.algo.inline_comment_dedup import key_issue_fingerprint

STATE_SCHEMA_VERSION = 2
DEFAULT_MAX_RESOLVED_FINDINGS = 20
_STATE_MARKER_RE = re.compile(
    r"<!-- pr-agent-review-state:v(?P<version>\d+)\n(?P<payload>.*?)\n-->",
    re.DOTALL,
)
_STATE_MARKER_NAMESPACE = "<!-- pr-agent-review-state"
_WHITESPACE_RE = re.compile(r"\s+")
_VALID_STATES = {"ACTIVE", "UNCONFIRMED", "RESOLVED"}
_VALID_SCHEMA_VERSIONS = (1, 2)


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
    # Ids of findings this run actually reported, keyed by the *retained* finding id (the
    # previous id when a current finding fuzzy-matched one, not a fresh fingerprint of its
    # current wording). Callers use this - not a re-fingerprint of current findings - to decide
    # which stored findings are "carried" from earlier runs rather than present this run.
    current_ids: tuple[str, ...]


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

    # The fingerprint ignores whitespace so re-wrapped prose stays the same finding, but the
    # body is what the resolved section renders, so it keeps the line breaks the reviewer used.
    finding_id = key_issue_fingerprint(path, _WHITESPACE_RE.sub(" ", body).lower())
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
    if state.get("schema_version") not in _VALID_SCHEMA_VERSIONS:
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
    if version not in _VALID_SCHEMA_VERSIONS or not _is_valid_state(state):
        return ParsedReviewState(None, present=True, valid=False)
    return ParsedReviewState(state, present=True, valid=True)


def serialize_review_state(state: Mapping[str, Any]) -> str:
    """Serialize state deterministically so repeated updates are diffable."""
    if not _is_valid_state(state):
        raise ValueError("Invalid review finding state")
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # An HTML comment ends at the first "-->" or "--!>", and a finding quotes whatever the
    # diff contains. Escaping the ">" keeps the marker one comment; json.loads decodes the
    # escape, so the finding round-trips unchanged.
    payload = payload.replace("-->", "--\\u003e").replace("--!>", "--!\\u003e")
    return f"<!-- pr-agent-review-state:v{STATE_SCHEMA_VERSION}\n{payload}\n-->"


def _retained_findings(
    findings: Iterable[dict[str, Any]],
    max_resolved_findings: int,
) -> list[dict[str, Any]]:
    active = [finding for finding in findings if finding["state"] in ("ACTIVE", "UNCONFIRMED")]
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
    fully_reviewed_files: Iterable[str] | None = None,
    head_sha: str = "",
    run_id: str = "",
    timestamp: str | None = None,
    max_resolved_findings: int = DEFAULT_MAX_RESOLVED_FINDINGS,
) -> ReconciliationResult:
    """Reconcile current structured findings against the previous state.

    Resolution is deliberately conservative. The caller must only pass
    allow_resolution=True for a successful, complete full review, and the
    previous and current reviewed HEADs must both be known and different.
    An absent finding only resolves when its file was itself fully reviewed
    this run (per fully_reviewed_files); otherwise it becomes UNCONFIRMED,
    since the absence carries no evidence the underlying line was re-checked.
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
    # Local import: review_merge.py does not import review_finding_state.py today, but importing
    # inside the function avoids creating a module-load-order dependency between the two.
    from pr_agent.algo.review_merge import normalize_finding_path, same_finding_across_runs

    reviewed_paths = {normalize_finding_path(p) for p in (fully_reviewed_files or []) if p}
    previous_by_id = {finding["finding_id"]: finding for finding in previous_findings}
    current_by_id = {finding["finding_id"]: finding for finding in current}
    # Pre-claim every id an exact match will need, before any fuzzy matching runs. Otherwise a
    # fuzzy match processed first (current findings are iterated in sorted-hash order, not input
    # order) could steal a previous finding that a *different*, exact-id current finding also
    # matches - the two would then collide on the same retained id and one record would silently
    # overwrite the other.
    matched_previous_ids: set[str] = set(current_by_id) & set(previous_by_id)

    def _previous_match(current_finding: dict[str, Any]) -> dict[str, Any] | None:
        # A finding without a line range (a file-level defect) never fuzzy-matches - see
        # same_finding_across_runs - so it falls back to exact finding_id identity only.
        exact = previous_by_id.get(current_finding["finding_id"])
        if exact is not None:
            return exact
        for candidate in previous_findings:
            if candidate["finding_id"] in matched_previous_ids:
                continue
            if candidate.get("state") != "RESOLVED" and same_finding_across_runs(candidate, current_finding):
                return candidate
        return None

    reconciled: dict[str, dict[str, Any]] = {}
    resolved_ids: list[str] = []
    reopened_ids: list[str] = []
    current_ids: list[str] = []
    changed = previous_state is None and bool(current)

    for _, current_finding in current_by_id.items():
        previous = _previous_match(current_finding)
        if previous is None:
            record = dict(current_finding)
            record.update(first_seen=now, last_seen=now)
            changed = True
        else:
            matched_previous_ids.add(previous["finding_id"])
            record = copy.deepcopy(previous)
            old_state = record.get("state")
            record.update(current_finding)
            record["finding_id"] = previous["finding_id"]
            record["state"] = "ACTIVE"
            record["last_seen"] = now
            if old_state == "RESOLVED":
                record["reopened_at"] = now
                record["reopened_count"] = int(record.get("reopened_count", 0)) + 1
                reopened_ids.append(previous["finding_id"])
            elif old_state == "UNCONFIRMED":
                record.pop("unconfirmed_at", None)
            if record != previous:
                changed = True
        if head_sha:
            record["last_seen_head_sha"] = head_sha
        reconciled[record["finding_id"]] = record
        current_ids.append(record["finding_id"])

    for finding_id, previous in previous_by_id.items():
        if finding_id in matched_previous_ids:
            continue
        record = copy.deepcopy(previous)
        state_now = record.get("state")
        file_reviewed = normalize_finding_path(record.get("path")) in reviewed_paths
        if state_now in ("ACTIVE", "UNCONFIRMED") and resolution_allowed and file_reviewed:
            record["state"] = "RESOLVED"
            record["resolved_at"] = now
            record.pop("unconfirmed_at", None)
            if head_sha:
                record["resolved_head_sha"] = head_sha
            if run_id:
                record["resolution_run_id"] = run_id
            resolved_ids.append(finding_id)
            changed = True
        elif state_now == "ACTIVE":
            record["state"] = "UNCONFIRMED"
            record["unconfirmed_at"] = now
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
        current_ids=tuple(sorted(current_ids)),
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


def render_carried_section(
    state: Mapping[str, Any],
    current_ids: set[str],
    fully_reviewed_files: Iterable[str],
) -> str:
    """Render every ACTIVE/UNCONFIRMED finding this run did not itself report.

    A finding absent from `current_ids` was not (re)emitted this run - either because its file
    was not touched, or because it was reviewed and simply not flagged again. Both cases stay
    visible here so a reader never loses track of a still-open finding just because one run
    didn't happen to restate it.
    """
    # Local import: review_merge.py does not import review_finding_state.py today, but importing
    # inside the function avoids creating a module-load-order dependency between the two.
    from pr_agent.algo.review_merge import normalize_finding_path

    reviewed = {normalize_finding_path(p) for p in fully_reviewed_files or []}
    carried = [
        finding
        for finding in state.get("findings", [])
        if finding.get("state") in ("ACTIVE", "UNCONFIRMED") and finding.get("finding_id") not in current_ids
    ]
    if not carried:
        return ""
    carried.sort(key=lambda finding: (str(finding.get("path") or ""), finding.get("line_start") or 0))
    lines = ["### Carried from earlier runs", ""]
    for finding in carried:
        path = finding.get("path", "")
        loc = f"{path}:{finding['line_start']}" if finding.get("line_start") else path
        note = (
            "re-reviewed, not re-emitted"
            if normalize_finding_path(path) in reviewed
            else "not re-reviewed this run"
        )
        tag = " · unconfirmed" if finding.get("state") == "UNCONFIRMED" else ""
        header = finding.get("body", "").split("\n", 1)[0].strip("* ")
        first_seen = str(finding.get("first_seen") or "")[:10]
        lines.append(f"- **{header}** — `{loc}` · first seen {first_seen} · {note}{tag}")
    return "\n".join(lines)


_CARRIED_HEADING = "### Carried from earlier runs"
_CARRIED_CONTINUATION_HEADING = "### Carried from earlier runs (continued)"
_CARRIED_CONTINUATION_INTRO = "Continued from the primary review comment."


def _parse_carried_entries(carried_section: str) -> tuple[str, list[str]]:
    """Split a carried section into its heading block and whole `- **` entry lines."""
    if not carried_section:
        return "", []
    header_lines: list[str] = []
    entries: list[str] = []
    for line in carried_section.split("\n"):
        if line.startswith("- **"):
            entries.append(line)
        elif not entries:
            header_lines.append(line)
    header = "\n".join(header_lines).rstrip()
    return header, entries


def _rebuild_carried_section(header: str, entries: list[str]) -> str:
    if not entries:
        return ""
    heading = header or _CARRIED_HEADING
    return "\n".join([heading, "", *entries])


def _build_carried_continuation(entries: list[str]) -> str:
    if not entries:
        return ""
    return "\n".join([
        _CARRIED_CONTINUATION_HEADING,
        "",
        _CARRIED_CONTINUATION_INTRO,
        "",
        *entries,
    ])


def append_review_state_paginated(
    review_body: str,
    state: Mapping[str, Any],
    max_chars: int | None = None,
    *,
    carried_section: str = "",
) -> tuple[str, str]:
    """Append carried/resolved/marker within an optional limit; overflow carried becomes a second comment.

    Section order (highest priority for the reader first): the human review body, the carried
    section, the resolved section, then the hidden marker. When `max_chars` does not fit
    everything, sections give way in the opposite order: RESOLVED findings are dropped from the
    marker (and the resolved section, which mirrors it) first, then whole carried entries that
    do not fit move to a continuation comment, and only as a last resort is the human body
    itself truncated. `ValueError` is raised only when the marker, stripped of RESOLVED
    findings, still does not fit.

    Returns `(primary_comment, continuation_comment)`. The state marker appears only in the
    primary. Continuation is empty when there is no carried overflow.
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
    carried = carried_section or ""
    resolved_section = _render_resolved_section(state)
    marker = serialize_review_state(state)
    continuation = ""
    carried_header, carried_entries = _parse_carried_entries(carried)

    def _compose(b: str, c: str, r: str) -> str:
        return "\n\n".join(section for section in (b, c, r) if section)

    def _total_length(human: str, mark: str) -> int:
        return len(mark) + 1 if not human else len(human) + len(mark) + 3

    def _finalize(b: str, c: str, r: str, mark: str) -> str:
        human_body = _compose(b, c, r)
        sections = [section for section in (human_body, mark) if section]
        return "\n\n".join(sections).rstrip() + "\n"

    if max_chars is not None:
        if not isinstance(max_chars, int):
            raise ValueError(
                "Comment limit is too small for the persistent "
                "review state marker"
            )
        if _total_length(_compose(body, carried, resolved_section), marker) > max_chars:
            # Step 1: drop RESOLVED findings from the marker; the resolved section mirrors the
            # same state, so rendering it from the trimmed copy drops it too (it only ever shows
            # RESOLVED findings).
            trimmed_state = dict(state)
            trimmed_state["findings"] = _retained_findings(state.get("findings", []), 0)
            marker = serialize_review_state(trimmed_state)
            resolved_section = _render_resolved_section(trimmed_state)
            if max_chars < len(marker) + 1:
                raise ValueError(
                    "Comment limit is too small for the persistent "
                    "review state marker"
                )
            if _total_length(_compose(body, carried, resolved_section), marker) > max_chars:
                # Step 2: keep whole carried entries that fit; overflow goes to a continuation.
                # Step 3: truncate the human body only when even zero carried entries fit.
                budget = max(0, max_chars - len(marker) - 3)
                if len(body) > budget:
                    carried = ""
                    if budget <= 0:
                        body = ""
                    elif budget < 3:
                        body = body[:budget]
                    else:
                        body = body[: budget - 3] + "..."
                    continuation = _build_carried_continuation(carried_entries)
                else:
                    fitted: list[str] = []
                    overflow = list(carried_entries)
                    for index, entry in enumerate(carried_entries):
                        trial = _rebuild_carried_section(carried_header, fitted + [entry])
                        if len(_compose(body, trial, resolved_section)) <= budget:
                            fitted.append(entry)
                            overflow = carried_entries[index + 1 :]
                        else:
                            overflow = carried_entries[index:]
                            break
                    carried = _rebuild_carried_section(carried_header, fitted)
                    continuation = _build_carried_continuation(overflow)
    return _finalize(body, carried, resolved_section, marker), continuation


def append_review_state(
    review_body: str,
    state: Mapping[str, Any],
    max_chars: int | None = None,
    *,
    carried_section: str = "",
) -> str:
    """Append the carried section, resolved section and hidden marker within an optional limit.

    Wrapper around `append_review_state_paginated` that returns only the primary comment. Callers
    that need overflow pagination should use the paginated form instead.
    """
    primary, _continuation = append_review_state_paginated(
        review_body,
        state,
        max_chars,
        carried_section=carried_section,
    )
    return primary
