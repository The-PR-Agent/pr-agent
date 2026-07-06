---
phase: 04-expose-v1-0-describe-toggles-via-config-env-vars-and-embed-p
plan: 02
subsystem: ui
tags: [describe, org-template, walkthrough, mermaid, gitlab]

requires:
  - phase: 03-org-template-prepend-with-idempotency
    provides: "_prepend_org_template, _render_org_template_block, _stash_org_template_fields, sentinel block"
  - phase: 04-01
    provides: "_fork_toggle, enable_pr_agent_output key"
provides:
  - "'## Changes' section in org template with walkthrough + mermaid diagram"
  - "enable_pr_agent_output suppression of PR-Agent's default body when org template active"
affects: [describe, org-template]

tech-stack:
  added: []
  patterns: [marker-fill in sentinel block, gated body suppression]

key-files:
  created: [tests/unittest/test_org_template_changes_embed.py]
  modified: [pr_agent/tools/pr_description.py, pr_agent/settings/org_template.md, tests/unittest/test_org_template_prepend.py, docs/fork/org-mr-enhancements.md]

key-decisions:
  - "Walkthrough embedded as the RAW table (process_pr_files_prediction), not the wrapped changes_walkthrough — the wrapper carries the forbidden 'File Walkthrough' literal (Phase 3 SC#5)."
  - "Suppression lives inside _prepend_org_template's active branch, after the byte-identical-when-off early return — unreachable when org template is off."
  - "Copy semantics (user-accepted): walkthrough renders in the template; with enable_pr_agent_output=true it ALSO renders in the default body (double render)."

patterns-established:
  - "Fork fills pr_agent:walkthrough / pr_agent:diagram markers in the org block from values captured in _stash_org_template_fields."

requirements-completed: []

coverage:
  - id: D1
    description: "Org block renders a '## Changes' section with the walkthrough table + mermaid diagram"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_render_fills_walkthrough_and_diagram"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_render_has_no_unfilled_markers"
        status: pass
    human_judgment: false
  - id: D2
    description: "Org block contains no 'File Walkthrough'/'Diagram Walkthrough' literals (Phase 3 SC#5)"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_render_has_no_forbidden_literals"
        status: pass
    human_judgment: false
  - id: D3
    description: "enable_pr_agent_output=false suppresses the default PR-Agent body when org template active"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_suppression_on_returns_only_block"
        status: pass
    human_judgment: false
  - id: D4
    description: "enable_pr_agent_output=true renders the default body below the org block"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_suppression_off_renders_default_body"
        status: pass
    human_judgment: false
  - id: D5
    description: "enable_org_template=false is byte-identical regardless of enable_pr_agent_output"
    verification:
      - kind: unit
        ref: "tests/unittest/test_org_template_changes_embed.py#test_byte_identical_when_org_template_off"
        status: pass
    human_judgment: false
  - id: D6
    description: "Live GitLab MR renders single org body with ## Changes (walkthrough + mermaid)"
    verification: []
    human_judgment: true
    rationale: "Requires a real GitLab MR + LLM describe run; not reproducible in unit tests. Manual per docs/fork/org-mr-enhancements.md."

duration: 30min
completed: 2026-07-06
status: complete
---

# Phase 4 Plan 02: embed walkthrough+diagram in org template + enable_pr_agent_output

**Org template now renders the file walkthrough + mermaid diagram in a '## Changes' section, and enable_pr_agent_output (default false) suppresses PR-Agent's own default body when the org template is active.**

## Performance

- **Duration:** ~30 min
- **Tasks:** 5
- **Files modified:** 5

## Accomplishments
- `_render_org_template_block` gains `walkthrough`/`diagram` params and emits a `## Changes` section between Note/Risk and Checklist.
- `_stash_org_template_fields` captures the RAW walkthrough table (via `process_pr_files_prediction("")`) + `changes_diagram`, with graceful "" fallback on failure.
- `_prepend_org_template` suppresses the default PR-Agent body when `_fork_toggle("enable_pr_agent_output", False)` is false — unreachable on the off path.
- `org_template.md` gains the `## Changes` section with `pr_agent:walkthrough` / `pr_agent:diagram` markers (documentation parity).
- New test suite `test_org_template_changes_embed.py` covering marker fill, no-forbidden-literals, suppression on/off, and byte-identical-when-off.
- `docs/fork/org-mr-enhancements.md` documents `CONFIG__*` env vars and `enable_pr_agent_output`.

## Task Commits

1. **Tasks 1-5 (single feat commit)** - `d763684e` (feat) — embed + suppression + template + tests + docs landed together.

## Files Created/Modified
- `pr_agent/tools/pr_description.py` - `_render_org_template_block` (walkthrough/diagram params + `## Changes`), `_stash_org_template_fields` (capture walkthrough/diagram), `_prepend_org_template` (suppression).
- `pr_agent/settings/org_template.md` - `## Changes` section with markers.
- `tests/unittest/test_org_template_changes_embed.py` - new embed + suppression tests.
- `tests/unittest/test_org_template_prepend.py` - existing tests updated for the new `## Changes` section + `enable_pr_agent_output` toggle.
- `docs/fork/org-mr-enhancements.md` - `CONFIG__*` table + `enable_pr_agent_output` behavior.

## Decisions Made
- Kept the hardcoded block-render pattern from Phase 3 (consistent with how What/Risk/Checklist are rendered) rather than switching to template-body parsing — minimal diff, same smell as existing code. `org_template.md` mirrors the structure for documentation parity.
- Used the raw walkthrough table, never the wrapped `changes_walkthrough` (would reintroduce the forbidden "File Walkthrough" literal — Phase 3 SC#5).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - test alignment] Existing prepend tests needed enable_pr_agent_output + new fields**
- **Found during:** Task 1 (new tests) + Task 4 (suppression)
- **Issue:** (a) `_render_org_template_block` now emits `## Changes`, shifting the structure asserted by `test_prepend_org_template_replaces_existing_block_and_preserves_checkboxes`. (b) Suppression default-false drops the default body that test asserts. (c) `test_stash_org_template_fields_consumes_ai_keys` didn't expect the new `walkthrough`/`diagram` keys.
- **Fix:** Updated `_settings()` to include `enable_pr_agent_output` (default false); the body-rendering test passes `enable_pr_agent_output=True` to exercise the non-suppression path; the stash test now asserts the two new keys.
- **Verification:** `test_org_template_prepend.py` all green.
- **Committed in:** `d763684e`

**2. [Rule 2 - test setup] New embed tests needed get_pr_description mock**
- **Found during:** Task 1 (new tests)
- **Issue:** `_make_instance` used a bare `PRDescription.__new__` without mocking `git_provider.get_pr_description`; the checkbox-preservation fetch returned a MagicMock that broke `_ORG_TEMPLATE_RE.search`.
- **Fix:** Set `obj.git_provider.get_pr_description.return_value = ""` (mirroring the existing prepend test's `_make_instance`).
- **Verification:** `test_org_template_changes_embed.py` all green.
- **Committed in:** `d763684e`

---

**Total deviations:** 2 auto-fixed (both test alignment)
**Impact on plan:** None — mechanical test updates required by the new behavior.

## Issues Encountered
None beyond the test alignment above.

## User Setup Required
None.

## Next Phase Readiness
- Phase 4 complete: both config-layer (Plan 01) and embed/suppression (Plan 02) shipped.
- Ready for verification (must_haves all covered by passing unit tests; one manual GitLab MR check flagged for human judgment).

---
*Phase: 04-expose-v1-0-describe-toggles-via-config-env-vars-and-embed-p*
*Completed: 2026-07-06*
