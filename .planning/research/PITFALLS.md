# Pitfalls Research

**Domain:** PR-Agent `describe` enhancements — Angular-convention title rewrite + org template prepend for GitLab MRs (fork of Qodo Merge, config-gated)
**Researched:** 2026-07-02
**Confidence:** HIGH (verified against `pr_agent/tools/pr_description.py`, `pr_agent/algo/utils.py`, `pr_agent/settings/pr_description_prompts.toml`, `pr_agent/git_providers/gitlab_provider.py`)

## Critical Pitfalls

### Pitfall 1: Adding "What/Risk" fields to the YAML contract breaks `load_yaml`

**What goes wrong:**
The model is asked to emit AI-filled What/Risk sections directly inside the existing YAML response (e.g. new `what_summary:` / `risk_notes:` keys next to `title:` / `description:`). The moment the model writes an unescaped colon, a stray `# ` at the start of a line, an em dash, or a multi-line paragraph without the block-scalar `|`, `load_yaml(self.prediction.strip(), keys_fix_yaml=self.keys_fix)` in `_prepare_data` (pr_description.py:450) fails — or, worse, silently falls through the 7+ fallback strategies documented in `CONCERNS.md` and returns partially-parsed data with the new fields missing. The whole describe flow then returns None (line 112) or publishes an MR description without the org template at all.

**Why it happens:**
`self.keys_fix` is a hardcoded list: `["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]`. Only those exact prefixes get repair when the model writes non-block-scalar values. Any new field like `what_summary:` is not in that list, so a natural-language paragraph with a colon in it (e.g. `Refactor: extracted helper`) will terminate the mapping value early and desync indentation. The prompt template in `pr_description_prompts.toml` already warns "each YAML output should be in block scalar indicator (`|`)" but the model routinely ignores this when new fields don't show it in the example block.

**How to avoid:**
- Do NOT add `what_summary` / `risk_notes` as new top-level YAML fields inside the existing `pr_description_prompt`. Instead, ask the model to fill them into the already-parsed `description:` block scalar, or add them as a separate short LLM call whose failure is isolated.
- If new fields are truly required, extend `self.keys_fix` to include the new key prefixes (`"what_summary:"`, `"risk_notes:"`) so the fallback chain can repair them, AND update the example block in `pr_description_prompts.toml` (both `system` and the `duplicate_prompt_examples` copy in `user`) to show them wrapped in `|` block scalars.
- Never introduce a field whose value is expected to contain markdown headers (`##`), emojis, or checkboxes — those tokens collide with YAML anchors, comments, and directives.
- Run the enhanced prompt against 10+ real diffs and assert `load_yaml(prediction, keys_fix_yaml=self.keys_fix)` returns a dict with all expected keys before shipping.

**Warning signs:**
- Debug log entry `f"Error getting valid YAML in large PR handling for describe {self.pr_id}"` (line 322) or `Empty prediction, PR: {pr_id}` (line 112) starts appearing on MRs that previously succeeded.
- The final MR description contains the org template but the What/Risk headers are empty — indicates parse succeeded but the new keys landed under a swallowed sibling.
- `self.data` after `_prepare_data` is missing `title` or `description` entirely on non-trivial MRs.

**Phase to address:**
Phase where the org template AI-filled sections are wired in. Add golden-fixture tests for `load_yaml` on the new prompt output before shipping the toggle.

---

### Pitfall 2: Re-running `describe` stacks duplicate org templates

**What goes wrong:**
Author runs describe once, MR description now contains the org template plus the walkthrough. A commit is pushed, describe runs again (auto or manual), and the org template is prepended again on top of the previous one. After a few iterations the description has three or four "What does this MR do?" sections, each with slightly different AI-filled content, and the checkboxes reset every time (destroying human ticks).

**Why it happens:**
The existing `_prepare_pr_answer` builds `pr_body` from scratch every run out of `self.data.items()`. It does not read the current MR description to detect prior output. The only existing idempotency mechanism is the `use_description_markers` path (line 125-126), which replaces `pr_agent:type`, `pr_agent:summary`, `pr_agent:walkthrough`, `pr_agent:diagram` placeholders inside `self.user_description` — but that requires the author to seed the description with those markers first, which will not happen for the org template flow. The GitLab `publish_description` (gitlab_provider.py:486-493) does an unconditional `self.mr.description = pr_body; self.mr.save()`, so whatever body is passed overwrites the entire description.

**How to avoid:**
Adopt an HTML-comment marker strategy that survives GitLab's markdown rendering (comments are stripped from view but preserved in the raw source):
- Wrap the org template block with sentinels, e.g. `<!-- pr_agent:org_template:start -->` … `<!-- pr_agent:org_template:end -->`.
- Before assembling the new body, fetch `self.git_provider.get_pr_description(full=True)` (already used in `self.vars["description"]` at line 66 with `full=False`; use `full=True` for idempotency detection).
- If the sentinels exist, extract the human-modified checklist state (preserve checked boxes) and replace only the AI-filled What/Risk sections. If they do not exist, prepend the full template.
- Reuse the existing `PRDescriptionHeader.FILE_WALKTHROUGH` split logic (utils.py:1327-1344, `process_description`) as the reference implementation for regex-based section splitting; do not invent a new parser.
- Never rewrite the checklist portion — only the What/Risk paragraphs. Copy the raw checklist lines forward verbatim.

**Warning signs:**
- MR description length grows monotonically across describe runs.
- Multiple `## Checklist` headings visible in a single MR.
- Previously-ticked checkboxes revert to empty on the second run — this is the highest-signal early indicator.
- Merge conflicts on `.planning`/description-manipulation code when rebasing on upstream (marker names collide with upstream's `pr_agent:*` placeholders).

**Phase to address:**
The same phase that ships the org-template prepend feature. Idempotency is not optional — it must land with the initial toggle, not as a follow-up.

---

### Pitfall 3: AI-generated Angular title is malformed and gets published anyway

**What goes wrong:**
The model produces titles like `Refactor: added new feature.` (invalid type `Refactor`, trailing period, wrong case), `feat(): add pagination` (empty scope parens), `feat(api): ` (empty summary), or `feat(api): add pagination to /users endpoint that reads from the primary database and applies filters` (exceeds 72-char Angular convention). The current code path at pr_description.py:185 does `title_to_publish = pr_title.strip() if generate_ai_title else None` and hands the raw string straight to GitLab. GitLab accepts it and the MR now has a non-conformant title that fails downstream commit-lint / changelog-generation tooling.

**Why it happens:**
Prompt-only enforcement is unreliable for structural constraints. LLMs are trained on massive corpora of non-Angular commit messages; even with `type(scope): summary` in the prompt, temperature > 0 produces drift. There is no Python-side validator between `self.data['title']` (line 458-459) and `self.git_provider.publish_description`. The Pydantic definition in `pr_description_prompts.toml:49` says only `title: str = Field(description="a concise and descriptive title...")` — the description is prose, not a regex.

**How to avoid:**
Add a normalization layer in `_prepare_pr_answer` / `_prepare_pr_answer_with_markers` right after `ai_title = self.data.pop('title', ...)`:
- Regex-validate against `^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9\-\*\/]+\))?!?: [^\s].{1,70}[^.]$` (the trailing `[^.]` rejects trailing periods).
- If validation fails, attempt structural repair (lowercase type, strip trailing period, truncate summary to 72 chars, drop empty `()` scope, add missing `type:` prefix by inferring from `self.data['type']` labels — e.g. "Bug fix" → `fix`, "Enhancement" → `feat`).
- If repair also fails, fall back to `None` (leave GitLab title untouched — same escape hatch already used at line 185 for the "manual edit" case from PR #2474).
- Do the repair inside a helper `_normalize_angular_title(raw: str, pr_type_labels: list) -> Optional[str]` — testable in isolation, avoids further mutating `_prepare_pr_answer`.
- Add unit tests with adversarial fixtures: empty scope, trailing period, wrong case, invalid type, over-length summary, embedded newlines, backticks.

**Warning signs:**
- CI commit-lint failures on merge commits generated from MR titles.
- Trailing periods or capitalized types appearing in the git log after merge.
- MR titles that render as `feat(): summary` in the GitLab UI.
- Emitting `type: description` (colon after type without scope) that some Angular linters reject.

**Phase to address:**
Same phase that introduces AI title rewriting. Ship the validator with the feature, not as a hardening pass.

---

### Pitfall 4: Editing shared prompt files causes upstream merge conflicts on every rebase

**What goes wrong:**
The temptation is to add the Angular convention rules and org-template instructions directly into `pr_agent/settings/pr_description_prompts.toml`. Upstream Qodo Merge modifies this file frequently (see recent commits touching describe). Every upstream rebase will hit conflicts on both `system` and `user` prompts, on the Pydantic definition, on the example block, and on the `duplicate_prompt_examples` fork. Merge-conflict resolution on Jinja2 + TOML + embedded YAML examples is high-effort and error-prone (a lost whitespace flips a `{%- endif %}` block).

**Why it happens:**
Prompt files are the single most-mutated resource in PR-Agent. They double as both configuration and documentation for the model; upstream regularly tunes them for new models. Any inline edit becomes a permanent conflict surface. The `configuration.toml` shows the pattern PR-Agent already uses for optional features — separate `[section]` blocks, feature flags — and mutation of shared prompts is the exception, not the rule.

**How to avoid:**
- Do NOT edit `pr_description_prompts.toml` directly. Instead, add a new prompt section (e.g. `[pr_description_org_prompt]` with `system` and `user` keys) or extend via the existing `extra_instructions` mechanism (already wired into the prompt at pr_description_prompts.toml:11-17 and populated from `get_settings().pr_description.extra_instructions` at pr_description.py:69).
- For Angular title convention: prefer using `extra_instructions` set from a fork-owned config file (`configuration.toml` overlay in `settings_prod/` or a new fork-namespaced key) rather than modifying the prompt system message. It concatenates cleanly and upstream never touches it.
- For the org template body: keep the template as a Python constant or a fork-owned TOML file (e.g. `pr_agent/settings/org_template.toml`), assemble in the Tool code path, and never inject it into the prompt.
- Guard all fork-specific code paths with `if get_settings().pr_description.get("org_template_enabled", False):` so the default upstream flow is byte-identical when toggles are off — reduces conflict surface to just the toggle-reading lines.
- When you must touch `pr_description.py`, prefer adding new methods (e.g. `_prepend_org_template`, `_normalize_angular_title`) rather than modifying `_prepare_pr_answer` — upstream edits to that method conflict with any inline changes.

**Warning signs:**
- `git rebase upstream/main` produces conflicts in `pr_description_prompts.toml` or the middle of `_prepare_pr_answer`.
- More than 10 lines changed in either file for a "small toggle" — indicates coupling that will hurt on the next rebase.
- Fork diverges on `keys_fix` list — upstream will add fields eventually and the resolution is manual.

**Phase to address:**
Phase 1 (foundation / config gating). Establish the "no shared-prompt edits" rule before any feature code lands.

---

### Pitfall 5: Org template collides with PR-Agent's semantic file walkthrough

**What goes wrong:**
The org template is prepended, but the semantic file walkthrough that PR-Agent generates below it either (a) never renders because the description already exceeds GitLab's markdown-render limit for a single note, (b) renders but is duplicated because `_prepare_pr_answer` builds the walkthrough from `self.file_label_dict` regardless of what was already in the description, or (c) the walkthrough's `<details><summary><h3>File Walkthrough</h3></summary>` HTML block gets interpreted as being inside the org template's `## Checklist` section, breaking the collapsible behavior.

**Why it happens:**
The assembly order in `run()` is: `_prepare_pr_answer` returns `pr_title, pr_body, changes_walkthrough, pr_file_changes`, then `pr_body += "\n\n" + changes_walkthrough + "___\n\n"` (line 131) is unconditional when `inline_file_summary` is off. The org-template prepend must happen at the right insertion point — before help text, after the walkthrough but before the "Need help?" footer, OR replace the top of `pr_body` while preserving the walkthrough. Any of the three positions has failure modes. Also, `process_description` in utils.py:1332 uses the exact string `"File Walkthrough"` as the split anchor for re-parsing existing descriptions in other tools (e.g. review) — if the org template accidentally contains that phrase, downstream tools that read description will mis-split it.

**How to avoid:**
- Fix the final assembly order explicitly: `[org_template_with_ai_filled_sections]` + `\n\n___\n\n` + `[existing pr_body from _prepare_pr_answer]` + `[changes_walkthrough]` + `[help text]`. Document the invariant in a comment.
- Never let the org template contain the literal strings `"File Walkthrough"` or `"Diagram Walkthrough"` — they are the sentinel headers used by `process_description` and `_prepare_pr_answer` respectively. The proposed org template uses `📌 What`, `⚠️ Note / Risk`, `✅ Checklist` — safe. Keep it that way.
- When idempotency detection (Pitfall 2) parses the description, use the HTML-comment sentinels, not the section headers. Section headers change if the template gets edited; sentinels are invariant.
- Verify the walkthrough still renders by opening one real MR at each step of implementation. GitLab silently truncates markdown at 1MB, but even at 100KB some rendering behaviors change.

**Warning signs:**
- The `File Walkthrough` collapsible section stops rendering as collapsible (renders as raw HTML/text) — indicates a `<details>` tag got mis-nested.
- `process_description` returns an empty file list when it used to return files — the split key was disturbed.
- The org template appears twice, or the walkthrough appears above the org template.

**Phase to address:**
Phase where the org template prepend is wired in. Add an integration test that runs `describe` end-to-end against a fixture MR and asserts (a) org template exists exactly once, (b) `<details>` for File Walkthrough is present and well-formed, (c) `process_description` still parses out the file list.

---

<!-- gsd:write-continue -->

### Pitfall 6: GitLab title update API quirk trips the fork's own PR #2474 fix

**What goes wrong:**
Team enables `generate_ai_title = true` and the AI-title rewrite. GitLab's API accepts the new title, but if the author manually edited the MR title after opening (common on repushes), the AI overwrites the human's edit on the next describe run. Alternatively, the code passes an empty string instead of `None` when the AI fails to produce a title, and GitLab happily sets the MR title to `""`, which some GitLab versions reject with a 400 and some accept, leaving an untitled MR.

**Why it happens:**
`GitLabProvider.publish_description` (gitlab_provider.py:486-493) checks `if pr_title is not None:` — passing `None` is the correct way to leave title untouched. But `pr_title.strip()` on an AI response like `"\n"` yields `""` (empty string, not None), which bypasses the None check and sets `self.mr.title = ""`. The fork's existing fix (line 185 in pr_description.py) handles the `generate_ai_title = False` case but does NOT handle the "AI returned empty/whitespace title" case.

**How to avoid:**
- After Angular-normalization (Pitfall 3), also check `if not title_to_publish or not title_to_publish.strip(): title_to_publish = None` before passing to `publish_description`.
- For the "human edited title mid-flow" case: compare the current `self.git_provider.pr.title` (already captured in `self.vars["title"]` at line 64) against the previously-published title (fetch from the persistent-comment history or from a marker in the description body). If they differ and the human is likely the editor, skip title update. Simplest safe policy: only rewrite the title if it matches the git branch's default GitLab title (`Draft: <branch-name>` or `<first-commit-subject>`) — indicating no human edit has happened yet.
- GitLab description size limit is 1MB. Assemble length check: if `len(pr_body.encode('utf-8')) > 900_000`, log a warning and truncate the walkthrough section (not the org template) before calling `publish_description`.
- Emoji rendering: the org template uses `📌`, `⚠️`, `✅`. GitLab renders these fine but some corporate GitLab installs (especially self-hosted with old markdown pipelines) may render them as raw unicode escapes. Test on the target GitLab instance — do not assume GitLab.com behavior.

**Warning signs:**
- MRs with empty titles or titles identical to the branch name after describe runs.
- Author complaints that their manual title edits get reverted.
- Description body truncated at exactly ~1MB (silent drop of walkthrough content).
- Temporary comment `"Preparing PR description..."` (line 102) never gets removed — indicates `remove_initial_comment()` was skipped due to an exception; check the GitLab notes list for orphaned temp comments.

**Phase to address:**
Phase where title rewrite ships. The empty-string guard is a one-liner; the "was manually edited" heuristic can slip to a follow-up phase if it delays the initial ship.

---

### Pitfall 7: Toggles ship ON by default, surprising existing fork users

**What goes wrong:**
`configuration.toml` gets `generate_angular_title = true` and `prepend_org_template = true` shipped as defaults. Existing internal users who pulled the fork before these features land suddenly see all their MR titles rewritten and descriptions rewritten with the org template — including on old MRs where they had carefully crafted the description manually. Backlash, rollback, feature disabled in prod.

**Why it happens:**
The fork's existing config pattern (`get_settings().pr_description.<key>`) reads a single shared TOML — there's no "opt-in" distinction. Also, TOML multi-line strings for the org template are easy to escape wrong: single-quoted `'''...'''` preserves literal backslashes but forbids embedded single quotes; double-quoted `"""..."""` requires escaping backslashes and `\n` becomes a real newline. The Angular type list is also tempting to inline in TOML as `angular_types = ["feat", "fix", ...]` — safe — but the template body is a multi-line string with emojis, checkboxes, and `<!--` comments, which is where the escaping traps live.

**How to avoid:**
- Ship defaults as `false`. Document the config keys in a fork-specific README section. Announce the opt-in explicitly to the team; only flip to `true` after 2-3 pilot MRs succeed.
- For the org template in TOML, prefer triple-single-quoted literals: `org_template = '''<!-- pr_agent:org_template:start -->\n## 📌 What does this MR do? Why?\n...'''`. In TOML `'''...'''` are multi-line literal strings — no escape processing, so `\n` stays as backslash-n (bad — you want actual newlines). Correct choice: triple-double-quoted `"""..."""` with real newlines in the file:

  ```toml
  org_template = """
  <!-- pr_agent:org_template:start -->
  ## 📌 What does this MR do? Why?
  ...
  <!-- pr_agent:org_template:end -->
  """
  ```

  This preserves emojis and checkboxes literally.
- Alternative and safer: store the template in a separate file (`pr_agent/settings/org_template.md`) and load it via `Path(...).read_text(encoding='utf-8')` in the tool. No TOML escaping at all, and diff-friendly.
- Validate at startup: when the feature toggles are on but the template is empty or missing sentinels, log a warning and disable the feature for the run rather than publishing a broken description.

**Warning signs:**
- First MR after upgrade has the org template but with visible `\n` characters or missing emojis — indicates TOML escape processing corrupted the string.
- Users report "why did my description change without me changing settings" — defaults shipped as true.
- Config-diff on rebase touches the same TOML block upstream added something to — indicates the fork is layering config into upstream files instead of a separate overlay.

**Phase to address:**
Phase 1 (config gating). Establish default=false and the template-in-separate-file pattern before feature code lands.

---
<!-- gsd:write-continue -->
