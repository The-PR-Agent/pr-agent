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

Those three rules all rest on the chunks being *disjoint*. Consensus sampling breaks that
premise: `num_samples` samples each answer about the whole diff, so summing or taking the worst
value would scale the estimate with the sample count and let one outlier decide the verdict.
`merge_review_samples` therefore overrides the same fields with central-tendency rules (median,
majority), and `vote_review_samples` decides the findings by location vote. When both apply, the
sample reduction runs inside each chunk and the chunk reduction across them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Hashable, List, Mapping, Optional

from pr_agent.algo.utils import as_review_text, is_value_no
from pr_agent.log import get_logger

MAX_EFFORT = 5
MAX_SUB_PRS = 3
CONTRIBUTION_TIME_CASES = ("best_case", "average_case", "worst_case")
RISK_LEVELS = ("low", "medium", "high")
MERGE_RECOMMENDATIONS = ("safe_to_merge", "merge_with_caution", "changes_required")

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([mh])", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def merge_review_chunks(chunk_outputs: List[dict]) -> dict:
    """Reduce the parsed review of every *disjoint* chunk to a single `{'review': {...}}` dict.

    The chunks partition the diff, so findings union and judgements take the worst value. For
    several samples of the *same* diff use `merge_review_samples` instead - the rules differ.
    """
    return _merge_reviews(chunk_outputs, _MERGE_RULES, "chunks")


def merge_review_samples(sample_outputs: List[dict]) -> dict:
    """Reduce independent samples of the *same* review to one by central tendency.

    Every sample answers about the whole diff, so the chunk rules distort here: summing
    contribution time scales the estimate with `num_samples`, and taking the worst risk level or
    the union of security concerns lets one outlier sample decide the verdict - the very noise
    consensus sampling exists to average away. Judgements therefore take the median and
    categorical verdicts the majority (ties resolving to the more conservative value), and a
    concern has to be reported by more than half the samples to be published. `key_issues_to_review`
    is left to `vote_review_samples`, which votes on it by location.

    Sampling and chunking compose in one direction: this same-scope reduction runs *inside* each
    chunk, then `merge_review_chunks` runs across the chunks. Contribution time is therefore the
    median within a chunk and the sum across chunks, which is what both rules intend.
    """
    return _merge_reviews(sample_outputs, _SAMPLE_MERGE_RULES, "samples", pad_absent=True)


def _merge_reviews(outputs: List[dict], rules: dict, scope: str, pad_absent: bool = False) -> dict:
    reviews = [output["review"] for output in outputs
               if isinstance(output, dict) and isinstance(output.get("review"), dict)]
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
        # a sample that omitted the key still counts against a majority: on the sample path every
        # reducer's denominator has to be the sample count, not the number that answered, or a
        # concern one of three samples reported clears "more than half of one" and publishes
        values = ([review.get(key) for review in reviews] if pad_absent
                  else [review[key] for review in reviews if key in review])
        merge = rules.get(_rule_name(key), _first_non_empty)
        try:
            merged[key] = merge(values)
        except Exception as e:
            get_logger().warning(f"Failed to merge review field '{key}' across {scope}, "
                                 f"keeping the first value, error: {e}")
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


def _merge_findings_text(values: List[Any]) -> Any:
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


# --- same-scope reducers, for several samples of one diff -------------------------------------


def _majority(count: int) -> int:
    """Strictly more than half - 2 of 2, 2 of 3, 3 of 5.

    The bar for a *field*, where publishing what a minority of samples said is the noisy answer.
    The bar for a *finding* is `consensus_votes_needed`, which is deliberately lower: a missed
    defect costs more than a finding the reader dismisses.
    """
    return count // 2 + 1


def _median_of(numbers: List[int]) -> float:
    ordered = sorted(numbers)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _sample_effort(values: List[Any]) -> Any:
    """Median of the samples' 1-5 judgements: one alarmed sample must not set the effort."""
    numbers = [number for number in (_as_int(value) for value in values) if number is not None]
    if not numbers:
        return _first_non_empty(values)
    return max(1, min(MAX_EFFORT, int(round(_median_of(numbers)))))


def _sample_score(values: List[Any]) -> Any:
    """The sample whose score is closest to the median, keeping that sample's own formatting."""
    scored = [(number, value) for number, value in ((_as_int(value), value) for value in values)
              if number is not None]
    if not scored:
        return _first_non_empty(values)
    median = _median_of([number for number, _ in scored])
    return min(scored, key=lambda pair: (abs(pair[0] - median), pair[0]))[1]


def _majority_of(order: tuple) -> Callable[[List[Any]], Any]:
    """The value most samples chose; a tie goes to the one ranked last in `order`.

    Values outside `order` are ignored, and a field no sample answered in a recognised way keeps
    the first non-empty value, exactly as the chunk rule does.
    """
    def merge(values: List[Any]) -> Any:
        ranked = [(order.index(choice), value) for choice, value
                  in ((_normalize_text(value).replace(" ", "_"), value) for value in values)
                  if choice in order]
        if not ranked:
            return _first_non_empty(values)
        counts: dict = {}
        for rank, value in ranked:
            counts.setdefault(rank, [0, value])[0] += 1
        # most votes first, then the most conservative rank - so 1-1 resolves to the worse value
        best_rank = max(counts, key=lambda rank: (counts[rank][0], rank))
        return counts[best_rank][1]
    return merge


def _sample_findings_text(values: List[Any]) -> Any:
    """Publish a free-text concern only when more than half the samples reported one.

    The wording differs between samples, so this counts samples rather than clustering text, and
    reports the fullest wording among those that answered. `min_votes` does not reach here - it
    filters `key_issues_to_review` only - so without this one sample of three could publish a
    security concern the other two did not see.
    """
    reported = [text for text in (_reported_text(value) for value in values) if text]
    if len(reported) < _majority(len(values)):
        return "No"
    return max(reported, key=len)


def _sample_relevant_tests(values: List[Any]) -> Any:
    """"Yes" only when most samples saw a test: whether the diff adds tests is a fact they share."""
    answered = [value for value in values if not is_value_no(value)]
    if len(answered) < _majority(len(values)):
        return "No"
    return answered[0]


def _majority_of_lists(identity: Callable[[Any], Hashable]) -> Callable[[List[Any]], list]:
    """Keep the list entries that more than half the samples named, in first-seen order."""
    def merge(values: List[Any]) -> list:
        needed = _majority(len(values))
        counts: dict = {}
        order: list = []
        for value in values:
            seen = set()
            for item in value if isinstance(value, list) else []:
                key = identity(item)
                if key in seen:
                    continue
                seen.add(key)
                if key not in counts:
                    counts[key] = [0, item]
                    order.append(key)
                counts[key][0] += 1
        return [counts[key][1] for key in order if counts[key][0] >= needed]
    return merge


def _sample_priority_files(values: List[Any]) -> list:
    return _majority_of_lists(lambda name: str(name).strip().lower())(values)


def _sample_can_be_split(values: List[Any]) -> list:
    return _majority_of_lists(_sub_pr_identity)(values)[:MAX_SUB_PRS]


def _sample_contribution_time(values: List[Any]) -> Any:
    """Median per case: the samples estimate the same work, so adding them scales with num_samples."""
    estimates = [value for value in values if isinstance(value, dict)]
    if not estimates:
        return _first_non_empty(values)
    medians = {}
    for case in CONTRIBUTION_TIME_CASES:
        minutes = [_duration_minutes(estimate.get(case)) for estimate in estimates]
        if any(value is None for value in minutes):
            get_logger().debug("Contribution time estimates cannot be reduced across samples, "
                               "keeping the first sample's estimate", artifact={"case": case})
            return _first_non_empty(values)
        medians[case] = _format_duration(_median_of(minutes))
    return medians


#: Same-scope overrides. Everything not listed keeps the chunk rule, which is already right for a
#: sample: `todo_sections` and `ticket_compliance_check` report facts about the diff that any
#: sample may have spotted, and `key_issues_to_review` is decided by `vote_review_samples`.
_SAMPLE_MERGE_RULES: dict = {
    **_MERGE_RULES,
    "estimated_effort_to_review": _sample_effort,
    "score": _sample_score,
    "risk_level": _majority_of(RISK_LEVELS),
    "merge_recommendation": _majority_of(MERGE_RECOMMENDATIONS),
    "security_concerns": _sample_findings_text,
    "insights_from_user_answers": _sample_findings_text,
    "relevant_tests": _sample_relevant_tests,
    "review_priority_files": _sample_priority_files,
    "can_be_split": _sample_can_be_split,
    "contribution_time_cost_estimate": _sample_contribution_time,
}


# --- consensus over independent samples -------------------------------------------------------

#: Two findings on the same file whose line ranges come within this many lines of each other are
#: treated as the same finding. Models place the same defect a line or two apart between samples.
VOTE_LINE_TOLERANCE = 2


#: Word-overlap (Jaccard) at or above which two findings on one file are taken to describe the
#: same defect when neither carries a usable line range.
VOTE_TEXT_SIMILARITY = 0.5
#: Below this many distinctive words on either side, wording carries too little signal to cluster
#: on, and only the exact-identity rule applies. Two findings sharing nothing but boilerplate
#: ("does not handle the error case") would otherwise merge into one.
VOTE_MIN_DISTINCTIVE_WORDS = 4

#: Words that carry no discriminating signal in a review finding.
_STOP_WORDS = frozenset("""
a an the this that these those it its is are was were be been being no not and or but if then
of in on at to for with from by as into over under about than so such can could may might must
will would should shall do does did done has have had there here when where which who whom what
why how all any both each few more most other some only own same too very just also both
code line lines file files function method value values case cases issue problem bug error
""".split())


def finding_line_range(issue: dict) -> Optional[tuple[int, int]]:
    """The (start, end) target lines a finding points at, or None when it names none."""
    try:
        start = int(str(issue.get("start_line", "")).strip())
    except (TypeError, ValueError):
        return None
    try:
        end = int(str(issue.get("end_line", start)).strip())
    except (TypeError, ValueError):
        end = start
    if start <= 0:
        return None
    return start, max(start, end)


def normalize_finding_path(value: Any) -> str:
    """Lower-cased path with any leading "./" or "/" removed, for comparing reported paths."""
    # strip a leading "./" or "/" as a prefix - lstrip("./") would eat the dot of ".github/x"
    path = _normalize_text(value)
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def line_ranges_overlap(a: tuple[int, int], b: tuple[int, int],
                        tolerance: int = VOTE_LINE_TOLERANCE) -> bool:
    """Two line ranges that come within `tolerance` lines of each other."""
    return a[0] - tolerance <= b[1] and b[0] - tolerance <= a[1]


def _distinctive_words(issue: dict) -> set:
    text = f"{issue.get('issue_header', '')} {issue.get('issue_content', '')}"
    return {word for word in re.findall(r"[a-z0-9_]{3,}", _normalize_text(text))
            if word not in _STOP_WORDS}


def _similar_wording(a: dict, b: dict) -> bool:
    """Do two findings on one file describe the same defect in different words?

    Only reached when neither finding named a usable line, which is the common shape for a
    file-level problem ("no tests for this module") and for the small models this vote exists to
    help. Requiring identical text there - the previous fallback - dropped a defect every sample
    independently found, because each worded it differently.
    """
    words_a, words_b = _distinctive_words(a), _distinctive_words(b)
    if (len(words_a) < VOTE_MIN_DISTINCTIVE_WORDS
            or len(words_b) < VOTE_MIN_DISTINCTIVE_WORDS):
        return _key_issue_identity(a) == _key_issue_identity(b)
    overlap = len(words_a & words_b) / len(words_a | words_b)
    return overlap >= VOTE_TEXT_SIMILARITY


def _same_finding(a: dict, b: dict) -> bool:
    """Location first, wording second: samples describe one bug in different words."""
    if normalize_finding_path(a.get("relevant_file")) != normalize_finding_path(b.get("relevant_file")):
        return False
    lines_a, lines_b = finding_line_range(a), finding_line_range(b)
    if lines_a and lines_b:
        return line_ranges_overlap(lines_a, lines_b)
    return _similar_wording(a, b)


_HEADER_RE = re.compile(r"\*\*(.+?)\*\*")


def normalized_header(body: str) -> str:
    """The finding's bold header, lowercased and stripped of punctuation, or its first line."""
    match = _HEADER_RE.search(body or "")
    text = match.group(1) if match else ((body or "").splitlines()[0] if body else "")
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _state_range(record: Mapping) -> Optional[tuple[int, int]]:
    start, end = record.get("line_start"), record.get("line_end")
    if start is None:
        return None
    return int(start), int(end if end is not None else start)


def same_finding_across_runs(a: Mapping, b: Mapping) -> bool:
    """Is `b` a reworded restatement of the same defect `a` reported, in an earlier run?

    Stricter than `_same_finding`: that matcher accepts any line-range overlap, which is fine for
    samples of the *same* diff (identical prompt, identical diff, so an overlapping neighbour is
    implausible) but is too permissive across commits - a wide range like 67-87 overlaps almost
    anything nearby, so two distinct defects three lines apart would wrongly merge. Here the path
    must match exactly, both records must carry a line range and their start lines must sit within
    `VOTE_LINE_TOLERANCE` of each other, and only then does wording decide: either the same
    normalized bold header, or `_similar_wording`'s Jaccard test (`VOTE_TEXT_SIMILARITY`) on the
    whole body text. A finding with no line range never fuzzy-matches; it keeps exact-hash
    identity only.
    """
    if normalize_finding_path(a.get("path")) != normalize_finding_path(b.get("path")):
        return False
    range_a, range_b = _state_range(a), _state_range(b)
    if range_a is None or range_b is None or abs(range_a[0] - range_b[0]) > VOTE_LINE_TOLERANCE:
        return False
    body_a, body_b = a.get("body", ""), b.get("body", "")
    header_a, header_b = normalized_header(body_a), normalized_header(body_b)
    if header_a and header_a == header_b:
        return True
    return _similar_wording({"issue_content": body_a}, {"issue_content": body_b})


@dataclass(frozen=True)
class ConsensusResult:
    """The voted review, plus what the vote discarded.

    `dropped` is why this is not just a dict: a finding that lost the vote is not a finding that
    was fixed, so the caller has to know the review is partial before it resolves anything or
    tells the reader nothing was found.
    """
    review: dict
    dropped: int = 0
    candidates: int = 0
    needed: int = 0
    samples: int = 0


def consensus_votes_needed(min_votes: int, sample_count: int) -> int:
    """How many samples a finding must appear in.

    `min_votes <= 0` means auto: more than half the samples that parsed. That keeps two samples
    from demanding unanimity, where any disagreement about a defect's line empties the review.
    An explicit value is honoured, clamped to the samples that parsed - losing a sample must
    lower the bar, never silently empty the review.
    """
    if sample_count <= 0:
        return 1
    try:
        requested = int(min_votes)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        # half, rounded up: 1 of 2, 2 of 3, 3 of 5
        return max(1, (sample_count + 1) // 2)
    return max(1, min(requested, sample_count))


def vote_review_samples(samples: List[dict], min_votes: int, max_findings: int = 0) -> ConsensusResult:
    """Reduce independent samples of the *same* review to one by consensus.

    A small model at non-zero temperature reports a different subset of the real defects on
    every run. Taking the union raises recall; requiring a finding to recur in ``min_votes``
    samples restores precision. Findings are clustered by location (file + overlapping lines)
    rather than wording, and clusters are ordered by votes so the merged review leads with what
    every sample agreed on. Every other field is reduced by `merge_review_samples`, which takes
    the samples' central tendency rather than their worst case.

    ``max_findings`` (0 = unbounded) caps the kept findings the way the prompt caps a single
    call's, so a sampled review cannot exceed the ceiling the single-call flow respects. Findings
    lost to either the threshold or the cap are counted in the result's ``dropped``.
    """
    reviews = [s["review"] for s in samples
               if isinstance(s, dict) and isinstance(s.get("review"), dict) and s["review"]]
    if not reviews:
        return ConsensusResult({})
    if len(reviews) == 1:
        return ConsensusResult({"review": dict(reviews[0])}, samples=1, needed=1)

    needed = consensus_votes_needed(min_votes, len(reviews))
    clusters: list[dict] = []  # {"issue": representative, "votes": set(sample idx)}
    for index, review in enumerate(reviews):
        issues = review.get("key_issues_to_review")
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, dict):
                continue
            for cluster in clusters:
                # a sample cannot vote twice for its own cluster - two findings in one sample are
                # distinct by construction, even when their line ranges sit within tolerance
                if index in cluster["votes"]:
                    continue
                if _same_finding(cluster["issue"], issue):
                    cluster["votes"].add(index)
                    # keep the most informative wording as the representative
                    if len(str(issue.get("issue_content", ""))) > len(str(cluster["issue"].get("issue_content", ""))):
                        cluster["issue"] = issue
                    break
            else:
                clusters.append({"issue": issue, "votes": {index}})

    kept = [c for c in clusters if len(c["votes"]) >= needed]
    kept.sort(key=lambda c: (-len(c["votes"]),
                             normalize_finding_path(c["issue"].get("relevant_file")),
                             (finding_line_range(c["issue"]) or (0, 0))[0]))
    if max_findings > 0 and len(kept) > max_findings:
        get_logger().info(f"Consensus kept {len(kept)} findings, truncating to the configured "
                          f"num_max_findings of {max_findings}")
        kept = kept[:max_findings]
    dropped = len(clusters) - len(kept)
    if dropped:
        get_logger().info(f"Consensus dropped {dropped} of {len(clusters)} candidate findings "
                          f"not agreed by {needed} of {len(reviews)} samples")

    merged = merge_review_samples([{"review": r} for r in reviews])
    merged["review"]["key_issues_to_review"] = [c["issue"] for c in kept]
    return ConsensusResult(merged, dropped=dropped, candidates=len(clusters),
                           needed=needed, samples=len(reviews))
