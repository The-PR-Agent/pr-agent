---
phase: 01-config-skeleton-and-fork-safe-seam
reviewed: 2026-07-03T02:03:55Z
depth: deep
files_reviewed: 8
files_reviewed_list:
  - MANIFEST.in
  - docs/fork/org-mr-enhancements.md
  - pr_agent/settings/configuration.toml
  - pr_agent/settings/org_template.md
  - pr_agent/tools/pr_description.py
  - tests/unittest/test_describe_byte_identical_when_off.py
  - tests/unittest/test_org_template_config.py
  - tests/unittest/test_org_toggles_env_override.py
findings:
  critical: 0
  warning: 0
  info: 3
  total: 3
status: clean
---

# Phase 01: Code Review Report

**Reviewed:** 2026-07-03T02:03:55Z
**Depth:** deep
**Files Reviewed:** 8
**Status:** clean

## Summary

Iteration 2 re-review of the Phase 1 fork seam. The two BLOCKER/WARNING findings from iteration 1 are both resolved, and the resolutions are correct and complete:

- **CR-01 (BLOCKER) — RESOLVED.** `MANIFEST.in:2` now carries `include pr_agent/settings/org_template.md`, a narrow explicit include (preferred over a broad `*.md` glob). It sits after the `recursive-include pr_agent *.toml` rule and before `recursive-exclude pr_agent *.secrets.toml`; the exclude only matches `*.secrets.toml`, so it does not negate the new include. `org_template.md` will now be placed under `site-packages/pr_agent/settings/` in a built wheel/sdist, so a Phase 3 wiring of the loader will no longer hit `FileNotFoundError` in pip-installed deployments. (Not build-verified in this review — no `python -m build` was run — but the packaging rule is correct by inspection.)

- **WR-01 (WARNING) — RESOLVED.** `load_org_template()` in `pr_agent/tools/pr_description.py:41-57` now wraps the read in `try/except OSError`, logs a warning via `get_logger().warning(...)`, and returns `""` on failure. This matches the project's documented graceful-fallback convention and the exact fix shape recommended in iteration 1. `FileNotFoundError`, `PermissionError`, and `IsADirectoryError` are all `OSError` subclasses, so the realistic missing/unreadable cases are caught. The docstring was updated to document the fallback. The logged path is a static package-relative constant with no user input or secret content, so logging it is safe.

The core Phase 1 invariant still holds. `load_org_template(` appears exactly once (the `def` line, `pr_description.py:41`) — the loader remains inert and unwired. The two toggles read via the absent-safe `.get(<flag>, False)` form, both default `false` in `configuration.toml:130-131` under `[pr_description]`, and the byte-identical golden test's pinned inputs still map to the expected `(title, body)` literals. No fork behavior leaks into the toggles-off path. The env-override test still faithfully mirrors `config_loader.dynconf_kwargs` with a fresh `Dynaconf`.

No Critical or Warning findings remain. Three Info items persist or are newly noted; none block the phase. Status is clean.

## Info

### IN-01: Source-audit guard is substring-based and covers only dotted attribute access

**File:** `tests/unittest/test_describe_byte_identical_when_off.py:165-187`

**Issue:** Carried over from iteration 1, still open. `test_fork_flags_never_use_bare_attribute_access` counts the substring `.pr_description.<flag>` to enforce that fork flags are never read via bare attribute access. This catches the dotted form but not subscript access (`get_settings().pr_description["enable_org_template"]`) or `getattr(get_settings().pr_description, "enable_org_template")`, both of which would crash the same way on a missing key in a downstream `.pr_agent.toml` override. The guard is correct for the current code (which uses neither), so this is not a false negative today — but it can give false confidence when Phase 2/3 adds real flag reads. The docstring at lines 166-171 documents only the dotted form.

**Fix:** Add a one-line comment noting the guard covers the dotted form only, or extend the check to also assert absence of `.pr_description["enable_` and `getattr(get_settings().pr_description, "enable_` patterns.

### IN-02: Section-name terminology drifts across template, docs, and project summary

**File:** `pr_agent/settings/org_template.md:4`; `docs/fork/org-mr-enhancements.md:18`

**Issue:** Carried over from iteration 1, still open. The template header is `## Note / Risk` (`org_template.md:4`), the docs table calls it "Note-Risk" (`org-mr-enhancements.md:18`), and the `CLAUDE.md` project summary calls it "Risk filled by AI". Three near-but-not-identical labels for the same section. Since Phase 3 will fill this section by AI, any exact-match assumption on the header string risks a silent miss.

**Fix:** Standardize on the template's literal header (`Note / Risk`) across `docs/fork/org-mr-enhancements.md` and any Phase 3 logic that keys off the section name.

### IN-03: load_org_template() does not catch UnicodeDecodeError (encoding failures escape the fallback)

**File:** `pr_agent/tools/pr_description.py:53-57`

**Issue:** New note on the WR-01 fix. `read_text(encoding="utf-8")` raises `UnicodeDecodeError` on a file with invalid UTF-8 bytes. `UnicodeDecodeError` subclasses `ValueError`, not `OSError`, so it is not caught by the `except OSError` clause — it would propagate uncaught. The docstring claims the loader falls back "on a missing or unreadable template," and a malformed-encoding file is arguably "unreadable," so there is a small docstring/behavior mismatch. Likelihood is very low: the asset is fork-owned and committed as valid UTF-8, and the loader is unwired this phase, so nothing triggers a read. This is why it is Info, not Warning.

**Fix:** If encoding robustness is desired, broaden the clause to `except (OSError, ValueError) as e:` (or `except (OSError, UnicodeError)`). Otherwise leave as-is and optionally tighten the docstring to say "missing or inaccessible" rather than "unreadable" so the scope of the fallback is accurate.

---

_Reviewed: 2026-07-03T02:03:55Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
