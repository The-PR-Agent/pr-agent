# Phase 4 Research: config__* env vars + walkthrough embed

**Researched:** 2026-07-06
**Status:** Complete
**Method:** Direct source verification (config_loader.py, custom_merge_loader.py, pr_description.py, utils.py, existing tests). Prepared inline after the researcher subagent's write failed to persist twice (Windows stdio hang); all findings are grounded in verbatim on-disk source with file:line references.

## Summary

Phase 4 adds two additive, config-gated capabilities on the shipped v1.0 `describe` enhancements (GitLab MRs only, byte-identical to upstream when `enable_org_template=false`):

1. **`config__*` env-var access** for the fork toggles via a dual-read helper (`config.*` first, `pr_description.*` fallback) plus a load-time mirror so `CONFIG__USE_DESCRIPTION_MARKERS` reaches upstream read sites.
2. **A single org-formatted body** when `enable_org_template=true`: a new `## Changes` section in the template carries `pr_agent:walkthrough` + `pr_agent:diagram`, and a new `enable_pr_agent_output` toggle (default `false`) suppresses PR-Agent's default body when the org template is active.

Both build cleanly on Phase 3 seams (`_stash_org_template_fields`, `_prepend_org_template`, `_render_org_template_block`). No upstream code edits are required — everything routes through fork-owned helpers plus one load-time mirror in `config_loader.py`.

## Findings

### Q1 — Dual-read resolution

**How dynaconf maps env vars** (`pr_agent/config_loader.py:12-44`):
- `envvar_prefix=False` — no `DYNACONF_` prefix required.
- loaders = `['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader']` — TOML merged first, then env vars override (env_loader runs last, so env wins).
- `merge_enabled=True` — overlapping section fields merge rather than replacing the whole section.
- The `SECTION__KEY` double-underscore convention maps an env var to a settings path.

**Confirmed mapping:**
- `CONFIG__ENABLE_ORG_TEMPLATE=true` → `settings.config.enable_org_template` (the `[config]` section).
- `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true` → `settings.pr_description.enable_org_template` (the `[pr_description]` section).

Because the fork keys are **defined** under `[pr_description]` (configuration.toml:129-131) and NOT under `[config]`, `get_settings().config.get("enable_org_template")` returns `None` unless `CONFIG__*` was explicitly set. That makes a config-first dual-read safe: a `None` from `[config]` cleanly falls through to the `[pr_description]` value.

**Recommended helper** (fork-owned, module-level in `pr_description.py`):

```python
def _fork_toggle(key, default=False):
    # config.* (legacy CONFIG__* env access) wins when explicitly set; else pr_description.*
    val = get_settings().get("config", {}).get(key, None)
    if val is not None:
        return val
    return get_settings().pr_description.get(key, default)
```

Route `_conventional_title_enabled` (:121), `_org_template_enabled` (:125), and `_org_template_active`'s marker check (:130) through this helper. `enable_pr_agent_output` reads through it too.

**Caveat (documented in `docs/fork/org-mr-enhancements.md:66-77`):** dynaconf binds env vars at `Dynaconf(...)` construction time (module import). A late `os.environ` mutation cannot affect the singleton — all tests must build a fresh Dynaconf (see Validation Architecture).

### Q2 — use_description_markers mirror

`use_description_markers` is read at **three** sites, two of them upstream:
- `pr_description.py:130` — fork `_org_template_active` (we control this; route through `_fork_toggle`).
- `pr_description.py:358` — **upstream** marker-vs-normal branch in `run()`.
- `pr_description.py:453` — **upstream** `_prepare_pr_answer_with_markers` guard.

A dual-read helper alone cannot fix the two upstream sites without editing upstream code. To make `CONFIG__USE_DESCRIPTION_MARKERS` reach them with **zero upstream edits**, use a **load-time mirror** in `config_loader.py`: after the singleton is built AND after the pyproject load (`config_loader.py:89-91`), copy an explicitly-set `config.use_description_markers` into `pr_description.use_description_markers`.

```python
# config_loader.py, after the pyproject load block (~line 91)
def _mirror_fork_config_keys():
    # Let CONFIG__USE_DESCRIPTION_MARKERS reach upstream read sites that only
    # consult pr_description.*. Only mirrors when [config] value is explicitly set.
    val = global_settings.get("config.use_description_markers", None)
    if val is not None:
        global_settings.set("pr_description.use_description_markers", val)

_mirror_fork_config_keys()
```

**Why load-time mirror over a read-shim:** the read-shim would require editing the two upstream call sites (:358, :453) — exactly what the fork's "no upstream edits / clean rebases" convention forbids. The mirror runs once at import, is invisible to upstream code, and is naturally correct because env binds at construction (the value is already resolved by the time the mirror runs). Backward compatibility holds: `PR_DESCRIPTION__USE_DESCRIPTION_MARKERS` still works (mirror only copies when the `[config]` value is non-None; it never clobbers an explicitly-set `[pr_description]` value with a default).

**Ordering constraint:** the mirror MUST run after `config_loader.py:89-91` (pyproject load), because pyproject `[tool.pr-agent]` may also set `config.use_description_markers`. `apply_secrets_manager_config()` runs at server startup, not import, so it does not affect describe toggles here.

### Q3 — `## Changes` marker embed

**Source values:**
- Walkthrough: the **raw** GFM table. In `_prepare_pr_answer_with_markers` the raw table is `walkthrough_gfm` from `self.process_pr_files_prediction(walkthrough_gfm, self.file_label_dict)` (:816). In the normal path (`_prepare_pr_answer`, :363) the returned `changes_walkthrough` is **wrapped** in a `<details><summary><h3>File Walkthrough</h3>` block.
- Diagram: `self.data.get('changes_diagram')` — sanitized and **retained** in `self.data` (`_prepare_data` at :712-715 pops-then-restores it; it is never consumed by the normal path), so it is available at prepend time.

**Marker-replacement idiom to mirror** (from `_prepare_pr_answer_with_markers`):
- Walkthrough: `block.replace('pr_agent:walkthrough', walkthrough_gfm or '')` (:818).
- Diagram: `re.sub(r'<!--\s*pr_agent:diagram\s*-->|pr_agent:diagram', ai_diagram, block)` (:826). Prefer a `lambda: ai_diagram` replacement or `str.replace` to avoid backreference interpretation of `\1` etc. in mermaid content.

**`## Changes` header is safe:** `process_description` (utils.py:1327-1344) splits only on `PRDescriptionHeader.FILE_WALKTHROUGH.value` (the literal "File Walkthrough") via a `<details>...<h3>File Walkthrough</h3>` regex. A plain `## Changes` header does not match. **Phase 3 SC#5 holds** as long as the template file itself never contains the literals "File Walkthrough" / "Diagram Walkthrough".

**CRITICAL landmine — do NOT embed the wrapped `changes_walkthrough`:** the normal-path `changes_walkthrough` string contains the literal "File Walkthrough" `<h3>` header. Embedding it inside the sentinel block would (a) reintroduce the forbidden literal into the org block and (b) risk `process_description` splitting inside the org template on re-parse. **Use the raw `walkthrough_gfm` table**, obtained by calling `self.process_pr_files_prediction("", self.file_label_dict)` in the fork path (same call the marker path uses), NOT the wrapped return value.

**Where:** capture walkthrough + diagram in `_stash_org_template_fields` (:719) (it already runs at :343, before assembly, and has `self.data` + `self.file_label_dict` available); thread them into `_render_org_template_block` (:192) which grows two params and fills the two markers in the template body before checkbox re-application.

### Q4 — enable_pr_agent_output suppression

**Cleanest seam: inside `_prepend_org_template` (:728).** It already early-returns unchanged `pr_body` when `_org_template_active` is false (:729-730), so any suppression added below that guard **cannot** affect the byte-identical-when-off path.

```python
def _prepend_org_template(self, pr_body: str) -> str:
    if not _org_template_active(self.git_provider):
        return pr_body                      # byte-identical-when-off (unchanged)
    template = load_org_template()
    if not template:
        return pr_body
    ...
    block = _render_org_template_block(template, ..., walkthrough, diagram, existing_description)
    if not _fork_toggle("enable_pr_agent_output", False):
        return block                        # suppress PR-Agent default body
    clean_body = _strip_org_template_block(pr_body)
    return f"{block}\n\n{clean_body}" if clean_body else block
```

The body is fully finalized (description + walkthrough at :363-366, help text :370-383, relevant-configs :388-389) before the single `_prepend_org_template(pr_body)` call at :392. Suppressing at that seam drops the whole default body in one place. Default `false` means: with the org template on, the default body is suppressed unless the operator opts back in with `enable_pr_agent_output=true` (in which case walkthrough renders twice — the accepted copy-semantics trade-off from CONTEXT.md).

### Q5 — Validation Architecture

See dedicated section below.

## Recommended Approach

1. **Config surface** — add `enable_pr_agent_output=false` to `[pr_description]` (configuration.toml:129-131 fork block). Keys stay in `[pr_description]`.
2. **Dual-read helper** `_fork_toggle(key, default)` in `pr_description.py`; route the four fork reads through it.
3. **Load-time mirror** `_mirror_fork_config_keys()` in `config_loader.py` after the pyproject load, mirroring an explicitly-set `config.use_description_markers` into `pr_description.use_description_markers`.
4. **Template** — add a `## Changes` section with `pr_agent:walkthrough` and `pr_agent:diagram` markers to `org_template.md` (no forbidden literals).
5. **Assembly** — capture raw `walkthrough_gfm` + `changes_diagram` in `_stash_org_template_fields`; fill markers in `_render_org_template_block`; apply `enable_pr_agent_output` suppression in `_prepend_org_template`.
6. **Docs** — extend `docs/fork/org-mr-enhancements.md` with the `config__*` table and `enable_pr_agent_output`.

## Validation Architecture

**Env-override tests (fresh-Dynaconf, mirror `tests/unittest/test_org_toggles_env_override.py`):** build a Dynaconf mirroring `dynconf_kwargs` with each env var set, assert the toggle resolves true/false:
- `CONFIG__ENABLE_ORG_TEMPLATE`, `CONFIG__ENABLE_CONVENTIONAL_TITLE`, `CONFIG__ENABLE_PR_AGENT_OUTPUT`, `CONFIG__USE_DESCRIPTION_MARKERS`.
- Backward-compat: `PR_DESCRIPTION__*` equivalents still flip the toggles.
- Precedence: `config.*` wins over `pr_description.*` when both set.
- Mirror: after `_mirror_fork_config_keys()`, `pr_description.use_description_markers` reflects `CONFIG__USE_DESCRIPTION_MARKERS`.

**Assembly tests (mirror `tests/unittest/test_org_template_prepend.py`):**
- `## Changes` embed: rendered block contains the walkthrough table + diagram; block contains neither `pr_agent:walkthrough` nor `pr_agent:diagram` literal after fill.
- No forbidden literals: block contains neither "File Walkthrough" nor "Diagram Walkthrough".
- Suppression on (`enable_pr_agent_output=false`): published body == org block only (no default `## PR Description`).
- Suppression off (`enable_pr_agent_output=true`): default body present below the block.

**Byte-identical-when-off regression:** with `enable_org_template=false`, `_prepend_org_template` returns `pr_body` unchanged regardless of `enable_pr_agent_output` — assert the published body equals the pre-Phase-4 output.

**Nyquist coverage note:** each capability (dual-read, mirror, embed, suppression, off-regression) has ≥1 independent observable assertion; env and assembly dimensions are tested separately so a single seam change cannot mask two failures.

## Risks / Landmines

1. **MagicMock config in existing tests.** `settings.config` is a `MagicMock` in the Phase 3 tests; `settings.config.get("key")` returns a truthy `MagicMock`, not `None`, so `_fork_toggle` would wrongly treat every key as explicitly-set-in-config. **Fix pattern:** in affected tests set `settings.config.get.side_effect = lambda key, default=None: default` so absent keys fall through to `pr_description`. Flag every existing describe test that mocks `get_settings()`.
2. **Wrapped vs raw walkthrough** (Q3) — embedding the wrapped `changes_walkthrough` reintroduces the "File Walkthrough" literal and breaks Phase 3 SC#5. Use the raw `walkthrough_gfm` table.
3. **Mirror ordering** — must run after the pyproject load (config_loader.py:89-91) or pyproject-set values are missed.
4. **Env binds at construction** — no test may mutate `os.environ` after import and expect the singleton to change; always build a fresh Dynaconf.
5. **Double walkthrough** when `enable_pr_agent_output=true` + org template on — expected per copy-semantics decision; document, don't "fix".
6. **`changes_diagram` availability** — relies on `_prepare_data` retaining it in `self.data` (:712-715). If a future rebase pops it, the diagram marker fills empty (graceful). Capture it in `_stash_org_template_fields` at :343 to be safe.

## Open Questions

- None blocking. Helper naming and whether the mirror also covers `enable_org_template`/`enable_conventional_title` (harmless to mirror all fork keys for symmetry) are Claude's-discretion planning choices already flagged in CONTEXT.md.

## Validation Architecture

(Anchor heading for Nyquist consumption — see the "Validation Architecture" section above for the full strategy.)
