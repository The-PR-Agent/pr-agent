"""Merge the per-chunk outputs of a chunked `/review` into a single review.

`/review` normally answers from one model call over the whole diff. When the diff does not
fit, the reviewer can split it into chunks (`get_pr_multi_diffs`) and ask the same questions
about each chunk. Every chunk answers the same schema, so the answers have to be reduced to
one verdict. Three rules cover the schema:

- Fields that report what a chunk *found* - key issues, security concerns, TODO sections,
  priority files, sub-PRs, ticket bullet lists - are unioned in chunk order, dropping repeats.
  Ticket entries are keyed by the ticket they refer to, resolved against `related_tickets`,
  so a chunk that writes a full URL, `#12`, or a shortened URL still lands on one entry.
  The merged key issues are then cut to `max_findings`, the per-review limit that
  `num_max_findings` advertises, because every chunk may report its own full allowance.
- Fields that are one *judgement* about the whole PR - score, risk level, merge
  recommendation, review effort, "does the PR have tests" - take the most conservative value
  any chunk reported, so a merged review is never less alarming than its worst chunk.
- Contribution time measures *work* and is summed, because the chunks partition the change.

A key that matches none of the rules keeps the first chunk's non-empty value, so a field
added to the prompt later still survives the merge instead of disappearing from the review.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable
from typing import Any

from pr_agent.algo.comment_identity import as_review_text
from pr_agent.algo.output_models import parse_failure_modes
from pr_agent.algo.utils import is_value_no
from pr_agent.log import get_logger

MAX_EFFORT = 5
MAX_SUB_PRS = 3
CONTRIBUTION_TIME_CASES = ("best_case", "average_case", "worst_case")
RISK_LEVELS = ("low", "medium", "high")
MERGE_RECOMMENDATIONS = ("no_concerns_found", "needs_review", "changes_required")

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([mh])", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_NUMBER_RE = re.compile(r"(?<!\d)(\d+)\D*$")


def merge_review_chunks(chunk_outputs: list[dict],
                        related_tickets: list[Any] | None = None,
                        max_findings: int | None = None) -> dict:
    """Reduce the parsed review of every chunk to a single `{'review': {...}}` dict.

    `related_tickets` is the ticket list the prompt was built from. It gives every ticket
    entry a canonical URL, so chunks that describe the same ticket differently merge into one
    entry instead of contradicting each other.

    `max_findings` is the per-review limit from `num_max_findings`. Each chunk may report up
    to that many findings about its own part of the diff, so the union can be several times
    larger; passing the limit cuts the merged list back to what a single-call review would
    publish. `all_key_issues` returns the list before this cut, for callers that must tell a cut
    finding from an absent one.
    """
    reviews = [chunk["review"] for chunk in chunk_outputs
               if isinstance(chunk, dict) and isinstance(chunk.get("review"), dict)]
    if not reviews:
        return {}
    ticket_index = _ticket_url_index(related_tickets)
    if len(reviews) == 1:
        review = dict(reviews[0])
        if "failure_modes" in review:
            review["failure_modes"] = parse_failure_modes(review["failure_modes"])
        if (ticket_index[0] or ticket_index[1]) and isinstance(
                review.get("ticket_compliance_check"), list):
            review["ticket_compliance_check"] = _merge_ticket_compliance(
                [review["ticket_compliance_check"]], ticket_index)
        _cap_key_issues(review, max_findings)
        return {"review": review}

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
            if merge is _merge_ticket_compliance:
                merged[key] = merge(values, ticket_index)
            else:
                merged[key] = merge(values)
        except Exception as e:
            get_logger().warning(f"Failed to merge review field '{key}' across chunks, "
                                 f"keeping the first chunk's value, error: {e}")
            merged[key] = _first_non_empty(values)
    _cap_key_issues(merged, max_findings)
    return {"review": merged}


def _cap_key_issues(review: dict, max_findings: int | None) -> None:
    """Cut the merged findings to the per-review limit, in place.

    Without the cut, N chunks can each publish `num_max_findings` issues, so a chunked review
    exceeds the configured maximum that a single-call review respects.
    """
    if max_findings is None:
        return
    try:
        limit = max(0, int(max_findings))
    except (TypeError, ValueError):
        return
    issues = review.get("key_issues_to_review")
    if isinstance(issues, list) and len(issues) > limit:
        review["key_issues_to_review"] = issues[:limit]


def _chunk_key_issues(chunk_outputs: list[dict]) -> list[list]:
    per_chunk = [chunk["review"].get("key_issues_to_review") for chunk in chunk_outputs
                 if isinstance(chunk, dict) and isinstance(chunk.get("review"), dict)]
    return [issues if isinstance(issues, list) else [] for issues in per_chunk]


def all_key_issues(chunk_outputs: list[dict]) -> list:
    """Return every distinct finding the chunks reported, before `max_findings` cuts the list."""
    return _union_of_lists(_key_issue_identity)(_chunk_key_issues(chunk_outputs))


def chunk_hit_findings_limit(chunk_outputs: list[dict], max_findings: int) -> bool:
    """Say whether a chunk reported `max_findings` findings on its own.

    The model may have stopped at its allowance, so a finding it never listed is not known to be
    gone. A merge that cuts the union is different: `all_key_issues` still holds what it cut.
    """
    return any(len(issues) >= max_findings for issues in _chunk_key_issues(chunk_outputs))


def _rule_name(key: str) -> str:
    normalized = str(key).strip().lower()
    # the effort field carries its scale in its name: 'estimated_effort_to_review_[1-5]'
    if normalized.startswith("estimated_effort_to_review"):
        return "estimated_effort_to_review"
    return normalized


def _first_non_empty(values: list[Any]) -> Any:
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return values[0]


def _normalize_text(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value if value is not None else "")).strip().lower()


def _normalize_path(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip().split(",")[0].strip())
    except (TypeError, ValueError):
        return None


def _merge_effort(values: list[Any]) -> Any:
    """Effort is a 1-5 judgement, so the merged review reports the hardest chunk.

    Summing would saturate at 5 for any PR of a few modest chunks, and the field would stop
    telling PRs apart as soon as chunking is on.
    """
    numbers = [number for number in (_as_int(value) for value in values) if number is not None]
    if not numbers:
        return _first_non_empty(values)
    return max(1, min(MAX_EFFORT, max(numbers)))


def _merge_score(values: list[Any]) -> Any:
    """Lowest score wins: a clean chunk must not raise the grade of a bad one."""
    scored = [(number, value) for number, value in ((_as_int(value), value) for value in values)
              if number is not None]
    if not scored:
        return _first_non_empty(values)
    return min(scored, key=lambda pair: pair[0])[1]


def _worst_of(order: tuple) -> Callable[[list[Any]], Any]:
    """Pick the value ranked last in `order`; values outside it are ignored."""
    def merge(values: list[Any]) -> Any:
        ranked = [(order.index(choice), value) for choice, value
                  in ((_normalize_text(value).replace(" ", "_"), value) for value in values)
                  if choice in order]
        if not ranked:
            return _first_non_empty(values)
        return max(ranked, key=lambda pair: pair[0])[1]
    return merge


def _reported_text(value: Any) -> str:
    """Flatten one chunk's answer to text, or "" when that chunk reported nothing.

    The field is declared as a string, but a model listing several findings answers with a
    list or a mapping, which the review renderer already accepts.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        entries = [entry for entry in (_reported_text(item) for item in value) if entry]
        if not entries:
            return ""
        if len(entries) == 1:
            return entries[0]
        return "\n".join(f"- {entry}" for entry in entries)
    text = as_review_text(value)
    return "" if is_value_no(text) else text


def _merge_findings_text(values: list[Any]) -> Any:
    """Union the chunks that reported something; 'No' only when every chunk said no."""
    reported, seen = [], set()
    for value in values:
        text = _reported_text(value)
        if not text:
            continue
        fingerprint = _normalize_text(text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        reported.append(text)
    if not reported:
        # Every chunk reported nothing. Return the canonical "No" rather than one chunk's raw
        # value: a chunk answering ["No"] would otherwise reach the renderer as a list, which
        # is_value_no does not recognise, and the review would show a concern reading "- No".
        return "No"
    return "\n\n".join(reported)


def _merge_relevant_tests(values: list[Any]) -> Any:
    """A test added in any chunk is a test added by the PR."""
    for value in values:
        if not is_value_no(value):
            return value
    return _first_non_empty(values)


def _union_of_lists(identity: Callable[[Any], Hashable]) -> Callable[[list[Any]], list]:
    def merge(values: list[Any]) -> list:
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
    return (_normalize_path(issue.get("relevant_file")),
            _normalize_text(issue.get("issue_header")),
            _normalize_text(issue.get("issue_content")))


def _todo_identity(todo: Any) -> Hashable:
    if isinstance(todo, str):
        return ("", "0", _normalize_text(todo))
    if not isinstance(todo, dict):
        return _normalize_text(todo)
    return (_normalize_path(todo.get("relevant_file")),
            _normalize_text(todo.get("line_number")),
            _normalize_text(todo.get("content")))


def _sub_pr_identity(sub_pr: Any) -> Hashable:
    if not isinstance(sub_pr, dict):
        return _normalize_text(sub_pr)
    relevant_files = sub_pr.get("relevant_files")
    if isinstance(relevant_files, list) and relevant_files:
        return frozenset(_normalize_path(name) for name in relevant_files)
    return _normalize_text(sub_pr.get("title"))


def _merge_todo_sections(values: list[Any]) -> Any:
    sections = []
    for value in values:
        if isinstance(value, list):
            sections.append(value)
        elif isinstance(value, str) and value.strip() and not is_value_no(value):
            # Preserve prose alongside located TODOs without inventing a file or line.
            sections.append([{"relevant_file": "", "line_number": 0, "content": value}])
    return _union_of_lists(_todo_identity)(sections) or _first_non_empty(values)


def _merge_can_be_split(values: list[Any]) -> list:
    # the prompt asks for at most 3 sub-PRs, so the merged list keeps the same bound
    return _union_of_lists(_sub_pr_identity)(values)[:MAX_SUB_PRS]


def _merge_failure_modes(values: list[Any]) -> list:
    return parse_failure_modes([item for value in values if isinstance(value, list) for item in value])


def _merge_priority_files(values: list[Any]) -> list:
    merged, seen = [], set()
    for value in values:
        for item in value if isinstance(value, list) else []:
            name = str(item).strip()
            if not name or name in seen:
                continue
            seen.add(name)
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


def _ticket_number(value: Any) -> str:
    """Return the last number in `value`, the part a ticket URL or ID ends with."""
    match = _TRAILING_NUMBER_RE.search(str(value if value is not None else ""))
    return match.group(1) if match else ""


def _ticket_url_index(related_tickets: list[Any] | None) -> tuple:
    """Map each related ticket's URL text and trailing number to its canonical URL.

    A number that two tickets share is dropped, so a bare `#12` never merges unrelated tickets
    from different repositories. The canonical URL is what the review links to.
    """
    by_url: dict = {}
    by_number: dict = {}
    ambiguous_numbers = set()
    for ticket in related_tickets or []:
        if not isinstance(ticket, dict):
            continue
        url = str(ticket.get("ticket_url") or "").strip()
        if not url:
            continue
        by_url.setdefault(_normalize_text(url), url)
        number = _ticket_number(ticket.get("ticket_id")) or _ticket_number(url)
        if not number:
            continue
        existing = by_number.get(number)
        if existing is not None and existing != url:
            ambiguous_numbers.add(number)
        else:
            by_number[number] = url
    for number in ambiguous_numbers:
        by_number.pop(number, None)
    return by_url, by_number


def _canonical_ticket_url(raw_url: Any, ticket_index: tuple) -> str:
    """Resolve a chunk's ticket text to a related ticket's URL, falling back to the raw text."""
    text = str(raw_url if raw_url is not None else "").strip()
    if not text:
        return ""
    by_url, by_number = ticket_index
    if _normalize_text(text) in by_url:
        return by_url[_normalize_text(text)]
    number = _ticket_number(text)
    if number and number in by_number:
        return by_number[number]
    return text


def _merge_ticket_compliance(values: list[Any], ticket_index: tuple = ({}, {})) -> list:
    """One entry per ticket; its bullet lists are unioned across the chunks that saw it.

    Each chunk's `ticket_url` is resolved against the related tickets first, so the same
    ticket written as a full URL, an ID, or a shortened URL reaches one entry and one link.
    A requirement that one chunk met and another did not ends up in both lists, which
    `ticket_markdown_logic` already renders as 'Partially compliant'.
    """
    merged: dict = {}
    for value in values:
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            canonical_url = _canonical_ticket_url(entry.get("ticket_url"), ticket_index)
            ticket_url = _normalize_text(canonical_url)
            ticket = ("url", ticket_url) if ticket_url else (
                "missing_url",
                tuple(sorted((str(key), _normalize_text(field_value)) for key, field_value in entry.items())),
            )
            if ticket not in merged:
                record = dict(entry)
                if ticket_url:
                    record["ticket_url"] = canonical_url
                merged[ticket] = record
                continue
            target = merged[ticket]
            for field, field_value in entry.items():
                if field == "ticket_url":
                    # the canonical URL is already on the recorded entry; never union link text
                    continue
                if isinstance(field_value, str) and isinstance(target.get(field), str):
                    target[field] = _union_bullet_lines(target[field], field_value)
                elif not target.get(field):
                    target[field] = field_value
    return list(merged.values())


def _duration_minutes(value: Any) -> float | None:
    match = _DURATION_RE.fullmatch(str(value if value is not None else "").strip())
    if not match:
        return None
    return float(match.group(1)) * (60 if match.group(2).lower() == "h" else 1)


def _format_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))}m"
    hours = minutes / 60
    return f"{int(hours)}h" if float(hours).is_integer() else f"{hours:.1f}h"


def _merge_contribution_time(values: list[Any]) -> Any:
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
    "failure_modes": _merge_failure_modes,
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
