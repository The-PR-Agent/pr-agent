# Phase 4: Expose v1.0 describe toggles via config__* env vars and embed pr_agent:walkthrough into org template - Context

**Gathered:** 2026-07-06
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous — batch grey-area proposals, user-accepted)

<domain>
## Phase Boundary

Two additive capabilities on top of the shipped v1.0 `describe` enhancements, GitLab MRs only,
still config-gated and byte-identical to upstream when the org template is off:

1. **Legacy `config__*` env-var access** for the fork toggles. Operators can enable the fork
   features with `CONFIG__ENABLE_ORG_TEMPLATE=true` / `CONFIG__ENABLE_CONVENTIONAL_TITLE=true`
   (and set `CONFIG__USE_DESCRIPTION_MARKERS=false`) in addition to the existing
   `PR_DESCRIPTION__*` env vars and `[pr_description]` TOML overrides — all of which keep working.

2. **A single org-formatted MR body** when `enable_org_template=true`: the org template renders the
   AI-filled What/Why + Note/Risk, the human checklist, AND a new `## Changes` section carrying the
   PR-Agent file walkthrough + mermaid diagram. A new `enable_pr_agent_output` toggle (default
   `false`) controls whether PR-Agent's own default `## PR Description` summary body ALSO renders
   below the org block.

This revises two Phase 3 locked decisions on purpose (that is the point of this phase):
- Phase 3 kept PR-Agent's default `## PR Description` output rendering unchanged below the org
  block. Now it is suppressed by default (`enable_pr_agent_output=false`).
- Phase 3 kept the walkthrough in PR-Agent's default body below the template. Now the walkthrough +
  diagram are also surfaced inside the org template's `## Changes` section.

When `enable_org_template=false` (the shipped default), behavior stays byte-identical to upstream —
`enable_pr_agent_output` has no effect in that mode.

</domain>

<decisions>
## Implementation Decisions

### config__* env-var access (dual-read)
- Fork toggles stay **defined in `[pr_description]`** in `configuration.toml` — honors the Phase 1
  locked decision; no relocation to `[config]`.
- Reads go through a **dual-read helper**: consult `get_settings().config.get(key)` first, and fall
  back to `get_settings().pr_description.get(key, default)` when the `[config]` value is unset.
  This makes `CONFIG__ENABLE_ORG_TEMPLATE=true` (dynaconf `SECTION__KEY` → `[config]`) flip the
  toggle, because the fork read points check `[config]` first.
- **Full backward compatibility**: existing `PR_DESCRIPTION__*` env vars and `.pr_agent.toml`
  `[pr_description]` overrides continue to work unchanged (they populate the fallback read).
- `use_description_markers` is an **upstream** key read on upstream code paths (the marker-vs-normal
  branch in `run()`), not just by the fork. To let `CONFIG__USE_DESCRIPTION_MARKERS` actually affect
  the branch, planning picks the least-invasive mechanism — preferred: a **load-time mirror** that
  copies an explicitly-set `config.use_description_markers` into `pr_description.use_description_markers`
  so upstream reads see it, avoiding edits to the upstream branch itself. The fork's own consultation
  of this key (`_org_template_active`) also routes through the dual-read helper.

### enable_pr_agent_output toggle
- New fork toggle `enable_pr_agent_output`, added to `[pr_description]`, **default `false`**
  (user's explicit choice — overrides the "default true / byte-identical" recommendation).
- The toggle is **scoped**: it only suppresses output when the org template is active
  (`_org_template_active(git_provider)` true). When the org template is off, PR-Agent output always
  renders regardless of this toggle — preserving the byte-identical-when-off guarantee.
- When active and `false`: PR-Agent's default `## PR Description` summary/type body is **omitted**
  from the published description, leaving the org template (with its embedded `## Changes`) as the
  body. When `true`: PR-Agent's default body renders below the org block as in Phase 3.
- "Suppress the summary body" — labels/type metadata handling stays as-is; this toggle governs the
  description body content, not label publishing.

### Walkthrough + diagram in the org template (embed as copy)
- Add a **`## Changes`** section to `org_template.md` containing the `pr_agent:walkthrough` and
  `pr_agent:diagram` markers (plain-text marker form, same tokens upstream already replaces).
- At assembly, the fork **fills those markers** in the template block with the generated
  `walkthrough_gfm` and `changes_diagram` — reusing the existing `process_pr_files_prediction`
  output rather than recomputing.
- **Copy semantics** (user's choice): the walkthrough/diagram are injected into the template AND left
  in PR-Agent's default body when that body renders. Practical result: with `enable_pr_agent_output`
  default `false`, the walkthrough appears **once** (inside the org template). If an operator turns
  `enable_pr_agent_output=true` alongside the org template, the walkthrough/diagram render **twice** —
  an accepted trade-off of the copy approach.
- The template MUST NOT contain the literal strings `File Walkthrough` or `Diagram Walkthrough`
  (Phase 3 Success Criterion #5 — `process_description` in `utils.py` splits on those). Use a neutral
  header like `## Changes`.

### Claude's Discretion
- Exact name/signature of the dual-read helper and whether `use_description_markers` sync is a
  load-time mirror vs. a fork read-shim — pick the path with the fewest upstream edits.
- Exact `## Changes` header wording and section ordering within `org_template.md` (as long as it
  avoids the forbidden literals and keeps the sentinel block structurally stable).
- Whether marker filling for the template reuses `_prepare_pr_answer`'s existing
  `walkthrough_gfm`/`changes_diagram` values directly or via a small helper.
- Wording of any new `extra_instructions` needed (none expected — walkthrough/diagram already
  generated by existing paths).

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_conventional_title_enabled()` — `pr_agent/tools/pr_description.py:121` — `_is_gitlab_provider(gp)
  and get_settings().pr_description.get("enable_conventional_title", False)`. Route through dual-read.
- `_org_template_enabled()` — `pr_description.py:125` — same shape for `enable_org_template`.
- `_org_template_active()` — `pr_description.py:129` — `_org_template_enabled(gp) and not
  get_settings().pr_description.use_description_markers`. This is the fork's read of
  `use_description_markers`; route through dual-read.
- `_prepend_org_template()` — `pr_description.py:728` — assembles/prepends the sentinel-wrapped org
  block; 4 callers; covered by `tests/unittest/test_org_template_prepend.py`. The `## Changes`
  marker-fill and the `enable_pr_agent_output` suppression slot in around here / the publish seam.
- `load_org_template()` — `pr_description.py:98` — graceful `""` fallback; template body source.
- `_prepare_pr_answer()` — `pr_description.py:830` — produces `walkthrough_gfm`, `pr_file_changes`,
  and builds `pr_body`; `changes_diagram` comes from `self.data.get('changes_diagram')`.
- `_prepare_pr_answer_with_markers()` — `pr_description.py:779` — upstream marker path; shows the
  canonical `body.replace('pr_agent:walkthrough', walkthrough_gfm)` and
  `re.sub(r'...pr_agent:diagram...', ai_diagram, body)` replacement idiom to mirror for the template.
- `get_settings().config.get(...)` — `[config]` section access for the dual-read first hop.

### Established Patterns
- Fork toggles read as `get_settings().pr_description.get(key, False)` (safe default off) — extend
  with the `config`-first dual-read.
- Sentinel-wrapped org block: `<!-- pr_agent:org_template:start -->` / `:end -->`, replaced
  idempotently via `_ORG_TEMPLATE_RE`; keep the block structurally stable when adding `## Changes`.
- Marker replacement idiom: `body.replace('pr_agent:walkthrough', ...)` and the diagram `re.sub`
  handle both plain and `<!-- ... -->` forms.
- Graceful fallback everywhere: on any failure fill the marker with `""` and log, never crash
  `describe`.
- No edits to `pr_description_prompts.toml`; steer via `extra_instructions` / fork-owned files only.
- dynaconf `envvar_prefix=False` + `merge_enabled=True` + `SECTION__KEY` env convention
  (`config_loader.py:12-18`) — `CONFIG__KEY` maps to `[config]`, `PR_DESCRIPTION__KEY` to
  `[pr_description]`.

### Integration Points
- `configuration.toml` `[pr_description]` (lines 129-131, fork block) — add `enable_pr_agent_output=false`.
- `org_template.md` — add the `## Changes` section with `pr_agent:walkthrough` + `pr_agent:diagram`.
- `run()` assembly + publish seam (`pr_description.py` ~242-248 branch and ~288-313 publish) — where
  the org body is assembled, the `## Changes` markers filled, and the default-body suppression applied.
- `config_loader.py` — candidate spot for the load-time `use_description_markers` mirror, if planning
  chooses that mechanism.
- `docs/fork/org-mr-enhancements.md` — document `config__*` env vars and the new toggle.

</code_context>

<specifics>
## Specific Ideas

- Operator-facing target usage (the user's motivating example), legacy `config__*` style:
  ```
  export CONFIG__ENABLE_CONVENTIONAL_TITLE=true
  export CONFIG__ENABLE_ORG_TEMPLATE=true
  export CONFIG__USE_DESCRIPTION_MARKERS=false
  export CONFIG__ENABLE_PR_AGENT_OUTPUT=false
  ```
- Desired default rendered body when `enable_org_template=true`: What/Why → Note/Risk → Checklist →
  `## Changes` (walkthrough + mermaid), with PR-Agent's own `## PR Description` summary suppressed.
- Template must avoid the literals `File Walkthrough` / `Diagram Walkthrough` (Phase 3 SC#5).
- Backward-compat regression bar: `PR_DESCRIPTION__*` env and `.pr_agent.toml` overrides still flip
  the toggles; all-off behavior byte-identical to upstream.

</specifics>

<deferred>
## Deferred Ideas

- "Move" semantics for the walkthrough (strip it from PR-Agent's default body to guarantee no
  duplication when both toggles are on) — user chose copy for v1; move can be revisited if the
  double-render proves annoying.
- Per-section human-edit detection / hash-based preservation of edited What/Risk — still v2 (TMPL-V2-01).
- Extending config__* / org template to GitHub / Bitbucket / Azure DevOps — v2 (PROV-V2-01).

</deferred>
