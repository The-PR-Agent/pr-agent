---
phase: 02-angular-convention-title-rewriting
plan: 02
subsystem: tooling
tags: [python, pytest, conventional-commits, angular-title, gitlab]
requires:
  - phase: 02-angular-convention-title-rewriting
    provides: Pure Angular title validator _normalize_angular_title from Plan 02-01
provides:
  - Conventional-title publish seam gated by enable_conventional_title
  - AI title stash before _prepare_pr_answer title consumption
  - Angular title steering through runtime extra_instructions
affects: [phase-03-org-template-prepend, gitlab-title-publishing, pr-description-prompts]
tech-stack:
  added: []
  patterns:
    - Guarded fork-owned prompt steering via extra_instructions
    - Publish seam passes None, never empty string, when title normalization fails
key-files:
  created:
    - tests/unittest/test_conventional_title_publish_seam.py
  modified:
    - pr_agent/tools/pr_description.py
key-decisions:
  - "Kept _prepare_pr_answer and _prepare_pr_answer_with_markers title-selection blocks untouched; publish seam sources self.ai_title instead."
  - "Steered Angular title generation through runtime extra_instructions instead of editing pr_description_prompts.toml."
patterns-established:
  - "enable_conventional_title alone routes the AI title through _normalize_angular_title before publish_description."
  - "Validator failure flows as publish_description(None, body), preserving existing GitLab MR title."
requirements-completed: [CFG-04, TITLE-01]
coverage:
  - id: D1
    description: "enable_conventional_title auto-forces AI-title publishing without generate_ai_title."
    requirement: CFG-04
    verification:
      - kind: unit
        ref: "python -m pytest tests/unittest/test_conventional_title_publish_seam.py tests/unittest/test_normalize_angular_title.py tests/unittest/test_pr_description_output_core.py -q"
        status: pass
    human_judgment: false
  - id: D2
    description: "Angular title steering block is appended to effective extra_instructions when toggle is enabled."
    requirement: TITLE-01
    verification:
      - kind: unit
        ref: "tests/unittest/test_conventional_title_publish_seam.py#test_conventional_title_augments_effective_extra_instructions"
        status: pass
    human_judgment: false
duration: 10min
completed: 2026-07-03
status: complete
---

# Phase 02 Plan 02: Conventional Title Publish Wiring Summary

**Conventional-title toggle now steers Angular AI titles and publishes normalized output safely**

## Performance

- **Duration:** 10 min
- **Started:** 2026-07-03T03:30:50Z
- **Completed:** 2026-07-03T03:40:14Z
- **Tasks:** 2
- **Files modified:** 2 implementation/test files

## Accomplishments

- Added publish-seam matrix coverage for off/off, AI-title-only, conventional-only salvage, conventional-only fallback, and both-on cases.
- Captured the AI title before `_prepare_pr_answer*` consumes `self.data["title"]`, preserving the locked inline title-selection blocks.
- Wired `enable_conventional_title` so normalized AI titles reach `publish_description`, with validator failure passed as `None`.
- Appended Angular title-format guidance to effective `extra_instructions` when the toggle is enabled.

## Task Commits

1. **Task 1: RED publish-seam behavior matrix test** - `d308080f` (test)
2. **Task 2: GREEN conventional-title publish wiring** - `d2a747d3` (feat)

## Files Created/Modified

- `tests/unittest/test_conventional_title_publish_seam.py` - Publish-title argument matrix plus steering assertion.
- `pr_agent/tools/pr_description.py` - Angular steering block, AI-title stash, widened publish-seam conditional.

## Decisions Made

- No helper extraction was needed; tests drive the existing async `run()` seam with prediction/preparation stubs.
- Kept `pr_agent/settings/pr_description_prompts.toml` untouched per plan and project decision.
- Kept `_prepare_pr_answer` and `_prepare_pr_answer_with_markers` title-selection blocks byte-identical.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `python -m ruff check pr_agent/tools/pr_description.py` could not run because `ruff` is not installed in the active Python environment.
- `python -m flake8 pr_agent/tools/pr_description.py tests/unittest/test_conventional_title_publish_seam.py` could not run because `flake8` is not installed.
- No `.venv/Scripts/python.exe` exists in this checkout, so there was no local virtualenv fallback for those tools.

## Verification

- `python -m pytest tests/unittest/test_conventional_title_publish_seam.py -q` -> RED confirmed before implementation: 2 passed, 4 failed.
- `python -m pytest tests/unittest/test_conventional_title_publish_seam.py tests/unittest/test_normalize_angular_title.py tests/unittest/test_pr_description_output_core.py -q` -> 60 passed.
- `python -m py_compile pr_agent/tools/pr_description.py tests/unittest/test_conventional_title_publish_seam.py` -> passed.
- `git diff d308080f..d2a747d3 --unified=0 -- pr_agent/tools/pr_description.py` -> only constants, `__init__`, AI-title stash, and publish seam changed.
- `git diff --name-only d308080f~1..HEAD -- pr_agent/settings/pr_description_prompts.toml` -> no output.

## Known Stubs

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 3 can build the org template prepend on top of a title path that is now gated, normalized, and fallback-safe.

## Self-Check: PASSED

- Found created/modified files: `tests/unittest/test_conventional_title_publish_seam.py`, `pr_agent/tools/pr_description.py`, `02-02-SUMMARY.md`.
- Found task commits: `d308080f`, `d2a747d3`.
- Confirmed existing untracked `.codegraph/` and `bash.exe.stackdump` were left untouched.

---
*Phase: 02-angular-convention-title-rewriting*
*Completed: 2026-07-03*
