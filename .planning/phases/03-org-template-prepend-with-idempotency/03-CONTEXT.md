# Phase 3: Org template prepend with idempotency - Context

**Gathered:** 2026-07-03
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous — batch grey-area proposals, user-accepted)

<domain>
## Phase Boundary

When `enable_org_template` is on, `describe` prepends the fork-owned org template
(AI-filled "What does this MR do? Why?" and "Note / Risk" sections plus an empty
human checklist) ABOVE PR-Agent's default description output. The upstream
`## PR Description` section and file walkthrough remain intact and unchanged below
the prepended block. Re-running `describe` is idempotent: the org-template block is
not duplicated (HTML-comment sentinels detect and replace it) and human-ticked
checkbox states are preserved verbatim across re-runs.

Covers TMPL-01 through TMPL-09. GitLab MRs only. Default OFF — behavior is
byte-identical to upstream when the toggle is off.

</domain>

<decisions>
## Implementation Decisions

### AI Content Generation (What/Why + Note/Risk)
- The "What does this MR do? Why?" section renders as **markdown bullet points** —
  one bullet per key change, each conveying what changed and why (not a single prose blob).
- Empty / low-signal "Note / Risk" is rendered as `None` under the header (the section
  header always renders; the AI outputs `None` when there is nothing material). This
  keeps the sentinel-wrapped block structurally stable across re-runs. (Resolves the
  STATE.md open gate on empty note_risk handling.)
- The AI is steered to fill the sections via **new YAML keys** (`what_why`, `note_risk`)
  added through runtime `extra_instructions` — NOT by editing
  `pr_description_prompts.toml`. Honors the locked "no shared-prompt-file edits"
  convention established in Phase 2 (extra_instructions is the supported steering seam).
- `what_why` / `note_risk` are **independent AI-generated fields**. PR-Agent's own
  `description` field and its `## PR Description` section stay unchanged and continue
  to render below the org block (TMPL-05). The org template does not summarize or
  reuse PR-Agent's existing description content.

### Idempotency & Assembly
- The sentinel-wrapped org-template block is **prepended at the very top of `pr_body`**,
  above everything PR-Agent emits (including `## PR Description`). Purely additive —
  nothing is removed. (Resolves the STATE.md open gate on insertion point; matches
  Success Criterion #1 "prepended above".)
- Checkbox-state preservation: **before publishing, fetch the current MR description**,
  locate the existing sentinel block, extract its checkbox lines, and re-apply the human's
  ticked states onto the freshly rendered checklist.
- On re-run when sentinels are already present: **replace the whole block with fresh AI
  content** (keeps What/Risk current) while carrying over human checkbox ticks per the rule
  above. Do not leave the old block untouched.
- The existing/published description is read via the **`git_provider`** seam (the same
  interface the rest of the tool uses), not from `self.user_description`.

### Marker-Mode & Robustness
- When `use_description_markers` mode is active AND `enable_org_template` is on: **skip the
  org-template prepend, emit a WARN log, and leave the marker placeholder flow untouched**
  (TMPL-08 / Success Criterion #4). Output is identical to the org template being off.
- AI-filled values are emitted as **YAML block scalars**, and `self.keys_fix` is extended
  with the new keys (`what_why:`, `note_risk:`) so `load_yaml(prediction, keys_fix_yaml=self.keys_fix)`
  does not break on colons/markdown/emojis in the content (TMPL-09 / Success Criterion #3).
- Idempotency sentinels are **HTML comments**:
  `<!-- pr_agent:org_template:start -->` / `<!-- pr_agent:org_template:end -->` — invisible
  in rendered markdown (Success Criterion #1).
- Missing/unreadable org template file: reuse the existing **`load_org_template()` graceful
  path** (logs WARN, returns `""`), so the prepend is skipped rather than crashing `describe`.

### Claude's Discretion
- Exact wording of the `extra_instructions` block that requests `what_why` / `note_risk`.
- Helper-function decomposition (e.g., a `_prepend_org_template()` / checkbox-merge helper)
  vs. inline assembly — pick what reads cleanly and stays close to existing conventions.
- Regex / parsing approach for locating the sentinel block and extracting checkbox lines.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `load_org_template()` — `pr_agent/tools/pr_description.py:75` — reads the fork-owned
  template, graceful `""` fallback on OSError. Defined in Phase 1, currently inert.
- `_ORG_TEMPLATE_PATH` — `pr_description.py:38` — package-relative path to
  `pr_agent/settings/org_template.md` (the fixed template body; headers: "What does this
  MR do? Why?", "Note / Risk", "Checklist" with 3 empty checkboxes).
- `self.keys_fix` — `pr_description.py:162` — list of YAML key hints passed to `load_yaml`;
  extend here for `what_why:` / `note_risk:`.
- `_prepare_pr_answer()` — `pr_description.py:678` — builds `pr_body`; returns
  `(title, pr_body, changes_walkthrough, pr_file_changes)`.
- `_prepare_pr_answer_with_markers()` — `pr_description.py:627` — the marker-mode path to
  leave untouched.
- `_ANGULAR_TITLE_INSTRUCTIONS` — `pr_description.py:65` — Phase 2's pattern for appending
  fork instructions via extra_instructions; mirror this shape for the template keys.

### Established Patterns
- Settings read as `get_settings().pr_description.<key>` with `.get(key, False)` for
  fork toggles (safe default off).
- Runtime steering via `extra_instructions` (Phase 2 decision) — no edits to
  `pr_description_prompts.toml`; per-instance only, do not mutate global settings.
- Graceful fallback: log WARN / return "" or original input, never crash `describe`.
- YAML contract: AI output parsed by `load_yaml(prediction, keys_fix_yaml=self.keys_fix)`.

### Integration Points
- `run()` assembly — `pr_description.py:242-248` — chooses marker vs. normal path and
  builds `pr_body`. The org prepend + marker-skip decision live around here.
- Publish seam — `pr_description.py:288-313` — where `title_to_publish` / `pr_body` are
  finalized and `self.git_provider.publish_description(...)` is called; checkbox-preservation
  fetch of the current description slots in before publish.
- `_prepare_data()` — `pr_description.py:575` — loads `self.data` from the prediction;
  new keys `what_why` / `note_risk` arrive here.

</code_context>

<specifics>
## Specific Ideas

- Sentinel literals are fixed by Success Criterion #1:
  `<!-- pr_agent:org_template:start -->` and `<!-- pr_agent:org_template:end -->`.
- Org template must NOT contain the literal strings `File Walkthrough` or
  `Diagram Walkthrough` (Success Criterion #5 — `process_description` in `utils.py`
  splits on those).
- Verification expects 10+ real-diff fixtures proving `load_yaml` returns a dict with
  `what_why` and `note_risk` keys (Success Criterion #3).

</specifics>

<deferred>
## Deferred Ideas

- Per-section human-edit detection (hash-based preservation of manually edited What/Risk
  content) — v2 (TMPL-V2-01). v1 preserves only checkbox ticks, not free-text edits.
- Extending the org template to GitHub / Bitbucket / Azure DevOps — v2 (PROV-V2-01).

</deferred>
