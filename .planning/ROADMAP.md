# Roadmap: PR-Agent — Org MR Enhancements

**Created:** 2026-07-02
**Milestone:** v1.0 (Angular-convention MR titles + org description template prepend, GitLab-only, config-gated)
**Granularity:** standard (default)
**Project mode:** mvp (Vertical MVP — every phase ships an end-to-end, independently testable increment)
**Core Value:** When a GitLab MR opens, `describe` produces a conventionally-formatted title and an org-standard description body (What/Risk AI-filled, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

## Phases

- [x] **Phase 1: Config skeleton and fork-safe seam** - Add gated toggles (defaults off, env-overridable) and fork-owned template storage; describe output is byte-identical when off (completed 2026-07-03)
- [ ] **Phase 2: Angular-convention title rewriting** - Prompt-side title generation with Python validator/repair and safe fallback; auto-force publishing when enabled
- [ ] **Phase 3: Org template prepend with idempotency** - AI-filled What/Risk sections prepended above PR-Agent's default output, sentinel-bounded, checkbox-preserving on re-runs

## Phase Details

### Phase 1: Config skeleton and fork-safe seam

**Goal:** Establish the two `[pr_description]` toggles (defaults false, env-overridable through dynaconf), store the org template in a fork-owned location, and prove the describe output is byte-identical to upstream when toggles are off — locking in the mergeability and defaults-off conventions before any feature code lands.
**Mode:** mvp
**Depends on:** Nothing (first phase)
**Requirements:** CFG-01, CFG-02, CFG-03, CFG-05, CFG-06
**Success Criteria** (what must be TRUE):

  1. `enable_conventional_title` and `enable_org_template` keys exist under `[pr_description]` in `configuration.toml`, both defaulting to `false`, and are readable via `get_settings().pr_description.get(key, False)` without crashing when absent.
  2. The org template body lives in a fork-owned file (e.g. `pr_agent/settings/org_template.md`) and is loaded from Python, not inlined into the shared upstream `pr_description_prompts.toml`.
  3. Running `describe` on a fixture GitLab MR with both toggles off produces a title and description byte-identical to the same command on unpatched upstream (diff = 0 bytes).
  4. Fork-added code paths in `pr_description.py` are all guarded by `if get_settings().pr_description.get(<flag>, False):` so upstream rebases only conflict on toggle-reading lines, not on `_prepare_pr_answer` internals.
  5. Setting `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` (or `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true`) in the environment enables that toggle for the invocation without editing `configuration.toml` — verified via the existing `config_loader.py:12-18` dynaconf `env_loader` (`envvar_prefix=False`, `merge_enabled=True`) and documented in the fork README / config notes.

**Plans:** 3/3 plans complete
**Wave 1**

- [x] 01-01-PLAN.md — Config toggles (default off) + fork-owned org_template.md + inert Python loader helper (CFG-01, CFG-02, CFG-03)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 01-02-PLAN.md — Env-override verification test (fresh Dynaconf) + fork docs page (CFG-06)
- [x] 01-03-PLAN.md — Golden-output characterization + fork-seam guard-audit + describe suite regression (CFG-05)

### Phase 2: Angular-convention title rewriting

**Goal:** When `enable_conventional_title` is on, `describe` publishes a valid Angular-convention title (`type(scope): summary`) to the GitLab MR — with a Python validator that repairs common defects and safely falls back to leaving the original title untouched when the AI output cannot be salvaged.
**Mode:** mvp
**Depends on:** Phase 1
**Requirements:** CFG-04, TITLE-01, TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06
**Success Criteria** (what must be TRUE):

  1. With `enable_conventional_title=true` on a fixture MR, the published MR title matches `^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$` (Angular type set, kebab-case scope or omitted, imperative lowercase summary, no trailing period, within length).
  2. Enabling `enable_conventional_title` alone is sufficient to publish the rewritten title — the flow auto-forces the publish path without the operator also having to set `generate_ai_title=true`.
  3. Adversarial fixtures (empty scope `()`, trailing period, capitalized type, invalid type, over-length summary, embedded newlines, whitespace-only, empty string) all resolve to either a repaired valid title or a `None` fallback that leaves the pre-existing GitLab title untouched — GitLab never receives an empty or malformed title.
  4. The validator is a pure helper (`_normalize_angular_title`) unit-tested in isolation with adversarial cases and does not mutate `_prepare_pr_answer` inline logic.
  5. When the toggle is off, title behavior is identical to Phase 1 (upstream default) — verified against the same byte-diff fixture.

**Plans:** 1/2 plans executed
Plans:
**Wave 1**

- [x] 02-01-PLAN.md — Pure `_normalize_angular_title` validator/repair helper + 25-row adversarial fixture test (TITLE-02..06)

**Wave 2** *(blocked on Wave 1 completion)*

- [ ] 02-02-PLAN.md — CFG-04 publish-seam auto-force wiring + `self.ai_title` stash + Angular `extra_instructions` steering + behavior matrix (CFG-04, TITLE-01)

### Phase 3: Org template prepend with idempotency

**Goal:** When `enable_org_template` is on, `describe` prepends the org template (AI-filled What/Why and Note/Risk sections plus an empty human checklist) ABOVE PR-Agent's default description output — the upstream `## PR Description` section and file walkthrough remain intact below — and re-running `describe` neither duplicates the template nor resets human-ticked checkboxes.
**Mode:** mvp
**Depends on:** Phase 1 (Phase 2 optional — features are independent once the seam exists)
**Requirements:** TMPL-01, TMPL-02, TMPL-03, TMPL-04, TMPL-05, TMPL-06, TMPL-07, TMPL-08, TMPL-09
**Success Criteria** (what must be TRUE):

  1. With `enable_org_template=true`, a fresh MR's published description contains, in this order and exactly once: the org template block wrapped in `<!-- pr_agent:org_template:start -->` / `<!-- pr_agent:org_template:end -->` sentinels (with AI-filled What/Why and Note/Risk, empty checklist), followed by PR-Agent's default description output UNCHANGED — including its `## PR Description` section, the file walkthrough, and help text. The org template is purely additive (prepended above); nothing is removed from PR-Agent's default output.
  2. Re-running `describe` on the same MR does not duplicate the org template block (only one pair of sentinels remains), and any human-ticked checkbox states from the prior run are preserved verbatim in the new description.
  3. On 10+ real-diff fixtures, `load_yaml(prediction, keys_fix_yaml=self.keys_fix)` returns a dict containing the new `what_why` and `note_risk` keys — the YAML contract does not break on colons/markdown/emojis in AI content because values are emitted as block scalars and `keys_fix` is extended.
  4. When `use_description_markers` mode is active on an MR, the org-template feature emits a WARN log and skips the prepend — the marker flow's placeholder replacement is uncorrupted and produces the same output as if `enable_org_template` were off.
  5. The `File Walkthrough` collapsible `<details>` block still renders correctly and `process_description` in `utils.py` continues to split the file list from the description body — the org template does not contain the literal strings `File Walkthrough` or `Diagram Walkthrough`.

**Plans:** TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Config skeleton and fork-safe seam | 3/3 | Complete    | 2026-07-03 |
| 2. Angular-convention title rewriting | 1/2 | In Progress|  |
| 3. Org template prepend with idempotency | 0/0 | Not started | - |

## Coverage

**v1 requirements:** 21 total
**Mapped:** 21 (100%)
**Unmapped:** 0

| Phase | Requirements | Count |
|-------|--------------|-------|
| Phase 1 | CFG-01, CFG-02, CFG-03, CFG-05, CFG-06 | 5 |
| Phase 2 | CFG-04, TITLE-01, TITLE-02, TITLE-03, TITLE-04, TITLE-05, TITLE-06 | 7 |
| Phase 3 | TMPL-01, TMPL-02, TMPL-03, TMPL-04, TMPL-05, TMPL-06, TMPL-07, TMPL-08, TMPL-09 | 9 |

## Locked Design Decisions (from PROJECT.md / research)

| Decision | Applies to | Note |
|----------|-----------|------|
| Breaking-change `!` marker deferred to v2 | Phase 2 | TITLE-V2-01; out of scope for v1 |
| PR-Agent's own default description (incl. `## PR Description`) is retained unchanged; org template is purely additive (prepended) | Phase 3 | Encoded in TMPL-05 |
| `enable_conventional_title=true` auto-forces AI title publishing | Phase 2 | Encoded in CFG-04 |
| Both toggles are env-overridable via existing dynaconf `env_loader` (e.g. `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true`); no new plumbing, verify-and-document only | Phase 1 | Encoded in CFG-06; leverages `config_loader.py:12-18` |
| v1 idempotency is HTML-comment-sentinel based; no per-section human-edit hashing | Phase 3 | Per-section hash is v2 (TMPL-V2-01) |
| No shared-prompt-file edits — use `extra_instructions` / Jinja `{% if %}` blocks / fork-owned files | Phase 1 sets convention; Phases 2 and 3 honor it | Pitfall 4 mitigation |
| Defaults ship OFF | Phase 1 | Pitfall 7 mitigation |

## Phase Ordering Rationale

- **Phase 1 first:** Establishes the fork-safe seam (toggles, fork-owned template storage, byte-identical-when-off, env-overridable via existing dynaconf loader) so both feature phases plug into a stable foundation. Addresses Pitfalls 4 (upstream merge conflicts) and 7 (defaults-on surprise) upfront.
- **Phase 2 before Phase 3:** Lower complexity, contained blast radius. Validates AI output quality and the config seam on a smaller surface before tackling the template's YAML-contract, idempotency, and assembly-order risks. Addresses Pitfalls 3 (malformed title published) and 6 (GitLab empty-title quirk).
- **Phase 3 last:** Highest complexity (YAML contract + idempotency + assembly order + marker-mode interaction). Bundles idempotency with the initial ship (non-negotiable per Pitfall 2). Addresses Pitfalls 1 (YAML breakage), 2 (duplication / checkbox reset), and 5 (walkthrough collision).
- Phases 2 and 3 are technically independent once Phase 1 lands; the ordering reflects risk isolation, not a hard dependency.

---
*Roadmap created: 2026-07-02*
*Revised: 2026-07-02 — TMPL-05 flipped (org template is purely additive; PR-Agent default output retained unchanged); CFG-06 added to Phase 1 (env-variable toggle via dynaconf, verify-and-document only). Coverage now 21/21.*
