# Requirements: PR-Agent — Org MR Enhancements

**Defined:** 2026-07-02
**Core Value:** When a GitLab MR opens, `describe` produces a conventionally-formatted title and an org-standard description body (What/Risk AI-filled, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

## v1 Requirements

Requirements for this milestone. Each maps to roadmap phases. All new behavior is GitLab-only and config-gated (defaults OFF).

### Configuration

- [ ] **CFG-01**: `enable_conventional_title` toggle exists in `[pr_description]` config, defaults to `false`
- [ ] **CFG-02**: `enable_org_template` toggle exists in `[pr_description]` config, defaults to `false`
- [ ] **CFG-03**: The org template body is stored in a fork-owned location (separate file or constant), not inlined into the shared upstream prompt file, to minimize upstream merge conflicts
- [ ] **CFG-04**: When `enable_conventional_title` is on, AI title publishing is auto-forced so the rewritten title actually reaches GitLab (no need to also set `generate_ai_title`)
- [ ] **CFG-05**: When toggles are off, `describe` output is byte-identical to upstream PR-Agent behavior

### Title (Angular Commit Convention)

- [ ] **TITLE-01**: `describe` rewrites the MR title to `type(scope): summary`, with type and scope inferred from the diff by the AI
- [ ] **TITLE-02**: Commit type is constrained to the Angular set (`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`)
- [ ] **TITLE-03**: Subject follows Angular rules — imperative present tense, lowercase first letter, no trailing period
- [ ] **TITLE-04**: Scope is a single kebab-case token inferred from the diff; parens are omitted entirely when scope is unknown (never emit empty `()`)
- [ ] **TITLE-05**: A Python validator checks the AI title and repairs common defects (lowercase type, strip trailing period, truncate over-length, drop empty scope); if it cannot produce a valid title, it falls back to leaving the original MR title untouched
- [ ] **TITLE-06**: An empty or whitespace-only AI title is coerced to "leave title untouched" (never set an empty GitLab title)

### Template (Org Description Prepend)

- [ ] **TMPL-01**: `describe` prepends the org template (What does this MR do?/Why, Note/Risk, Checklist) at the start of the MR description
- [ ] **TMPL-02**: The AI fills the "What does this MR do? Why?" and "Note / Risk" sections
- [ ] **TMPL-03**: The checklist renders as empty checkboxes; the AI never ticks them
- [ ] **TMPL-04**: PR-Agent's existing generated walkthrough/file-summary is retained below the org template
- [ ] **TMPL-05**: PR-Agent's own default `## PR Description` section is removed when the org template is on (the org template's What/Why supersedes it); the walkthrough and other sections remain
- [ ] **TMPL-06**: Re-running `describe` is idempotent — the org template is not duplicated (HTML-comment sentinel markers wrap the block and are detected/replaced)
- [ ] **TMPL-07**: Human-ticked checkbox state is preserved verbatim across re-runs
- [ ] **TMPL-08**: When `use_description_markers` mode is active, the org-template feature is skipped with a WARN log rather than corrupting the marker flow
- [ ] **TMPL-09**: New AI-filled values are emitted as YAML block scalars and `keys_fix` is extended so `load_yaml` does not break on colons/markdown in the content

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Title

- **TITLE-V2-01**: Detect breaking changes from the diff and emit the Angular `!` marker (e.g. `feat(api)!: ...`)
- **TITLE-V2-02**: Support the Angular `revert` special commit form

### Template

- **TMPL-V2-01**: Per-section human-edit detection (hash-based) so manually edited What/Risk content is preserved, not overwritten

### Providers

- **PROV-V2-01**: Extend both features to GitHub, Bitbucket, and Azure DevOps

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Changes to `review` and `improve` commands | Milestone scoped to `describe` only |
| Cross-provider support (GitHub/Bitbucket/Azure) in v1 | Prove the GitLab flow first; deferred to v2 |
| AI-authored / dynamic per-MR template structure | Org template structure is fixed |
| AI ticking the checklist items | Checklist items (self-reviewed, tested) require human judgment |
| Compound Angular types (`feat/fix`) | Pick a single primary type per MR |
| Rewriting titles that already conform | Leave conforming titles or only normalize |
| Breaking-change `!` marker | Harder for AI to judge reliably; deferred to v2 |

## Traceability

Which phases cover which requirements. Populated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| CFG-01 | TBD | Pending |
| CFG-02 | TBD | Pending |
| CFG-03 | TBD | Pending |
| CFG-04 | TBD | Pending |
| CFG-05 | TBD | Pending |
| TITLE-01 | TBD | Pending |
| TITLE-02 | TBD | Pending |
| TITLE-03 | TBD | Pending |
| TITLE-04 | TBD | Pending |
| TITLE-05 | TBD | Pending |
| TITLE-06 | TBD | Pending |
| TMPL-01 | TBD | Pending |
| TMPL-02 | TBD | Pending |
| TMPL-03 | TBD | Pending |
| TMPL-04 | TBD | Pending |
| TMPL-05 | TBD | Pending |
| TMPL-06 | TBD | Pending |
| TMPL-07 | TBD | Pending |
| TMPL-08 | TBD | Pending |
| TMPL-09 | TBD | Pending |

**Coverage:**
- v1 requirements: 20 total
- Mapped to phases: 0 (roadmap pending)
- Unmapped: 20 ⚠️

---
*Requirements defined: 2026-07-02*
*Last updated: 2026-07-02 after initial definition*
