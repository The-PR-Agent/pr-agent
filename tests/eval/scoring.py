"""Score a review's findings against a seeded defect.

The metric that matters most for a small local model is not recall but ``parse_fail``. A review
whose YAML cannot be repaired never reaches ``publish_structured_review``
(``pr_reviewer.py:938-946`` returns early), so it writes no JSON at all. Counting that as
"found nothing" would hide it inside the recall number; it is reported as its own outcome.
"""

from dataclasses import dataclass
from enum import Enum

from unidiff import PatchSet

from pr_agent.algo.review_merge import (
    VOTE_LINE_TOLERANCE,
    finding_line_range,
    line_ranges_overlap,
    normalize_finding_path,
)
from tests.eval.corpus import SeededDefect

#: A finding whose lines come within this many of a seeded hunk counts as pointing at it. Models
#: anchor a defect a line or two off; unidiff's three context lines already sit inside this. Shared
#: with the consensus vote: the harness must match findings the way production clusters them, or it
#: reports recall for a rule the reviewer no longer uses.
LINE_TOLERANCE = VOTE_LINE_TOLERANCE


def defect_line_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Target-side line range of every hunk, per file - the objective label for a seeded defect.

    Derived from the diff rather than hand-written, so a corpus item cannot carry a wrong line
    label. Wording-based signals stay as a secondary match; a model that says "the comparison
    is inverted" without the word "flipped" should still score, and a model that echoes the
    right words about the wrong lines should not.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for patched_file in PatchSet(diff_text):
        path = normalize_finding_path(patched_file.path)
        for hunk in patched_file:
            end = hunk.target_start + max(hunk.target_length, 1) - 1
            ranges.setdefault(path, []).append((hunk.target_start, end))
    return ranges


class Outcome(str, Enum):
    HIT = "hit"                  # a finding on the right file that names the defect
    FILE_ONLY = "file_only"      # right file, but nothing that identifies this defect
    MISS = "miss"                # review parsed, defect not reported
    PARSE_FAIL = "parse_fail"    # no structured review produced at all


@dataclass(frozen=True)
class DefectResult:
    defect_id: str
    defect_class: str
    outcome: Outcome
    leaks_rationale: bool
    matched_finding: str | None = None
    #: "lines", "signal", or "both" - which evidence produced a HIT.
    hit_by: str | None = None
    #: Findings that matched no seeded defect. Not the same as false positives - the diff can
    #: contain real problems nobody seeded - so this is reported as "unmatched", never as
    #: "wrong". Treating it as precision would penalise a model for being right about
    #: something the corpus does not know about.
    unmatched_findings: int = 0


def _finding_text(finding: dict) -> str:
    parts = [str(finding.get(key, "")) for key in ("issue_header", "issue_content", "issue")]
    return " ".join(parts).lower()


def _is_path_suffix(shorter: str, longer: str) -> bool:
    """`longer` ends with `shorter` on a directory boundary: "b/x.py" matches "a/b/x.py", not "ab/x.py"."""
    return shorter == longer or longer.endswith("/" + shorter)


def _same_file(finding_file: str, defect_files: tuple[str, ...]) -> bool:
    found = normalize_finding_path(finding_file)
    if not found:
        return False
    # Models routinely report a basename or a partially-qualified path, and rejecting those
    # would undercount real hits. Suffix matching in either direction covers both - but only on
    # a path-segment boundary, or "_agent/x.py" would count as "pr_agent/x.py".
    return any(
        _is_path_suffix(found, normalize_finding_path(f)) or _is_path_suffix(normalize_finding_path(f), found)
        for f in defect_files
    )


def _overlaps_defect(finding: dict, ranges: list[tuple[int, int]]) -> bool:
    lines = finding_line_range(finding)
    if lines is None:
        return False
    return any(line_ranges_overlap(lines, hunk, LINE_TOLERANCE) for hunk in ranges)


def score_defect(defect: SeededDefect, review: dict | None,
                 diff_text: str | None = None) -> DefectResult:
    """Score one review against one seeded defect.

    ``review`` is the parsed structured review, or None when none was produced. ``diff_text``
    enables line matching; without it only the wording signals apply.
    """
    line_ranges = defect_line_ranges(diff_text) if diff_text else {}
    if not isinstance(review, dict) or not review:
        return DefectResult(defect.id, defect.defect_class, Outcome.PARSE_FAIL,
                            defect.leaks_rationale)

    body = review.get("review")
    if not isinstance(body, dict) or not body:
        return DefectResult(defect.id, defect.defect_class, Outcome.PARSE_FAIL,
                            defect.leaks_rationale)

    findings = body.get("key_issues_to_review")
    if not isinstance(findings, list):
        findings = []

    hit: str | None = None
    hit_by: str | None = None
    file_only = False
    matched = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if not _same_file(finding.get("relevant_file", ""), defect.files):
            continue
        by_signal = any(signal in _finding_text(finding) for signal in defect.signals)
        by_lines = any(_overlaps_defect(finding, ranges) for path, ranges in line_ranges.items()
                       if _same_file(finding.get("relevant_file", ""), (path,)))
        if by_signal or by_lines:
            matched += 1
            if hit is None:
                hit = str(finding.get("issue_header", ""))[:120]
                hit_by = "both" if by_signal and by_lines else ("lines" if by_lines else "signal")
        else:
            file_only = True

    unmatched = max(len(findings) - matched, 0)
    if hit is not None:
        return DefectResult(defect.id, defect.defect_class, Outcome.HIT,
                            defect.leaks_rationale, hit, hit_by, unmatched)
    outcome = Outcome.FILE_ONLY if file_only else Outcome.MISS
    return DefectResult(defect.id, defect.defect_class, outcome, defect.leaks_rationale,
                        None, None, unmatched)


def summarize(results: list[DefectResult]) -> dict:
    """Aggregate per-defect results. Recall counts HIT only; FILE_ONLY is not a find."""
    total = len(results)
    if not total:
        return {"total": 0}
    counts = {outcome: sum(1 for r in results if r.outcome is outcome) for outcome in Outcome}
    scored = [r for r in results if r.outcome is not Outcome.PARSE_FAIL]
    honest = [r for r in scored if not r.leaks_rationale]

    def recall(rows):
        return round(sum(1 for r in rows if r.outcome is Outcome.HIT) / len(rows), 3) if rows else None

    by_class: dict[str, dict] = {}
    for r in scored:
        bucket = by_class.setdefault(r.defect_class, {"n": 0, "hits": 0})
        bucket["n"] += 1
        bucket["hits"] += r.outcome is Outcome.HIT
    for bucket in by_class.values():
        bucket["recall"] = round(bucket["hits"] / bucket["n"], 3)

    return {
        "total": total,
        "parse_fail_rate": round(counts[Outcome.PARSE_FAIL] / total, 3),
        # Recall over reviews that actually parsed. Read it next to parse_fail_rate, never alone:
        # a model that only answers when it finds something would score well on one and badly on
        # the other.
        "recall": recall(scored),
        # Excludes defects whose reversed fix removed a comment naming the bug.
        "recall_no_rationale_leak": recall(honest),
        # How the hits were earned. A corpus where every hit is "signal" is measuring wording.
        "hits_by": {kind: sum(1 for r in scored if r.hit_by == kind) for kind in ("lines", "signal", "both")},
        "file_only": counts[Outcome.FILE_ONLY],
        "miss": counts[Outcome.MISS],
        "unmatched_findings": sum(r.unmatched_findings for r in results),
        "by_class": by_class,
    }
