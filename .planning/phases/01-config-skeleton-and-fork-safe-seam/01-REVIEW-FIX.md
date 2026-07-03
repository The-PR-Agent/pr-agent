---
phase: 01-config-skeleton-and-fork-safe-seam
fixed_at: 2026-07-03T02:00:10Z
review_path: .planning/phases/01-config-skeleton-and-fork-safe-seam/01-REVIEW.md
iteration: 1
findings_in_scope: 2
fixed: 2
skipped: 0
status: all_fixed
---

# Phase 01: Code Review Fix Report

**Fixed at:** 2026-07-03T02:00:10Z
**Source review:** .planning/phases/01-config-skeleton-and-fork-safe-seam/01-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 2
- Fixed: 2
- Skipped: 0

## Fixed Issues

### CR-01: org_template.md is not shipped as package data — loader will FileNotFoundError in pip-installed deployments

**Files modified:** `MANIFEST.in`
**Commit:** eeb5fc08
**Applied fix:** Added a narrow `include pr_agent/settings/org_template.md` rule to `MANIFEST.in`, placed between the existing `recursive-include pr_agent *.toml` and `recursive-exclude pr_agent *.secrets.toml` lines. Chose the explicit single-file include over a broad `*.md` glob (as the review recommended) so unrelated docs/READMEs are not swept into the distribution. This ensures the fork-owned asset ships in the wheel/sdist under `site-packages/pr_agent/settings/`, so the loader will not hit `FileNotFoundError` in pip-installed deployments once Phase 3 wires it in.

### WR-01: load_org_template() has no error handling — diverges from the project's graceful-fallback convention

**Files modified:** `pr_agent/tools/pr_description.py`
**Commit:** fbea45d0
**Applied fix:** Wrapped the `_ORG_TEMPLATE_PATH.read_text(...)` call in a `try/except OSError` block that logs a warning via the project's `get_logger()` (already imported in the module) and returns `""` on failure, matching CLAUDE.md's graceful-fallback convention ("never crash silently, return empty string on failure"). Used `OSError` as the catch (superclass of both `FileNotFoundError` and permission/encoding read errors) per the review's suggested shape. Extended the docstring to document the fallback behavior so the failure mode is explicit. Syntax verified via `ast.parse`; the three affected test files pass (11 passed).

---

_Fixed: 2026-07-03T02:00:10Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
