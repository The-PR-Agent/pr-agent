# Phase 1: Config skeleton and fork-safe seam - Research

**Researched:** 2026-07-02
**Method:** Direct codebase grounding (subagent hit Windows stdio hang; research hook is `onError: skip`, non-blocking — orchestrator gathered facts directly from real files).

All findings are grounded in real file paths + line numbers in this repo.

---

## 1. Config toggle mechanics (CFG-01, CFG-02)

### Where the keys go
`pr_agent/settings/configuration.toml` — the `[pr_description]` section starts at **line 103** (`[pr_description] # /describe #`) and runs through line 128 (`async_ai_calls=true`), ending before `[pr_questions]` at line 129.

Existing keys use bare `key=value` TOML (no quotes for bools):
```toml
generate_ai_title=false      # line 106 — the closest existing analog
enable_pr_type=true          # line 109
use_description_markers=false # line 123
```

**Add the two new toggles here**, grouped with a fork comment so upstream rebases show a clean localized diff. Suggested placement: after `async_ai_calls=true` (line 128), before the section ends:
```toml
# --- fork: org MR enhancements (default off; see fork README) ---
enable_conventional_title=false
enable_org_template=false
```

### How toggles are read
Two read patterns coexist in `pr_description.py`:
- **Attribute access** (crashes if key absent from TOML): `get_settings().pr_description.enable_semantic_files_types` (line 51), `get_settings().pr_description.generate_ai_title` (lines 185, 505, 569).
- **`.get(key, default)`** (absent-safe): `get_settings().pr_description.get("collapsible_file_list_threshold", 8)` (line 61), `get_settings().pr_description.get("enable_pr_diagram", False)` (line 62).

**Success criterion #1 explicitly requires `.get(key, False)`** — the absent-safe form. Use `get_settings().pr_description.get("enable_conventional_title", False)` and `get_settings().pr_description.get("enable_org_template", False)` everywhere. This means the code works even if the TOML keys were somehow missing, and keeps fork seams uniform (criterion #4).

---

## 2. Env override (CFG-06) — already wired, verify-and-document only

`pr_agent/config_loader.py:12-18` builds the Dynaconf singleton:
```python
dynconf_kwargs = {'core_loaders': [],
                  'loaders': ['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader'],
                  'root_path': join(current_dir, "settings"),
                  'merge_enabled': True}
global_settings = Dynaconf(
    envvar_prefix=False,
    load_dotenv=False,  # Security: Don't load .env files
    settings_files=[...])
```

- `envvar_prefix=False` + `dynaconf.loaders.env_loader` in the loader list means env vars map to settings via the `SECTION__KEY` double-underscore convention with **no prefix**. So `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` sets `pr_description.enable_conventional_title`.
- `load_dotenv=False` — env vars must be set in the real process environment (CI invocation), not a `.env` file. This is exactly the per-CI-invocation use case CFG-06 targets.

**No new plumbing required.** Phase work for CFG-06 is: (a) confirm with a test that the env var flips the toggle, (b) document the two env vars in the fork README / config notes.

**Testing note:** dynaconf reads env at `Dynaconf(...)` construction time (import of `config_loader`). A test must set `os.environ` and force a settings reload, OR construct a fresh `Dynaconf` with the same kwargs, OR use `get_settings().set(...)` won't test the env path. Cleanest: a small test that sets the env var, builds a `Dynaconf` with the same kwargs pointing at `configuration.toml`, and asserts `.pr_description.enable_conventional_title is True`. See existing `tests/unittest/test_config_loader_secrets.py` and `tests/unittest/test_extra_config_url.py` for config-construction test patterns.

---

## 3. Fork-owned template file (CFG-03)

Success criterion #2: template body lives in a fork-owned file, loaded from Python, NOT inlined into `pr_description_prompts.toml`.

- Settings dir is resolved as `current_dir = dirname(abspath(__file__))` then `join(current_dir, "settings")` in `config_loader.py:10,14`. The tool file `pr_agent/tools/pr_description.py` can resolve the settings dir the same way relative to the package.
- **Recommended location:** `pr_agent/settings/org_template.md` (matches criterion #2's own example).
- **Loading pattern:** a small module-level helper in `pr_description.py` (or a tiny fork-owned module) that reads the file with an absolute path derived from `__file__`:
  ```python
  from pathlib import Path
  _ORG_TEMPLATE_PATH = Path(__file__).parent.parent / "settings" / "org_template.md"
  ```
  Note: `pr_description.py` is in `pr_agent/tools/`, so `.parent.parent` → `pr_agent/`, then `/ "settings" / "org_template.md"`. Verify the relative depth when implementing.
- Phase 1 only needs the file to **exist** and be **loadable**. It is not consumed yet (that's Phase 3). A guard-only load is fine — do not wire it into output this phase (criterion #3 requires byte-identical-when-off).

There is **no existing generic file loader** for markdown assets in the settings dir — prompt TOMLs are loaded by dynaconf, not read as raw files. A direct `Path(...).read_text(encoding="utf-8")` is the minimal, idiomatic approach. Do not introduce a new abstraction (CLAUDE.md constraint).

---

## 4. Fork seam placement & guarding (CFG-05, criterion #4)

### The describe assembly + publish path
`pr_agent/tools/pr_description.py`:
- **Line 126-128** — branch on markers:
  ```python
  if get_settings().pr_description.use_description_markers and 'pr_files' in self.data:
      pr_title, pr_body, changes_walkthrough, pr_file_changes = self._prepare_pr_answer_with_markers()
  else:
      pr_title, pr_body, changes_walkthrough, pr_file_changes = self._prepare_pr_answer()
  ```
  (This is the TMPL-08 branch — org template must skip/WARN when `use_description_markers` is active. Not this phase, but the seam is here.)
- **`_prepare_pr_answer()`** — line 551. Builds `pr_body` from `self.data`. `ai_title` popped at line 568; title chosen at 569-574 based on `generate_ai_title`.
- **`_prepare_pr_answer_with_markers()`** — line 500. Same `ai_title`/title logic at 504-510.
- **Publish** — line 172-186:
  ```python
  title_to_publish = pr_title.strip() if get_settings().pr_description.generate_ai_title else None
  self.git_provider.publish_description(title_to_publish, pr_body)
  ```
  This is where Phase 2's title-forcing (CFG-04) and Phase 3's body-prepend will hook.

### Guarding rule
Every fork-added line must sit behind `if get_settings().pr_description.get("<flag>", False):`. For Phase 1, the goal is that **no behavior changes** — so any seam added now must be inert (the guard is false by default and the branch body is a no-op or absent). The cleanest Phase-1 posture:
- Add the two TOML keys.
- Add the template file + a loader helper (unused this phase, or used only inside a `False`-guarded branch).
- Optionally add the guard skeletons in `_prepare_pr_answer` / publish path, each wrapping a `pass`/no-op, so Phases 2-3 fill them in without touching upstream lines.

This keeps upstream rebase conflicts limited to the toggle-reading lines (criterion #4), not `_prepare_pr_answer` internals.

---

## 5. Byte-identical-when-off verification (CFG-05, criterion #3)

Criterion #3 wants: `describe` on a fixture MR with both toggles off == unpatched upstream (0-byte diff).

### Test infrastructure that exists
- `tests/unittest/` — pytest, `asyncio_mode = "auto"` (from `pyproject.toml`). Async tests need no explicit decorator.
- **`tests/unittest/test_pr_description.py`** and **`tests/unittest/test_pr_description_output_core.py`** already exercise `PRDescription`. Read these for the mocking pattern (git provider mock, `self.data` shaping, calling `_prepare_pr_answer` directly).
- Provider-level publishing is mocked in these tests — no live GitLab needed.

### Practical byte-identical strategy
A true "diff against unpatched upstream binary" is awkward in-repo. The realistic, CI-friendly proof of criterion #3:
1. **Unit-level:** With both toggles off (default), assert `_prepare_pr_answer()` returns the exact same `(pr_title, pr_body, ...)` tuple as before the fork — i.e., the fork guards are all false, so the function is untouched. A characterization test that feeds a fixed `self.data` and asserts the exact `pr_body` string.
2. **Guard audit:** A test/grep asserting every fork-added `pr_description.py` line is inside a `get("enable_conventional_title"/"enable_org_template", False)` guard.
3. **Optional golden-file:** Capture the current `_prepare_pr_answer` output for a fixed input as a golden string BEFORE adding seams; assert equality after. This is the most literal "0-byte diff" proof available without a second checkout.

Given MVP mode and Phase 1's "prove defaults-off" goal, option (1) + (3) (a golden-output characterization test with toggles off) is the strongest, cheapest evidence.

---

## 6. TMPL-09 forward note (not this phase, but seam-relevant)

`self.keys_fix` at **line 49**:
```python
self.keys_fix = ["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]
```
Phase 3 (TMPL-09) extends this so `load_yaml` survives colons/markdown in AI-filled What/Risk. Noted here only so Phase 1 leaves the list untouched and the fork seam is understood.

---

## 7. Risks / landmines

- **Attribute vs `.get` crash:** if any fork code uses attribute access (`get_settings().pr_description.enable_conventional_title`) and the key is missing (e.g. a downstream repo's `.pr_agent.toml` shadows the section), it raises. Criterion #1 mandates `.get(key, False)` — enforce uniformly.
- **Template path depth:** `pr_description.py` is under `pr_agent/tools/`, so the settings dir is two parents up. Verify `.parent.parent` resolves to `pr_agent/` at implementation time; a wrong depth silently fails only when the template is actually read (Phase 3), so add a Phase-1 load test that asserts the file is readable.
- **Env test isolation:** dynaconf binds env at construction. A naive `os.environ[...]=...; get_settings()` won't pick it up because the singleton is already built. Test with a fresh `Dynaconf` or documented reload.
- **Byte-identical is a moving target:** upstream `_prepare_pr_answer` output depends on many other config keys. Pin ALL inputs in the golden test (fixed `self.data`, fixed vars) so the assertion is deterministic.

---

## 8. Recommended plan shape (for the planner)

Given MVP mode + pure-infra phase, a thin vertical slice:
1. **Config + template asset** — add two TOML toggles (default false) + create `pr_agent/settings/org_template.md` with the fixed org template body + Python loader helper (inert). Tests: keys readable via `.get(..., False)`; template file loads.
2. **Env-override verification + docs** — test proving `PR_DESCRIPTION__ENABLE_*` flips the toggle via existing dynaconf env_loader; document both env vars in fork README/config notes (CFG-06).
3. **Defaults-off characterization** — golden-output test asserting `_prepare_pr_answer` byte-identical with toggles off; guard-audit that fork seams are all `.get(flag, False)`-guarded (CFG-05, criterion #4).

All three are small and independent; waves can be 1 (config+template), 1 (env+docs), 2 (characterization, depends on seams existing).
