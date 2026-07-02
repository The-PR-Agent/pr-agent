# Feature Research

**Domain:** Brownfield enhancement to PR-Agent `describe` command (GitLab MRs) — Angular-convention title rewriting + org description template prepend
**Researched:** 2026-07-02
**Confidence:** HIGH (behavior grounded in `pr_agent/tools/pr_description.py` and PROJECT.md decisions)

## Feature Landscape

### Feature 1 — Angular-Convention MR Title Rewriting

#### Table Stakes (Users Expect These)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Emit `type(scope): summary` for the standard Angular types (`feat`, `fix`, `refactor`, `docs`, `chore`, `perf`, `test`, `build`, `ci`, `style`, `revert`) | Angular convention is a spec; devs assume any tool that claims to follow it emits the full valid vocabulary | LOW | Type is inferred by the AI from the diff. Add the enum to the prompt so the LLM cannot hallucinate a non-Angular type. |
| Summary is imperative, lowercase, no trailing period, under ~72 chars | Convention rule; long titles get truncated in GitLab lists and email digests | LOW | Post-process the LLM output: strip trailing `.`, lowercase the first char after `: `, enforce a max length with graceful clip. |
| Config-gated: OFF by default; ON via `pr_description.rewrite_title_angular = true` (or similar) | PROJECT.md decision — fork must stay mergeable with upstream, and default PR-Agent behavior must not change | LOW | Add flag alongside existing `generate_ai_title` in `configuration.toml`. When flag is ON, force `generate_ai_title = true` implicitly, otherwise there is nothing to rewrite. |
| Idempotent on re-run: if title is already `type(scope): …` and still describes the diff, leave it (or only normalize casing/punctuation) | Re-running `describe` is common (new commits pushed) and re-titling churn is annoying | MEDIUM | Detect existing prefix with regex `^(feat\|fix\|refactor\|docs\|chore\|perf\|test\|build\|ci\|style\|revert)(\([^)]+\))?!?:\s`. If present, only normalize whitespace / clip length; do not re-invoke the LLM to change the type. |
| Graceful fallback: if the LLM returns an invalid type or unparseable title, fall back to original title (do not publish garbage) | Existing PR-Agent pattern (`title_to_publish = None` on failure at line 185) | LOW | Wrap validation in try/except; on failure, log and pass `None` to `publish_description` so GitLab keeps the manual title. |
| Multi-type MR — pick a single primary type deterministically | Angular convention requires one type per commit/PR; users trust the tool to choose sensibly | MEDIUM | Priority order in the prompt: `feat` > `fix` > `perf` > `refactor` > `test` > `docs` > `build` > `ci` > `chore` > `style`. Tie-break by largest changed-line-count category. Never emit compound types like `feat/fix`. |
| Breaking-change marker `!` before the colon (e.g. `feat(api)!: rename user_id to userId`) | Angular v2 spec; teams that gate releases on SemVer bumps rely on this signal | MEDIUM | AI must detect breaking changes from the diff (removed/renamed public API, changed function signatures, DB migrations without back-compat). Prefer the `!` marker over the `BREAKING CHANGE:` footer for MR titles (titles have no footer). |

#### Differentiators (Nice-to-Have Polish)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Scope inference from top-level directory or module | Makes titles skimmable in MR lists (`feat(auth): …` vs bare `feat: …`) | MEDIUM | Heuristic: if >70% of changed files share a top-level dir, use it as scope; else omit. Let the AI propose scope but validate against the actual changed-files list. |
| Emit `BREAKING CHANGE:` note in body (in addition to `!` in title) | Some CI/release tools parse the body footer, not the title | LOW | Append to org template's Risk section when breaking change is detected. |
| Preserve ticket/issue tag suffixes from the original title (e.g. `[PROJ-123]`) | Teams often prefix titles with ticket IDs for Jira/Linear linkage; wiping them breaks tracking | MEDIUM | Detect `\[[A-Z]+-\d+\]` or trailing `(#123)` in the original title and reattach to the rewritten title. |
| Emit revert titles as `revert: <original title>` | Angular convention has a special revert form | LOW | Detect revert-only diffs (mostly deletions restoring prior content) or a merge-commit revert. Rare edge case. |

#### Anti-Features (Deliberately Do Not Build)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Compound types like `feat/fix(auth): …` or `feat+fix: …` | Feels honest for mixed MRs | Not valid Angular. Downstream tools (semantic-release, conventional-changelog) will reject or misparse | Pick one primary type per the priority rule; put secondary changes in the org template's "What/Why" section |
| Free-form scope invented by the AI (e.g. `feat(better-perf): …`) | Sounds descriptive | Scopes drift over time and become meaningless; Angular expects a stable module/package name | Restrict scope to top-level dir names from the actual diff, or omit |
| Rewriting a manually-set title that already conforms | Consistency | Users tick manual edits deliberately (typos, wording preference) — see upstream issue #2474 which the existing `title_to_publish = None` pattern already guards against | Idempotency check: if title matches Angular regex, only normalize whitespace/case |
| Enforcing scope as REQUIRED | Feels tidy | Cross-cutting MRs (refactors, dep bumps, repo-wide chores) legitimately have no scope; forcing one leads to `chore(root): …` noise | Scope is optional per the spec; omit when the diff is not localized |
| Silently changing the title on every re-run | "Always fresh" | Author sees their MR title flip repeatedly, loses trust in the tool | Only rewrite when: (a) title is non-conforming, or (b) `generate_ai_title=true` AND the diff has meaningfully changed. Keep a marker/hash to detect no-op re-runs. |

---

### Feature 2 — Org Description Template Prepend

The fixed template (per PROJECT.md):

```
## 📌 What does this MR do? Why?
<AI-filled paragraph>

## ⚠️ Note / Risk
<!-- System, performance, security impact -->
<AI-filled bullets>

## ✅ Checklist
- [ ] Self-reviewed
- [ ] Tested
- [ ] No backward compatibility break
- [ ] Documentation updated (if needed)
```

Followed by PR-Agent's existing generated body (walkthrough, file table, help text).

#### Table Stakes (Users Expect These)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Template appears at the TOP of the MR description, walkthrough below | PROJECT.md decision; humans read top-down and org context beats file-level detail | LOW | Assemble in `_prepare_pr_answer` (or a wrapper) as `template_block + "\n\n---\n\n" + existing_pr_body`. |
| "What / Why" section AI-filled with 2-4 sentence prose (not bullet dump) | It is the human-readable summary reviewers scan first | LOW | New YAML key in the prompt output (`what_why:`). Prompt must emphasize *why*, not just *what* (that duplicates the walkthrough). |
| "Note / Risk" section AI-filled with concrete risks (system/perf/security), or explicit "None identified" when the diff is trivial | Empty risk sections train reviewers to ignore them | MEDIUM | New YAML key (`note_risk:`). Prompt with the categories from the HTML comment (System, performance, security impact). If no risks: emit `- None identified` rather than blank. Keep the HTML comment as a hint for humans editing later. |
| Checklist stays as empty checkboxes | Requires human judgment ("self-reviewed", "tested"); AI cannot honestly tick these | LOW | Hardcoded literal string in the template constant. Never let the LLM touch it. |
| Idempotency on re-run: template appears exactly once, no duplication | `publish_description` on GitLab overwrites the body, but re-runs still overwrite whatever body currently exists (which may include the template). Need to strip old template before regenerating | HIGH | See "Idempotency Strategy" below. |
| Preserve human-ticked checkboxes across re-runs | The single biggest annoyance if broken — reviewer ticks "Self-reviewed", pushes a commit, tool wipes the tick | HIGH | Fetch current MR description, parse the checklist section, diff the boxes, carry ticked state into the new template. |
| PR-Agent walkthrough/file-summary retained below the template unchanged | PROJECT.md decision; the walkthrough is existing value we do not want to lose | LOW | The current `_prepare_pr_answer` output becomes the "below" block. No changes to walkthrough generation. |
| Config-gated: OFF by default; ON via `pr_description.prepend_org_template = true` (or similar) | Fork/upstream compatibility (same rationale as Feature 1) | LOW | Add flag to `configuration.toml`. When OFF, current behavior is identical to upstream. |
| Graceful fallback: if LLM omits `what_why` or `note_risk`, publish the template with placeholder text (`_AI could not generate — please fill_`) rather than skipping the section | Skipped sections silently produce broken MR templates that look correct at a glance | LOW | Default strings in the template renderer; log at WARN. |

#### Differentiators (Nice-to-Have Polish)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Section headings configurable (labels, emojis) via `configuration.toml` | Other teams forking may want slightly different template text | LOW | Load template strings from settings; ship the org's defaults. Do not templatize the *structure* (that becomes a support burden). |
| Risk categorization (auto-labels `risk:security`, `risk:performance` when detected) | Feeds triage and dashboards | MEDIUM | Extend existing `_prepare_labels` to look at `note_risk` content. |
| "What / Why" links to the ticket detected by `ticket_pr_compliance_check` | Removes duplicate work for authors who already put the ticket in the MR | LOW | The tool already extracts ticket links (`extract_ticket_links_from_pr_description`). Reuse. |
| Detect breaking changes flagged by Feature 1's `!` marker and auto-tick a "This is a breaking change" line in Risk | Cross-feature reinforcement | LOW | Only meaningful if Feature 1 is also enabled. |

#### Anti-Features (Deliberately Do Not Build)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| AI-tick the checklist ("looks self-reviewed to me") | Feels helpful | Actively harmful — undermines the checklist's purpose (human attestation) and creates false compliance signals for audits | Always leave checkboxes empty. Document in the tool's README why. |
| Dynamic per-MR template (AI decides which sections to include) | "Adaptive" and "smart" | Reviewers rely on template consistency to skim quickly; a template that changes shape MR-to-MR is worse than no template | Fixed structure (per PROJECT.md); AI only fills the content. |
| Rewriting or replacing the user's manually-written "What/Why" prose on re-run | "Keep it fresh with the latest diff" | Authors edit these sections to add context the diff cannot show (linked designs, rollout plan). Overwriting destroys that work | On re-run, detect whether the section was edited vs still the AI original (e.g., hash comparison via hidden HTML comment marker `<!-- pr_agent:what_why_hash:abc123 -->`). If human-edited, leave alone. |
| Injecting the template into an existing MR body via string concatenation without parsing | "Simple" | Duplicates the template on every re-run, produces monster descriptions after a few pushes | Idempotency strategy below. |
| Skipping the walkthrough when the template is present | "Cleaner" | Loses PR-Agent's core value; the walkthrough is the tool's differentiator | Template above, walkthrough below, separated by `---`. |
| Publishing the template as a comment separate from the description | "Non-invasive" | The org's workflow expects the template *in* the description (that is where GitLab UI surfaces it) | Publish inline via `publish_description`. |

---

## Idempotency Strategy (applies to Feature 2; also validates Feature 1)

Re-runs are the trickiest part. The approach:

1. **Wrap AI-generated sections with sentinel HTML comments:**
   ```
   <!-- pr_agent:org_template_start -->
   ## 📌 What does this MR do? Why?
   <!-- pr_agent:what_why_start hash=abc123 -->
   …AI content…
   <!-- pr_agent:what_why_end -->
   …
   <!-- pr_agent:org_template_end -->
   ```
2. **On re-run, before generating:** fetch current MR body, locate sentinels.
   - If sentinels found: extract the checklist block (with any ticked boxes), extract per-section hashes.
   - For each AI-filled section: if the content between sentinels differs from `hash`, treat as human-edited and preserve verbatim; else regenerate.
   - Preserve ticked checkboxes by parsing `- [x]` vs `- [ ]` line-for-line and re-emitting the same state under new template.
3. **Reassemble:** new template block + preserved checklist state + separator + fresh walkthrough (walkthrough is always regenerated; it is a mechanical view of the diff).
4. **Publish once** via `publish_description(title, body)` — GitLab overwrites atomically.

Complexity is HIGH but the code is contained (one helper function `merge_with_existing_template(current_body, new_ai_data) -> new_body`). All other pieces are straightforward.

---

## Feature Dependencies

```
Feature 1 (Angular title)
    └── uses existing "generate_ai_title" plumbing in _prepare_pr_answer (line 568)
    └── uses existing LLM prompt path (pr_description_prompt.system/user)
    └── needs: constrained type enum, breaking-change detection, idempotency regex

Feature 2 (Org template prepend)
    └── uses existing "pr_body" assembly in _prepare_pr_answer (line 578+)
    └── needs: new YAML keys (what_why, note_risk) in LLM output
    └── needs: sentinel-based idempotency helper
    └── needs: checklist state preservation (fetch current description)

Feature 1 --enhances--> Feature 2
    (breaking-change detection from title feeds the Risk section)

Feature 2 --depends on--> git_provider.get_pr_description(full=True)
    (must read existing body for idempotency; currently called with full=False on line 66)
```

### Dependency Notes

- **Feature 2 requires reading existing description at full fidelity.** Line 66 currently calls `get_pr_description(full=False)`. For idempotency, add a second read (or extend `self.user_description` handling) that returns the full body including AI-generated content. Confirm GitLab provider supports this.
- **Both features must share the LLM call.** The existing prompt already produces a title. Extending the same YAML output with `what_why` and `note_risk` keys avoids a second LLM round-trip and keeps token cost flat.
- **Feature 1 has no runtime dependency on Feature 2.** They can ship independently. Toggling one without the other must produce coherent output (title-only enhancement, or template-only enhancement).

---

## MVP Definition

### Launch With (v1)

- [ ] Angular type inference from diff (feat/fix/refactor/docs/chore, plus perf/test/build/ci/style/revert) — table stakes for the convention claim
- [ ] Angular title assembly with optional scope, imperative summary, length clip — table stakes
- [ ] Config toggle `rewrite_title_angular` — required for upstream mergeability
- [ ] Idempotency check on titles (leave conforming titles alone) — table stakes
- [ ] Graceful fallback on parse failure (existing pattern) — table stakes
- [ ] Fixed org template renderer with `what_why` and `note_risk` YAML keys — table stakes
- [ ] Checklist as literal empty-boxes constant — table stakes
- [ ] Template above, walkthrough below, single `---` separator — table stakes (PROJECT.md decision)
- [ ] Config toggle `prepend_org_template` — required for upstream mergeability
- [ ] Sentinel-comment-based idempotency (no duplicate template on re-run) — table stakes
- [ ] Checklist tick preservation across re-runs — table stakes (most-reported annoyance if missing)
- [ ] Breaking-change `!` marker when detected — table stakes for Angular claim

### Add After Validation (v1.x)

- [ ] Scope inference from top-level directory heuristic — differentiator, adds polish once titles are stable
- [ ] Ticket-tag preservation from original title — differentiator, needs light regex work
- [ ] Per-section hash marker for "was this human-edited?" detection — differentiator, only needed if authors report their edits getting wiped
- [ ] Risk auto-labels (`risk:security`, `risk:performance`) — differentiator, feeds triage
- [ ] Configurable section headings via TOML — only if a second team wants to fork

### Future Consideration (v2+)

- [ ] Extend both features to GitHub PRs and Bitbucket — PROJECT.md scopes v1 to GitLab; wait until GitLab flow is validated
- [ ] AI-suggested checklist additions per repo — high risk of over-engineering; only pursue if the checklist is materially incomplete
- [ ] `BREAKING CHANGE:` footer in body in addition to `!` in title — only if a downstream tool actually parses it

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Angular type inference + assembly | HIGH | LOW | P1 |
| Title idempotency check | HIGH | LOW | P1 |
| Multi-type primary selection rule | HIGH | LOW | P1 |
| Breaking-change `!` marker | MEDIUM | MEDIUM | P1 |
| Scope inference | MEDIUM | MEDIUM | P2 |
| Ticket-tag preservation | MEDIUM | LOW | P2 |
| Revert title form | LOW | LOW | P3 |
| Org template renderer | HIGH | LOW | P1 |
| AI-filled What/Why | HIGH | LOW | P1 |
| AI-filled Note/Risk | HIGH | MEDIUM | P1 |
| Sentinel-based idempotency | HIGH | HIGH | P1 |
| Checklist state preservation | HIGH | MEDIUM | P1 |
| Human-edit detection (per-section hash) | MEDIUM | MEDIUM | P2 |
| Risk auto-labels | LOW | LOW | P3 |
| Configurable section headings | LOW | LOW | P3 |

**Priority key:**
- P1: Must have for launch — either table stakes or a decision baked into PROJECT.md
- P2: Should have, add after validation
- P3: Nice to have, defer

---

## Edge Case Catalogue (per quality gate)

### Feature 1 — Title Rewriting

| Edge case | Expected behavior |
|-----------|-------------------|
| MR contains both a new feature and a bugfix | Emit `feat` (feature wins over fix by the priority rule). Mention the fix in the "What/Why" section of Feature 2. |
| MR is a pure dependency bump | `chore(deps): bump <pkg> to <ver>` — a very common variant; add `deps` as an allowed synthetic scope for this case. |
| MR touches 40 files across 6 top-level directories | Omit scope. Bare `type: summary`. |
| MR touches 40 files but 35 are in `pr_agent/tools/` | `type(tools): …`. |
| Existing title is `feat(auth): add sso` and diff unchanged | Leave title untouched (idempotency). |
| Existing title is `feat(auth): Add SSO.` (bad casing, trailing period) | Normalize to `feat(auth): add sso`. Debatable — safer default is "leave manual titles alone unless clearly non-conforming"; document the choice. |
| Existing title is `WIP: adding stuff` | Rewrite in full (non-conforming prefix). |
| Diff removes a public function without a deprecation path | Emit `refactor(api)!: remove deprecated foo()` or `feat(api)!: …`. AI must detect the removal. |
| LLM returns `type: build/ci` (compound) | Reject in validation; take the first valid token or fall back to original title. |
| LLM returns `revert(auth): revert sso commit` | Accept — Angular's revert form is a valid special case. |
| Original title has `[PROJ-1234]` prefix | (Post-MVP) Preserve as suffix: `feat(auth): add sso [PROJ-1234]`. |

### Feature 2 — Template Prepend

| Edge case | Expected behavior |
|-----------|-------------------|
| First run on a fresh MR (no existing body) | Emit template + walkthrough. Straightforward. |
| Second run, no human edits, no ticked boxes | Regenerate What/Why and Risk; checkboxes stay empty; walkthrough refreshed. Should be idempotent-ish (content may differ if diff changed). |
| Second run, human ticked `Self-reviewed` and `Tested` | Preserve those two ticks; other boxes stay empty. |
| Second run, human replaced the What/Why prose with their own | Detect via hash mismatch; keep human prose verbatim. (P2 in MVP — for v1, always regenerate and rely on hash marker as an escape hatch.) |
| Second run, human deleted the org template entirely | Sentinels are gone; treat as fresh run, re-emit template. Author sees it come back. Document as expected behavior; provide a config to permanently opt out per-MR via a label like `no-template`. |
| Existing user description contains `pr_agent:` markers (the `use_description_markers` path) | Feature 2 is incompatible with `use_description_markers=true`. Detect and skip Feature 2 with a WARN log. Do not silently corrupt marker-based flows. |
| LLM produces empty `note_risk` | Emit `- None identified` under the Risk heading, not a blank section. |
| LLM produces `note_risk` longer than ~500 chars | Clip with `…` suffix; keep the raw content available in logs. |
| MR body approaches GitLab's description limit (~1MB, effectively rarely hit but real) | Prefer trimming the walkthrough (it is regenerable) over trimming the template (it is authored context). |
| Author disables `prepend_org_template` mid-lifecycle after ticking boxes | Next run publishes without the template; ticks are effectively lost. Document; do not attempt to preserve template state when the feature itself is off. |
| Both features off | Behavior is byte-identical to upstream PR-Agent. This is the compatibility contract. |

---

## Behavior Expectations Users Would Assume

- **Toggles compose independently.** Turning on Feature 1 alone changes the title only. Turning on Feature 2 alone changes the body only. Both on: both change. No hidden coupling.
- **Re-running `describe` is safe.** Nothing gets duplicated, nothing gets wiped, ticked checkboxes survive.
- **The walkthrough is not going away.** Existing PR-Agent value is preserved; the template is additive.
- **Nothing gets published when the tool errors.** Existing PR-Agent already returns `""` from `run()` on exception; keep that behavior. Never publish a partially-formed template.
- **Manual edits win in a conflict.** If the author edits the title or the What/Why section, the tool does not fight them (Feature 1 idempotency; Feature 2 hash marker).
- **Config-off = upstream behavior.** Fork stays mergeable with upstream; teams can adopt piecemeal.

---

## Sources

- `D:\git\pr-agent\.planning\PROJECT.md` — org template, key decisions, GitLab-only scope, config-gated toggles
- `D:\git\pr-agent\pr_agent\tools\pr_description.py` — existing `_prepare_pr_answer`, `generate_ai_title` toggle, `title_to_publish = None` fallback pattern (line 185), body assembly (line 128-131), `use_description_markers` alternate path (line 500)
- Angular Commit Message Conventions (community-maintained spec; type list, `!` breaking marker, revert form) — HIGH confidence, widely-adopted standard
- Existing PR-Agent config pattern (`get_settings().pr_description.<key>`, defaults in `configuration.toml`) — verified in source
