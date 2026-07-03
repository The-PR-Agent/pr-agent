---
phase: 01-config-skeleton-and-fork-safe-seam
reviewed: 2026-07-03T01:52:31Z
depth: deep
files_reviewed: 7
files_reviewed_list:
  - docs/fork/org-mr-enhancements.md
  - pr_agent/settings/configuration.toml
  - pr_agent/settings/org_template.md
  - pr_agent/tools/pr_description.py
  - tests/unittest/test_describe_byte_identical_when_off.py
  - tests/unittest/test_org_template_config.py
  - tests/unittest/test_org_toggles_env_override.py
findings:
  critical: 1
  warning: 1
  info: 2
  total: 4
status: issues_found
---

# Phase 01: Code Review Report

**Reviewed:** 2026-07-03T01:52:31Z
**Depth:** deep
**Files Reviewed:** 7
**Status:** issues_found

## Summary

Phase 1 is a deliberately inert fork seam: two `[pr_description]` toggles defaulting `false`, a fork-owned `org_template.md` asset, a module-level `load_org_template()` helper that is defined but not wired, plus docs and tests. The core invariant — byte-identical `describe` output when both toggles are off — holds. I traced `_prepare_pr_answer` against the pinned inputs in `test_describe_byte_identical_when_off.py` and the golden literals match exactly; the loader is genuinely uncalled (grep confirms one occurrence, the `def` line); the flag reads use the absent-safe `.get(flag, False)` form; and the path constant `Path(__file__).parent.parent / "settings" / "org_template.md"` resolves correctly to `pr_agent/settings/org_template.md`. No user input touches the path, so there is no traversal risk.

The significant defect is not in the Python logic but in packaging: the new `org_template.md` is the first non-`.toml` runtime asset under `pr_agent/settings/`, and the packaging configuration only bundles `*.toml`. In a standard pip-installed deployment the asset will be absent and the loader will raise `FileNotFoundError` — latent this phase because the loader is unwired, but it undermines the phase's deliverable (a loadable asset) and will surface the moment Phase 3 wires it in. A secondary concern is that the loader has no graceful-fallback error handling, which diverges from the project's documented error-handling convention.

## Critical Issues

### CR-01: org_template.md is not shipped as package data — loader will FileNotFoundError in pip-installed deployments

**File:** `pr_agent/tools/pr_description.py:38-49` (asset: `pr_agent/settings/org_template.md`; packaging: `MANIFEST.in:1`, `pyproject.toml:40-47`)

**Issue:** `load_org_template()` reads `pr_agent/settings/org_template.md` via a package-relative path. The only mechanism that declares package data for this project is `MANIFEST.in`, which contains a single positive rule:

```
recursive-include pr_agent *.toml
```

`pyproject.toml` sets `include-package-data = true` but defines no `[tool.setuptools.package-data]` section, so setuptools draws package data exclusively from `MANIFEST.in`. Every existing `settings/` asset is a `.toml` file, which is exactly why the `*.toml` glob has sufficed until now. `org_template.md` is a `.md` file and is not matched by any include rule, so it will not be placed in the built wheel/sdist under `site-packages/pr_agent/settings/`.

Consequently, in any deployment that imports `pr_agent` from an installed distribution (a plain `pip install`, the `cli_pip.py` path, or a PyPI install), `_ORG_TEMPLATE_PATH.read_text(...)` will raise `FileNotFoundError`.

This is currently masked in the Docker images only by accident: `docker/Dockerfile` does `pip install .` and then `ADD pr_agent pr_agent` with `ENV PYTHONPATH=/app`, so `pr_agent` is imported from the source tree copy (which contains the `.md`), not from `site-packages`. Any deployment path that does not source-mount the tree is broken.

The defect is latent this phase because the loader is intentionally unwired, so nothing triggers the read yet. But the phase's stated deliverable is a *loadable* fork-owned asset, and shipping the asset without packaging it means a Phase 3 developer who wires the loader will hit a `FileNotFoundError` that has nothing to do with their wiring code. The fix belongs in the commit that introduces the asset.

**Fix:** Explicitly include the asset in `MANIFEST.in` (narrow rule preferred over a broad `*.md` glob so docs/READMEs elsewhere are not swept in):

```
recursive-include pr_agent *.toml
include pr_agent/settings/org_template.md
recursive-exclude pr_agent *.secrets.toml
```

Alternatively, declare it via `pyproject.toml`:

```toml
[tool.setuptools.package-data]
"pr_agent.settings" = ["*.toml", "org_template.md"]
```

After the change, verify with a build: `python -m build` then inspect the wheel (`unzip -l dist/*.whl | grep org_template.md`) to confirm the asset ships.

## Warnings

### WR-01: load_org_template() has no error handling — diverges from the project's graceful-fallback convention

**File:** `pr_agent/tools/pr_description.py:41-49`

**Issue:** The loader is a bare `return _ORG_TEMPLATE_PATH.read_text(encoding="utf-8")`. `CLAUDE.md` documents the project error-handling convention as "Try/except with logging and graceful fallback (never crash silently)" and "Functions return empty string `""` ... on failure," and the fork constraints in `CLAUDE.md` explicitly call for "graceful fallback on error." As written, a missing or unreadable template (see CR-01, or a permissions/encoding error) raises an uncaught `FileNotFoundError`/`OSError`. When the loader is wired into the `describe` output path in Phase 3, an uncaught exception here would propagate into `run()` — which catches broadly at line 219-221 and returns `""`, silently producing no description rather than a degraded-but-useful one.

Note the tradeoff: returning `""` on failure would let Phase 3 prepend an empty template silently, which is arguably worse than a logged failure. The right shape is to log a warning and fall back, so the failure is observable without crashing:

**Fix:**
```python
def load_org_template() -> str:
    try:
        return _ORG_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        get_logger().warning(
            f"Could not load org template at {_ORG_TEMPLATE_PATH}: {e}"
        )
        return ""
```

If a loud failure is intentionally preferred for this asset, document that decision in the docstring so the divergence from the project convention is explicit. Either way, resolving CR-01 is the primary mitigation.

## Info

### IN-01: Source-audit guard is substring-based and can be bypassed by non-attribute flag access

**File:** `tests/unittest/test_describe_byte_identical_when_off.py:165-187`

**Issue:** `test_fork_flags_never_use_bare_attribute_access` enforces the fork-safe-seam contract by counting the substring `.pr_description.<flag>`. This catches bare dotted attribute access, but dynaconf also supports subscript (`get_settings().pr_description["enable_org_template"]`) and `getattr(...)` access, neither of which the substring check would flag — yet both would crash the same way if the key were absent from a downstream `.pr_agent.toml` override. This does not make the test flaky (it correctly guards the current code), but it can give false confidence in a future phase. Worth a comment noting the guard covers the dotted form only, so reviewers of Phase 2/3 know to watch for subscript/`getattr` reads too.

**Fix:** Add a brief comment documenting the guard's scope, or extend the check to also assert absence of `.pr_description["enable_...` and `getattr(get_settings().pr_description, "enable_...` patterns.

### IN-02: Section-name terminology drifts across template, docs, and project summary

**File:** `pr_agent/settings/org_template.md:4`; `docs/fork/org-mr-enhancements.md:18`

**Issue:** The template header is `## Note / Risk`, the docs table refers to the section as "Note-Risk," and the `CLAUDE.md` project summary describes it as "Risk filled by AI." These are close enough to be understood but not identical. Since Phase 3 will fill this section by AI, an exact-match assumption on the header string could cause a silent miss. Aligning the wording now (pick one canonical label) removes a small future footgun.

**Fix:** Standardize on the template's literal header (`Note / Risk`) across `docs/fork/org-mr-enhancements.md` and any Phase 3 logic that keys off the section name.

---

_Reviewed: 2026-07-03T01:52:31Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
