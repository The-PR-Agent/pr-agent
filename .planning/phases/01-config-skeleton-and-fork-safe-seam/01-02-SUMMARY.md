---
phase: 01-config-skeleton-and-fork-safe-seam
plan: 02
subsystem: config
tags: [config, env-override, dynaconf, docs, cfg-06]
status: complete
depends_on: [01-01]
requires:
  - "01-01: [pr_description] toggles enable_conventional_title / enable_org_template exist in configuration.toml"
  - "config_loader.py:12-18 dynaconf env_loader with envvar_prefix=False, load_dotenv=False, merge_enabled=True"
provides:
  - "Executable spec that env vars PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE and PR_DESCRIPTION__ENABLE_ORG_TEMPLATE flip the toggles"
  - "Fork-owned docs referenced by the configuration.toml fork comment"
affects: []
tech_stack:
  added: []
  patterns:
    - "Fresh-Dynaconf pattern for env-loader tests (mirrors config_loader.dynconf_kwargs to bind env at construction time)"
    - "SECTION__KEY double-underscore env-var mapping (no prefix)"
key_files:
  created:
    - tests/unittest/test_org_toggles_env_override.py
    - docs/fork/org-mr-enhancements.md
  modified: []
decisions:
  - "Env override rides the existing dynaconf env_loader — zero new plumbing (CFG-06 is verify-and-document only)"
  - "Test builds a fresh Dynaconf mirroring config_loader.dynconf_kwargs because the module-level singleton is bound to env at import time and cannot be re-bound"
  - "Boolean-literal coercion only: 'true'/'True' flip toggles to `is True`. Integer literals like '1' are coerced by dynaconf to int, which is truthy but not `is True` — documented in both the test and the docs page so operators do not rely on it"
metrics:
  duration_minutes: 6
  completed_date: "2026-07-03"
requirements: [CFG-06]
---

# Phase 01 Plan 02: Env-Var Override Verification and Fork Docs Summary

Prove and document that PR-Agent's two org-MR toggles (`enable_conventional_title`, `enable_org_template`) can be flipped from the environment via PR-Agent's existing dynaconf env_loader, adding no new plumbing. Add executable proof (a test) and fork-owned reference docs.

## What Shipped

### Task 1 — Env-override test (`tests/unittest/test_org_toggles_env_override.py`)

Five tests, all green, that construct a **fresh** `Dynaconf` mirroring `config_loader.dynconf_kwargs` (`core_loaders=[]`, `loaders=['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader']`, `merge_enabled=True`, `envvar_prefix=False`, `load_dotenv=False`) pointed at the real `pr_agent/settings/configuration.toml`:

- `test_defaults_are_false_without_env_vars` — control assertion: both toggles read `False` when no env var is set.
- `test_env_var_flips_enable_conventional_title` — `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` flips `pr_description.enable_conventional_title` to `True`. Sibling toggle stays `False`.
- `test_env_var_flips_enable_org_template` — `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true` flips `pr_description.enable_org_template` to `True`. Sibling toggle stays `False`.
- `test_env_var_case_variants_flip_toggle` — parametrized across both env vars and the case variants `"true"` and `"True"`; asserts both variants coerce to `is True`.

The isolation landmine from `01-RESEARCH.md` section 7 is avoided by NOT using the module-level `global_settings` singleton (dynaconf binds env at `Dynaconf(...)` construction; the singleton is built when `pr_agent.config_loader` is first imported and cannot pick up a late `monkeypatch.setenv`).

### Task 2 — Fork docs (`docs/fork/org-mr-enhancements.md`)

Concise reference page (77 lines) that documents:

1. Both `[pr_description]` toggles (`enable_conventional_title`, `enable_org_template`), both defaulting to `false`, and the phases in which each ships.
2. The two env vars — `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` and `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true` — with a concrete GitLab CI invocation example.
3. The three dynaconf configuration facts that make the override work with no new plumbing (`envvar_prefix=False`, `load_dotenv=False`, `SECTION__KEY` convention).
4. Explicit note that env vars must be set in the real process environment (not a `.env` file) because `load_dotenv=False`.
5. The boolean-literal coercion contract (`true`/`True` preferred over `1`) and why the `.get(key, False)` call sites in code still tolerate truthy ints.
6. Reference to the env-override test as the executable spec.

This is the exact fork-owned docs path that the `configuration.toml` fork comment from Plan 01-01 (line 129, `# --- fork: org MR enhancements (default off; see fork README) ---`) points readers at.

## Verification Run

```
$ python -m pytest tests/unittest/test_org_toggles_env_override.py -x -q
.....                                                                    [100%]
5 passed in 0.46s
```

```
$ python -c "import pathlib; t=pathlib.Path('docs/fork/org-mr-enhancements.md').read_text(encoding='utf-8'); \
    assert 'PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE' in t; \
    assert 'PR_DESCRIPTION__ENABLE_ORG_TEMPLATE' in t; \
    assert 'false' in t.lower(); print('ok')"
ok
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fresh-Dynaconf parametrized test failed on `"1"` variant**

- **Found during:** Task 1 verification (first `pytest` run)
- **Issue:** The parametrized case-variant test originally asserted that `"true"`, `"True"`, and `"1"` all coerce to `is True`. Dynaconf coerces `"1"` to the Python `int` `1`, which is truthy but not `is True`, so the assertion `getattr(settings.pr_description, attr_name) is True` failed with `AssertionError: env var ... must flip enable_conventional_title to True` and `assert 1 is True`.
- **Fix:** Dropped the `"1"` variant from the parametrized values (kept `"true"` and `"True"`) and added an inline comment documenting that dynaconf coerces integer-literal strings to `int`, not `bool`. The plan's acceptance criteria only require the two literal env vars flip their toggle to `True`; both boolean-literal cases satisfy that. Also mirrored this contract in the fork docs so operators know to prefer boolean literals.
- **Files modified:** `tests/unittest/test_org_toggles_env_override.py` (renamed test to `test_env_var_case_variants_flip_toggle`, added docstring explaining the exclusion).
- **Commit:** `cddb785b` (test contained the fix pre-commit; the failing run was during authoring)

No other deviations. Both tasks executed as specified in the plan.

## Commits

| Task | Commit | Message |
| ---- | ------ | ------- |
| 1    | `cddb785b` | test(01-02): prove env-var override for org MR toggles |
| 2    | `6bd89120` | docs(01-02): document org MR toggles and env-var override |

## Files Created

- `tests/unittest/test_org_toggles_env_override.py` (117 lines)
- `docs/fork/org-mr-enhancements.md` (77 lines)

## Files Modified

None. Plan 01-02 is verify-and-document only — no changes to `pr_agent/` source or `configuration.toml`.

## Requirement Coverage

- **CFG-06** — Both toggles proven env-overridable through the existing dynaconf `env_loader`, documented in fork-owned docs. Satisfied.

## Known Stubs

None. Everything shipped is executable spec + reference docs; no code stubs introduced.

## Threat Flags

None. The env-override path is pre-existing dynaconf plumbing; no new trust boundaries. The plan's `<threat_model>` (T-01-04/T-01-05/T-01-06) is fully addressed:

- **T-01-04** (env-var privilege): both toggles ship `false` and gate feature bodies that are inert until Phases 2/3; setting env vars already requires CI environment control.
- **T-01-05** (test env isolation): `monkeypatch.setenv/delenv` restores env state after each test; the fresh-Dynaconf pattern prevents singleton contamination.
- **T-01-06** (docs disclosure): docs describe public config keys only; no secrets exposed.

## Handoff Notes for Plan 01-03

- The env-override test file is a good pattern reference for any future plan that needs to exercise dynaconf's env_loader without mutating the singleton.
- The fork docs page is where any subsequent phase documenting new fork-owned toggles or env vars should land — keep the single-page reference to avoid docs sprawl.

## Self-Check: PASSED

All required files exist and both per-task commits are present in the branch history:

- `tests/unittest/test_org_toggles_env_override.py` (FOUND)
- `docs/fork/org-mr-enhancements.md` (FOUND)
- `.planning/phases/01-config-skeleton-and-fork-safe-seam/01-02-SUMMARY.md` (FOUND)
- Commit `cddb785b` (FOUND)
- Commit `6bd89120` (FOUND)
