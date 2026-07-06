---
phase: 04-expose-v1-0-describe-toggles-via-config-env-vars-and-embed-p
plan: 01
subsystem: infra
tags: [dynaconf, config, env-vars, describe]

requires:
  - phase: 01-config-skeleton-and-fork-safe-seam
    provides: "[pr_description] fork toggles + dynaconf env_loader wiring"
provides:
  - "_fork_toggle dual-read helper (config.* first, pr_description.* fallback)"
  - "load-time mirror so CONFIG__USE_DESCRIPTION_MARKERS reaches upstream read sites"
  - "enable_pr_agent_output toggle key (default false)"
affects: [04-02, describe, org-template]

tech-stack:
  added: []
  patterns: [dual-read config helper, load-time config mirror]

key-files:
  created: [tests/unittest/test_config_env_override.py]
  modified: [pr_agent/tools/pr_description.py, pr_agent/config_loader.py, pr_agent/settings/configuration.toml, tests/unittest/test_org_template_prepend.py]

key-decisions:
  - "Keys stay defined in [pr_description]; config.* is an override channel only, accessed via dual-read."
  - "use_description_markers uses a load-time mirror (not a read-shim) so upstream code at pr_description.py:358/:453 sees CONFIG__ overrides with zero upstream edits."

patterns-established:
  - "Dual-read helper for fork toggles that must accept both CONFIG__* and PR_DESCRIPTION__* env styles."
  - "Load-time mirror for upstream-read keys that cannot be reached by a fork read-shim."

requirements-completed: []

coverage:
  - id: D1
    description: "CONFIG__* env vars flip all four fork describe toggles via dual-read"
    verification:
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_config_env_flips_enable_org_template"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_config_env_flips_enable_conventional_title"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_config_env_sets_enable_pr_agent_output_false"
        status: pass
    human_judgment: false
  - id: D2
    description: "PR_DESCRIPTION__* env vars still flip toggles (backward compatible)"
    verification:
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_pr_description_env_still_flips_toggle"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_org_toggles_env_override.py"
        status: pass
    human_judgment: false
  - id: D3
    description: "config.* precedence over pr_description.* when both set"
    verification:
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_config_env_wins_over_pr_description"
        status: pass
    human_judgment: false
  - id: D4
    description: "CONFIG__USE_DESCRIPTION_MARKERS mirrored into pr_description for upstream reads"
    verification:
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_mirror_copies_config_use_description_markers"
        status: pass
      - kind: unit
        ref: "tests/unittest/test_config_env_override.py#test_mirror_noop_when_config_unset"
        status: pass
    human_judgment: false
  - id: D5
    description: "enable_org_template=false stays byte-identical regardless of enable_pr_agent_output"
    verification:
      - kind: unit
        ref: "tests/unittest/test_describe_byte_identical_when_off.py"
        status: pass
    human_judgment: false

duration: 35min
completed: 2026-07-06
status: complete
---

# Phase 4 Plan 01: config__* env access for describe toggles

**Dual-read _fork_toggle helper + load-time mirror let operators use CONFIG__* legacy env vars for all fork describe toggles without breaking PR_DESCRIPTION__* or editing upstream code.**

## Performance

- **Duration:** ~35 min
- **Tasks:** 5
- **Files modified:** 5

## Accomplishments
- Added `_fork_toggle(key, default, settings=None)` reading `config.*` first, `pr_description.*` fallback; routed all three fork toggle reads through it.
- Added `_mirror_fork_config_keys()` in config_loader.py, invoked after the pyproject load, mirroring an explicitly-set `config.use_description_markers` into `pr_description.use_description_markers`.
- Added `enable_pr_agent_output=false` to `[pr_description]` fork block.
- Fresh-Dynaconf test suite `test_config_env_override.py` covering CONFIG__* dual-read, backward compat, precedence, and the mirror.

## Task Commits

1. **Task 1-5 (single feat commit)** - `9f70cdc7` (feat) — TDD: tests + helper + mirror + config key + MagicMock landmine fix landed together.

## Files Created/Modified
- `pr_agent/tools/pr_description.py` - `_fork_toggle` helper + routed `_conventional_title_enabled`, `_org_template_enabled`, `_org_template_active`.
- `pr_agent/config_loader.py` - `_mirror_fork_config_keys()` load-time mirror.
- `pr_agent/settings/configuration.toml` - `enable_pr_agent_output=false` key.
- `tests/unittest/test_config_env_override.py` - new fresh-Dynaconf env tests.
- `tests/unittest/test_org_template_prepend.py` - MagicMock config.get landmine fix + `use_description_markers`/`enable_pr_agent_output` in `_settings()`.

## Decisions Made
- Helper accepts optional `settings=` param for unit-testability against a fresh Dynaconf (avoids mutating the module singleton).
- Mirror is scoped to `use_description_markers` only — the other fork toggles are fork-read and covered by `_fork_toggle`; only this upstream-read key needs the mirror.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - test fixture drift] `_settings()` helper needed use_description_markers in its .get dict**
- **Found during:** Task 5 (MagicMock fix)
- **Issue:** `_org_template_active` switched from attribute access (`pr_description.use_description_markers`) to `_fork_toggle` (`.get(key)`); the test's `.get` lambda didn't include the key, so the marker-skip test failed.
- **Fix:** Added `use_description_markers` (and later `enable_pr_agent_output`) to the `_settings()` `.get` lambda dict.
- **Verification:** `test_marker_mode_skips_prepend` green.
- **Committed in:** `9f70cdc7`

---

**Total deviations:** 1 auto-fixed (test fixture alignment)
**Impact on plan:** None — mechanical test-fixture update required by the read-path change.

## Issues Encountered
None beyond the fixture drift above.

## User Setup Required
None.

## Next Phase Readiness
- `enable_pr_agent_output` key is in place for Plan 02's suppression to consume via `_fork_toggle`.
- `_fork_toggle` is the single read seam Plan 02 extends.

---
*Phase: 04-expose-v1-0-describe-toggles-via-config-env-vars-and-embed-p*
*Completed: 2026-07-06*
