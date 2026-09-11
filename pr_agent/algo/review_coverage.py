"""Per-file, line-weighted record of what a review actually looked at.

A file the model saw whole is reviewed. A clipped patch earns half credit: the model saw some
of it and we cannot say which half mattered. A failed chunk, a budget skip and an ignored file
earn nothing. Deletion-only files carry no changed lines to review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STATUS_CREDIT = {
    "reviewed": 1.0,
    "clipped": 0.5,
    "low_priority_summary": 0.0,
    "skipped_budget": 0.0,
    "chunk_failed": 0.0,
    "ignored": 0.0,
    "deletion_only": 0.0,
}
FULL_STATUSES = {"reviewed", "deletion_only", "ignored"}


def patch_line_counts(patch: str) -> tuple[int, int]:
    """Count a unified diff patch's added and removed lines, for providers that never report
    num_plus_lines/num_minus_lines directly (local/plain-diff, gerrit, bitbucket, codecommit).

    Everything before the first `@@` hunk header - `diff --git`, `index`, and the
    `--- a/file`/`+++ b/file` file-header lines - is skipped by position, not by matching the
    `+++`/`---` prefix: a real removed or added line can itself start with those characters
    (an SQL comment `-- old comment` renders as a diff line `--- old comment`) and must not be
    mistaken for a header. A patch with no hunk header at all (already-stripped or malformed)
    counts as empty.
    """
    if not patch:
        return 0, 0
    plus = minus = 0
    seen_hunk = False
    for line in patch.splitlines():
        if not seen_hunk:
            if line.startswith("@@"):
                seen_hunk = True
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
    return plus, minus


def changed_lines_from_patch(patch: str) -> int:
    """Total added+removed lines in a unified diff patch. See `patch_line_counts`."""
    plus, minus = patch_line_counts(patch)
    return plus + minus


@dataclass
class FileCoverage:
    path: str
    changed_lines: int
    status: str

    def __post_init__(self) -> None:
        if self.status not in STATUS_CREDIT:
            raise ValueError(f"unknown coverage status {self.status!r}")


@dataclass
class CoverageLedger:
    files: dict[str, FileCoverage] = field(default_factory=dict)

    def add(self, entry: FileCoverage) -> None:
        self.files[entry.path] = entry

    def mark(self, path: str, status: str) -> None:
        entry = self.files.get(path)
        if entry is None:
            self.files[path] = FileCoverage(path, changed_lines=0, status=status)
        else:
            entry.status = status if status in STATUS_CREDIT else entry.status

    @property
    def reviewed_ratio(self) -> float:
        total = sum(f.changed_lines for f in self.files.values())
        if total == 0:
            return 1.0
        credit = sum(f.changed_lines * STATUS_CREDIT[f.status] for f in self.files.values())
        return credit / total

    def not_fully_reviewed(self) -> list[str]:
        return sorted(p for p, f in self.files.items() if f.status not in FULL_STATUSES)

    def render_footer(self) -> str:
        pct = round(self.reviewed_ratio * 100)
        partial = self.not_fully_reviewed()
        line = f"Reviewed {pct}% of changed lines"
        if partial:
            line += f" · {len(partial)} file(s) not fully reviewed"
        return line
