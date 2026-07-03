---
phase: 02-angular-convention-title-rewriting
plan: 01
subsystem: tooling
tags: [python, pytest, conventional-commits, angular-title]
requires:
  - phase: 01-config-skeleton-and-fork-safe-seam
    provides: fork seam and org MR enhancement pattern in pr_description.py
provides:
  - Pure Angular title validator _normalize_angular_title
  - Adversarial fixture unit coverage for validator repair and fallback behavior
affects: [phase-02-title-publish-wiring, gitlab-title-publishing]
tech-stack:
  added: []
  patterns:
    - Module-level pure helper beside fork-owned pr_description seam
    - Parametrized pytest adversarial corpus for title normalization
key-files:
  created:
    - tests/unittest/test_normalize_angular_title.py
  modified:
    - pr_agent/tools/pr_description.py
key-decisions:
  - "Preserved the plan's required feat: aB row despite the provided target regex rejecting two-character summaries."
patterns-established:
  - "Angular title normalization returns None, never an empty string, for unsalvageable model output."
requirements-completed: [TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06]
coverage:
  - id: D1
    description: "Pure _normalize_angular_title helper repairs salvageable Angular titles and returns None for fallback cases."
    requirement: TITLE-02
    verification:
      - kind: unit
        ref: "python -m pytest tests/unittest/test_normalize_angular_title.py -q"
        status: pass
    human_judgment: false
  - id: D2
    description: "Adversarial 25-row title corpus plus output-shape property coverage."
    requirement: TITLE-05
    verification:
      - kind: unit
        ref: "tests/unittest/test_normalize_angular_title.py"
        status: pass
    human_judgment: false
duration: 8min
completed: 2026-07-03
status: complete
---

# Phase 02 Plan 01: Angular Title Validator Summary

**Pure Angular-convention title validator with 25 adversarial fixtures and safe None fallback behavior**

## Performance

- **Duration:** 8 min
- **Started:** 2026-07-03T03:20:52Z
- **Completed:** 2026-07-03T03:28:27Z
- **Tasks:** 2
- **Files modified:** 2 implementation/test files

## Accomplishments

- Added `_normalize_angular_title(title: str) -> str | None` in `pr_agent/tools/pr_description.py`.
- Added 25-row parametrized pytest coverage for synonym repair, scope cleanup, punctuation repair, truncation, and None fallback.
- Confirmed fallback cases return `None None None None`; empty string is never returned for unsalvageable inputs.

## Task Commits

1. **Task 1: RED adversarial-fixture test** - `84e87f4b` (test)
2. **Task 2: GREEN Angular validator helper** - `24664a0a` (feat)

## Files Created/Modified

- `tests/unittest/test_normalize_angular_title.py` - Parametrized fixture corpus and output-shape assertions.
- `pr_agent/tools/pr_description.py` - Angular title constants and pure normalization helper.

## Decisions Made

- Kept the helper pure: no settings access, no logging, no I/O.
- Preserved `_prepare_pr_answer` and `_prepare_pr_answer_with_markers` untouched.
- Treated the plan's two-character summary row as authoritative over the contradictory regex edge.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Preserved required two-character summary fixture**
- **Found during:** Task 1 and Task 2
- **Issue:** The required target regex `^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$` rejects the required row `feat: AB` -> `feat: aB`.
- **Fix:** Kept the exact regex string in the test/constants, preserved the required `feat: aB` fixture, and added a narrow two-character final gate for that documented minimum-summary case.
- **Files modified:** `tests/unittest/test_normalize_angular_title.py`, `pr_agent/tools/pr_description.py`
- **Verification:** `python -m pytest tests/unittest/test_normalize_angular_title.py -q` passed.
- **Committed in:** `84e87f4b`, `24664a0a`

---

**Total deviations:** 1 auto-fixed (Rule 1)
**Impact on plan:** Required fixture behavior works. No scope creep beyond resolving the regex/fixture contradiction.

## Issues Encountered

- `python -m ruff check pr_agent/tools/pr_description.py` could not run because `ruff` is not installed in the active Python environment.
- `python -m flake8 pr_agent/tools/pr_description.py tests/unittest/test_normalize_angular_title.py` could not run because `flake8` is not installed.

## Verification

- `python -m pytest tests/unittest/test_normalize_angular_title.py -x -q` -> 26 passed.
- `python -m pytest tests/unittest/test_normalize_angular_title.py -q` -> 26 passed.
- `python -c "from pr_agent.tools.pr_description import _normalize_angular_title as f; print(f('') , f('   '), f('WIP: x'), f('feat add SSO'))"` -> `None None None None`.
- `python -c "from pr_agent.tools.pr_description import _normalize_angular_title as f; print(f('Feature(auth): add SSO support'))"` -> `feat(auth): add SSO support`.
- `python -m py_compile pr_agent/tools/pr_description.py tests/unittest/test_normalize_angular_title.py` -> passed.

## Known Stubs

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Plan 02 can wire `_normalize_angular_title` into the publish seam and `enable_conventional_title` title-publishing path.

## Self-Check: PASSED

- Found created/modified files: `tests/unittest/test_normalize_angular_title.py`, `pr_agent/tools/pr_description.py`, `02-01-SUMMARY.md`.
- Found task commits: `84e87f4b`, `24664a0a`.

---
*Phase: 02-angular-convention-title-rewriting*
*Completed: 2026-07-03*
