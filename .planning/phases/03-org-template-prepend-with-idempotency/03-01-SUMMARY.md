---
phase: 03-org-template-prepend-with-idempotency
plan: 01
subsystem: tooling
tags: [python, pytest, pr-description, gitlab, org-template]
requires:
  - phase: 01-config-skeleton-and-fork-safe-seam
    provides: enable_org_template toggle and fork-owned org_template.md loader
provides:
  - Sentinel-wrapped org template prepend gated by enable_org_template
  - Checklist-state preservation on reruns
  - what_why and note_risk YAML key handling through keys_fix and extra_instructions
  - Marker-mode skip path for org template behavior
affects: [phase-03-org-template-prepend, gitlab-description-publishing, pr-description-output]
tech-stack:
  added: []
  patterns:
    - Guarded fork-owned prompt steering via extra_instructions
    - Sentinel-bounded replacement for idempotent generated markdown blocks
    - Existing-provider read before publish to preserve human checkbox state
key-files:
  created:
    - tests/unittest/test_org_template_prepend.py
  modified:
    - pr_agent/git_providers/git_provider.py
    - pr_agent/tools/pr_description.py
    - tests/unittest/test_describe_byte_identical_when_off.py
key-decisions:
  - "Rendered the org template from the fork-owned template headings/checklist, with AI fields inserted into What/Why and Note/Risk."
  - "Preserved only matching checklist states from the previous sentinel block; fresh AI content replaces prior What/Risk content."
  - "Skipped org-template prepend in marker mode to keep marker replacement output unchanged."
patterns-established:
  - "enable_org_template extends keys_fix with what_why/note_risk only when marker mode is off."
  - "Fresh generated body is stripped of any old sentinel block before prepending a new block."
requirements-completed: [TMPL-01, TMPL-02, TMPL-03, TMPL-04, TMPL-05, TMPL-06, TMPL-07, TMPL-08, TMPL-09]
coverage:
  - id: D1
    description: "Sentinel-wrapped org block prepends exactly once above PR-Agent default body."
    requirement: TMPL-01
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_prepend.py#test_prepend_org_template_replaces_existing_block_and_preserves_checkboxes"
        status: pass
    human_judgment: false
  - id: D2
    description: "Checklist ticks are preserved across rerenders while old What/Risk content is replaced."
    requirement: TMPL-02
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_prepend.py#test_render_org_template_block_preserves_matching_checkbox_states"
        status: pass
    human_judgment: false
  - id: D3
    description: "10 block-scalar YAML fixtures containing what_why and note_risk parse through load_yaml with extended keys_fix."
    requirement: TMPL-03
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_prepend.py#test_yaml_block_scalar_keys_parse_with_org_template_keys"
        status: pass
    human_judgment: false
  - id: D4
    description: "Marker mode skips org-template prepend."
    requirement: TMPL-08
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_prepend.py#test_marker_mode_skips_prepend"
        status: pass
    human_judgment: false
duration: 35min
completed: 2026-07-03
status: complete
---

# Phase 03 Plan 01: Org Template Prepend Summary

**Org template prepend is gated, idempotent, and checkbox-preserving**

## Performance

- **Duration:** 35 min
- **Completed:** 2026-07-03
- **Tasks:** 2
- **Files modified:** 3 implementation/test files plus plan and summary artifacts

## Accomplishments

- Added org-template sentinels and render helpers in `pr_agent/tools/pr_description.py`.
- Extended `keys_fix` and effective `extra_instructions` with `what_why` and `note_risk` only when `enable_org_template` is active and marker mode is off.
- Consumed `what_why` / `note_risk` before `_prepare_pr_answer()` so they do not appear as default PR-Agent sections.
- Prepended the org block above the generated body, removed any stale sentinel block from the generated body, and preserved matching checklist states from the existing MR description.
- Closed review blockers by stripping the org sentinel block before provider user-description detection and by sanitizing AI-forged sentinel comments.
- Updated the Phase 1 source audit now that Phase 3 intentionally calls `load_org_template()` behind the org-template gate.

## Task Commits

1. **Task 1: Phase 3 plan artifact** - `0f24a371` (docs)
2. **Task 2: Org template implementation and tests** - `abd51ac2` (feat)
3. **Task 3: Code-review blocker fixes** - `6e04c3ef` (fix)

## Files Created/Modified

- `pr_agent/tools/pr_description.py` - Org-template prompt steering, key stashing, sentinel rendering, sentinel sanitization, checkbox merge, and publish-path prepend.
- `pr_agent/git_providers/git_provider.py` - Strip org sentinel block before generated-description detection so reruns do not treat generated content as user-authored.
- `tests/unittest/test_org_template_prepend.py` - Unit coverage for sentinels, idempotency, checkbox preservation, marker skip, and YAML key parsing.
- `tests/unittest/test_describe_byte_identical_when_off.py` - Updated source guard to require the now-active loader call to stay org-template gated.

## Decisions Made

- Preserved checklist state by matching checklist labels, not by copying arbitrary old block content.
- Reused `load_org_template()` for missing-template graceful fallback.
- Kept `pr_agent/settings/pr_description_prompts.toml` untouched; prompt steering remains runtime-only.

## Deviations from Plan

- No separate AI-SPEC or RESEARCH artifact was produced because Phase 3 context and prior phase patterns already specified the required seams, and the implementation was one narrow vertical slice.

## Issues Encountered

- `python -m ruff check ...` could not run because `ruff` is not installed in the active Python environment.
- `python -m flake8 ...` could not run because `flake8` is not installed.
- No `.venv/Scripts/python.exe` exists in this checkout, so there was no local virtualenv fallback for lint tools.

## Verification

- `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py -q` -> 16 passed.
- `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py tests/unittest/test_pr_description_output_core.py tests/unittest/test_describe_byte_identical_when_off.py -q` -> 47 passed.
- `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py tests/unittest/test_conventional_title_publish_seam.py tests/unittest/test_normalize_angular_title.py tests/unittest/test_pr_description_output_core.py tests/unittest/test_describe_byte_identical_when_off.py -q` -> 90 passed.
- After review fixes: `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py -q` -> 18 passed.
- After review fixes: `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py tests/unittest/test_conventional_title_publish_seam.py tests/unittest/test_normalize_angular_title.py tests/unittest/test_pr_description_output_core.py tests/unittest/test_describe_byte_identical_when_off.py -q` -> 92 passed.
- Provider-adjacent check: `PYTHONPATH=. python -m pytest tests/unittest/test_org_template_prepend.py tests/unittest/test_git_provider_utils.py tests/unittest/test_ticket_extraction_async.py -q` -> 44 passed.
- `python -m py_compile pr_agent/tools/pr_description.py tests/unittest/test_org_template_prepend.py tests/unittest/test_describe_byte_identical_when_off.py` -> passed.
- After provider fix: `python -m py_compile pr_agent/tools/pr_description.py pr_agent/git_providers/git_provider.py tests/unittest/test_org_template_prepend.py` -> passed.

## Known Stubs

None.

## User Setup Required

None.

## Next Phase Readiness

All roadmap phases now have plan summaries. Milestone lifecycle can proceed after review, verification, and audit gates.

## Self-Check: PASSED

- Confirmed related describe/title regression suite passes.
- Confirmed no edits to `pr_agent/settings/pr_description_prompts.toml`.
- Left existing untracked `.codegraph/` and `bash.exe.stackdump` untouched.

---
*Phase: 03-org-template-prepend-with-idempotency*
*Completed: 2026-07-03*
