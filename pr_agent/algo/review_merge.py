"""Merge the per-chunk outputs of a chunked `/review` into a single review.

`/review` normally answers from one model call over the whole diff. When the diff does not
fit, the reviewer can split it into chunks (`get_pr_multi_diffs`) and ask the same questions
about each chunk. Every chunk answers the same schema, so the answers have to be reduced to
one verdict. Three rules cover the schema:

- Fields that report what a chunk *found* - key issues, security concerns, TODO sections,
  priority files, sub-PRs, ticket bullet lists - are unioned in chunk order, dropping repeats.
- Fields that are one *judgement* about the whole PR - score, risk level, merge
  recommendation, review effort, "does the PR have tests" - take the most conservative value
  any chunk reported, so a merged review is never less alarming than its worst chunk.
- Contribution time measures *work* and is summed, because the chunks partition the change.

A key that matches none of the rules keeps the first chunk's non-empty value, so a field
added to the prompt later still survives the merge instead of disappearing from the review.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Hashable, List, Optional

from pr_agent.algo.utils import is_value_no
from pr_agent.log import get_logger

MAX_EFFORT = 5
MAX_SUB_PRS = 3
CONTRIBUTION_TIME_CASES = ("best_case", "average_case", "worst_case")
RISK_LEVELS = ("low", "medium", "high")
MERGE_RECOMMENDATIONS = ("safe_to_merge", "merge_with_caution", "changes_required")

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([mh])", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def merge_review_chunks(chunk_outputs: List[dict]) -> dict:
    """Reduce the parsed review of every chunk to a single `{'review': {...}}` dict."""
    reviews = [chunk["review"] for chunk in chunk_outputs
               if isinstance(chunk, dict) and isinstance(chunk.get("review"), dict)]
    if not reviews:
        return {}
    if len(reviews) == 1:
        return {"review": dict(reviews[0])}

    # keep the prompt's field order, which is also the order the review is rendered in
    keys = []
    for review in reviews:
        for key in review:
            if key not in keys:
                keys.append(key)

    merged = {}
    for key in keys:
        values = [review[key] for review in reviews if key in review]
        merge = _MERGE_RULES.get(_rule_name(key), _first_non_empty)
        try:
            merged[key] = merge(values)
        except Exception as e:
            get_logger().warning(f"Failed to merge review field '{key}' across chunks, "
                                 f"keeping the first chunk's value, error: {e}")
            merged[key] = _first_non_empty(values)
    return {"review": merged}


def _rule_name(key: str) -> str:
    normalized = str(key).strip().lower()
    # the effort field carries its scale in its name: 'estimated_effort_to_review_[1-5]'
    if normalized.startswith("estimated_effort_to_review"):
        return "estimated_effort_to_review"
    return normalized


def _first_non_empty(values: List[Any]) -> Any:
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return values[0]


def _normalize_text(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value if value is not None else "")).strip().lower()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip().split(",")[0].strip())
    except (TypeError, ValueError):
        return None


def _merge_effort(values: List[Any]) -> Any:
    """Effort is a 1-5 judgement, so the merged review reports the hardest chunk.

    Summing would saturate at 5 for any PR of a few modest chunks, and the field would stop
    telling PRs apart as soon as chunking is on.
    """
    numbers = [number for number in (_as_int(value) for value in values) if number is not None]
    if not numbers:
        return _first_non_empty(values)
    return max(1, min(MAX_EFFORT, max(numbers)))


def _merge_score(values: List[Any]) -> Any:
    """Lowest score wins: a clean chunk must not raise the grade of a bad one."""
    scored = [(number, value) for number, value in ((_as_int(value), value) for value in values)
              if number is not None]
    if not scored:
        return _first_non_empty(values)
    return min(scored, key=lambda pair: pair[0])[1]


def _worst_of(order: tuple) -> Callable[[List[Any]], Any]:
    """Pick the value ranked last in `order`; values outside it are ignored."""
    def merge(values: List[Any]) -> Any:
        ranked = [(order.index(choice), value) for choice, value
                  in ((_normalize_text(value).replace(" ", "_"), value) for value in values)
                  if choice in order]
        if not ranked:
            return _first_non_empty(values)
        return max(ranked, key=lambda pair: pair[0])[1]
    return merge


def _merge_findings_text(values: List[Any]) -> Any:
    """Union the chunks that reported something; 'No' only when every chunk said no."""
    reported, seen = [], set()
    for value in values:
        text = str(value if value is not None else "").strip()
        if is_value_no(text):
            continue
        fingerprint = _normalize_text(text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        reported.append(text)
    if not reported:
        return _first_non_empty(values)
    return "\n\n".join(reported)


def _merge_relevant_tests(values: List[Any]) -> Any:
    """A test added in any chunk is a test added by the PR."""
    for value in values:
        if not is_value_no(value):
            return value
    return _first_non_empty(values)


def _union_of_lists(identity: Callable[[Any], Hashable]) -> Callable[[List[Any]], list]:
    def merge(values: List[Any]) -> list:
        merged, seen = [], set()
        for value in values:
            for item in value if isinstance(value, list) else []:
                key = identity(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged
    return merge


def _key_issue_identity(issue: Any) -> Hashable:
    if not isinstance(issue, dict):
        return _normalize_text(issue)
    return (_normalize_text(issue.get("relevant_file")),
            _normalize_text(issue.get("issue_header")),
            _normalize_text(issue.get("issue_content")))


def _todo_identity(todo: Any) -> Hashable:
    if not isinstance(todo, dict):
        return _normalize_text(todo)
    return (_normalize_text(todo.get("relevant_file")),
            _normalize_text(todo.get("line_number")),
            _normalize_text(todo.get("content")))


def _sub_pr_identity(sub_pr: Any) -> Hashable:
    if not isinstance(sub_pr, dict):
        return _normalize_text(sub_pr)
    relevant_files = sub_pr.get("relevant_files")
    if isinstance(relevant_files, list) and relevant_files:
        return frozenset(_normalize_text(name) for name in relevant_files)
    return _normalize_text(sub_pr.get("title"))


def _merge_todo_sections(values: List[Any]) -> Any:
    return _union_of_lists(_todo_identity)(values) or _first_non_empty(values)


def _merge_can_be_split(values: List[Any]) -> list:
    # the prompt asks for at most 3 sub-PRs, so the merged list keeps the same bound
    return _union_of_lists(_sub_pr_identity)(values)[:MAX_SUB_PRS]


def _merge_priority_files(values: List[Any]) -> list:
    merged, seen = [], set()
    for value in values:
        for item in value if isinstance(value, list) else []:
            name = str(item).strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            merged.append(name)
    return merged


def _union_bullet_lines(first: str, second: str) -> str:
    lines, seen = [], set()
    for line in f"{first}\n{second}".splitlines():
        if not line.strip():
            continue
        key = _normalize_text(line)
        if key in seen:
            continue
        seen.add(key)
        lines.append(line.rstrip())
    return "\n".join(lines)


def _merge_ticket_compliance(values: List[Any]) -> list:
    """One entry per ticket; its bullet lists are unioned across the chunks that saw it.

    A requirement that one chunk met and another did not ends up in both lists, which
    `ticket_markdown_logic` already renders as 'Partially compliant'.
    """
    merged: dict = {}
    for value in values:
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ticket = _normalize_text(entry.get("ticket_url"))
            if ticket not in merged:
                merged[ticket] = dict(entry)
                continue
            target = merged[ticket]
            for field, field_value in entry.items():
                if isinstance(field_value, str) and isinstance(target.get(field), str):
                    target[field] = _union_bullet_lines(target[field], field_value)
                elif not target.get(field):
                    target[field] = field_value
    return list(merged.values())


def _duration_minutes(value: Any) -> Optional[float]:
    match = _DURATION_RE.fullmatch(str(value if value is not None else "").strip())
    if not match:
        return None
    return float(match.group(1)) * (60 if match.group(2).lower() == "h" else 1)


def _format_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))}m"
    hours = minutes / 60
    return f"{int(hours)}h" if float(hours).is_integer() else f"{hours:.1f}h"


def _merge_contribution_time(values: List[Any]) -> Any:
    """The chunks partition the change, so the time to write them adds up."""
    estimates = [value for value in values if isinstance(value, dict)]
    if not estimates:
        return _first_non_empty(values)
    totals = {}
    for case in CONTRIBUTION_TIME_CASES:
        minutes = [_duration_minutes(estimate.get(case)) for estimate in estimates]
        if any(value is None for value in minutes):
            get_logger().debug("Contribution time estimates cannot be added across chunks, "
                               "keeping the first chunk's estimate", artifact={"case": case})
            return _first_non_empty(values)
        totals[case] = _format_duration(sum(minutes))
    return totals


_MERGE_RULES: dict = {
    "estimated_effort_to_review": _merge_effort,
    "score": _merge_score,
    "risk_level": _worst_of(RISK_LEVELS),
    "merge_recommendation": _worst_of(MERGE_RECOMMENDATIONS),
    "security_concerns": _merge_findings_text,
    "insights_from_user_answers": _merge_findings_text,
    "relevant_tests": _merge_relevant_tests,
    "key_issues_to_review": _union_of_lists(_key_issue_identity),
    "todo_sections": _merge_todo_sections,
    "can_be_split": _merge_can_be_split,
    "review_priority_files": _merge_priority_files,
    "ticket_compliance_check": _merge_ticket_compliance,
    "contribution_time_cost_estimate": _merge_contribution_time,
}
