---
phase: 01-config-skeleton-and-fork-safe-seam
plan: 01
subsystem: config
tags: [dynaconf, toml, pr_description, org-template, fork-seam]

requires: []
provides:
  - "Two defaults-off [pr_description] toggles: enable_conventional_title, enable_org_template"
  - "Fork-owned org_template.md asset (What/Why, Note/Risk, unchecked Checklist)"
  - "Inert module-level loader: _ORG_TEMPLATE_PATH + load_org_template() in pr_description.py"
affects: [angular-title-rewriting, org-template-prepend]

tech-stack:
  added: []
  patterns:
    - "Fork changes grouped under a single comment block to localize upstream rebase conflicts"
    - "Fork-owned template body stored as a package file, loaded via package-relative Path, never inlined into shared prompt TOML"

key-files:
  created:
    - pr_agent/settings/org_template.md
    - tests/unittest/test_org_template_config.py
  modified:
    - pr_agent/settings/configuration.toml
    - pr_agent/tools/pr_description.py

key-decisions:
  - "Loader helper is defined but unwired this phase so describe output stays byte-identical (Phase 3 wires it)"
  - "Toggles use bare key=value bool style matching existing generate_ai_title=false convention"

patterns-established:
  - "Fork seam: contiguous, comment-delimited config block for minimal rebase surface"
  - "Package-relative asset loading via Path(__file__).parent.parent / settings / <file>"

requirements-completed: [CFG-01, CFG-02, CFG-03]

coverage:
  - id: D1
    description: "enable_conventional_title and enable_org_template exist under [pr_description], both default false, absent-safe readable via .get(<flag>, False)"
    requirement: "CFG-01"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_config.py#test_org_toggles_default_false"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_org_template_config.py#test_absent_flag_read_is_safe"
        status: pass
    human_judgment: false
  - id: D2
    description: "enable_org_template toggle present and defaults false"
    requirement: "CFG-02"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_config.py#test_org_toggles_default_false"
        status: pass
    human_judgment: false
  - id: D3
    description: "Fork-owned org_template.md loads from Python via package-relative path (not inlined into prompt TOML)"
    requirement: "CFG-03"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_config.py#test_org_template_loads_from_package_path"
        status: pass
    human_judgment: false

duration: ~15min
completed: 2026-07-03
status: complete
---

# Phase 01 / Plan 01: Config Skeleton Seam Summary

**Two defaults-off `[pr_description]` toggles, a fork-owned `org_template.md` asset, and an inert package-relative loader — the mergeable seam Phases 2 and 3 build on, with describe output unchanged.**

## Performance

- **Duration:** ~15 min (including resume/close-out)
- **Completed:** 2026-07-03
- **Tasks:** 3
- **Files modified:** 4 (2 created, 2 modified)

## Accomplishments
- Added `enable_conventional_title=false` and `enable_org_template=false` under `[pr_description]`, grouped under a single fork comment to localize upstream rebase conflicts (CFG-01, CFG-02).
- Created fork-owned `pr_agent/settings/org_template.md` with What/Why, Note/Risk, and an unchecked Checklist — not inlined into `pr_description_prompts.toml` (CFG-03).
- Added module-level `_ORG_TEMPLATE_PATH` constant and `load_org_template()` helper in `pr_description.py`; both inert (unwired) this phase so `describe` output stays byte-identical.
- Added `tests/unittest/test_org_template_config.py` proving absent-safe reads and package-relative template loadability (3 tests, all passing).

## Task Commits

1. **Task 1: Add the two defaults-off toggles** - `51a29b0e` (feat)
2. **Task 2: Create org template asset and inert loader** - `28e9e98e` (feat)
3. **Task 3: Test absent-safe reads and template loadability** - `a8393b5f` (test)

## Files Created/Modified
- `pr_agent/settings/configuration.toml` - Added two fork toggles under `[pr_description]`
- `pr_agent/settings/org_template.md` - Fork-owned org description body
- `pr_agent/tools/pr_description.py` - `_ORG_TEMPLATE_PATH` + `load_org_template()` (inert)
- `tests/unittest/test_org_template_config.py` - Absent-safe read + template-load tests

## Decisions Made
- Loader defined but not called from any output path this phase — preserves byte-identical `describe` output (CFG-05 precondition); Phase 3 wires it.
- Bare `key=value` bool style matches existing `generate_ai_title=false` convention.

## Deviations from Plan

None - plan executed as written. Note: this plan was resumed mid-execution — Tasks 1–2 were committed in a prior interrupted session; the close-out session added the missing Task 3 test, verified all three automated checks pass, and wrote this SUMMARY.

## Issues Encountered
Plan 01-01 was found partially executed (Tasks 1–2 committed, no SUMMARY, stale STATE.md) at resume. Per the safe-resume gate, Tasks 1–2 commits were preserved and only the missing Task 3 + close-out were completed. All three tasks' automated verifications pass.

## Next Phase Readiness
- Config seam and fork-owned template asset are in place and defaults-off.
- Plans 01-02 (env-override test + docs) and 01-03 (byte-identical-when-off test) can proceed.

---
*Phase: 01-config-skeleton-and-fork-safe-seam*
*Completed: 2026-07-03*
