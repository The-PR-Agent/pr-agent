# Work plan — everything left from the original ask

Resume point for a cleared session: read this and `HANDOFF.md`, then start at Step 1.

**Repo:** `/Users/samer/dev/pr-agent-p0` (worktree, branch `fix/review-p0`; `main` is
fast-forwarded to it locally, **not pushed**, 44 ahead of `origin/main`).
**Spec:** `docs/superpowers/specs/2026-09-10-review-quality-requirements.md` (R-1…R-25).
**Measured state:** `tests/eval/BASELINE.md`. **Corpus:** `tests/eval/labels/block_rush_pr1.json`
— 23 expected-positive labels, 2 controls, on `samer2373/block_rush#1`.

## Where this stands

Built and accepted: R-1 (corpus + eval harness), R-2 (per-call token ledger), R-3 (coverage
ledger), R-4…R-8 (render every finding, evidence-based resolution, cross-run identity, bounded
recovery, premise verification), R-9/R-9a/R-9b (ship scope, per-file cap, defaults).

**The tool finds 1 of 23 labeled defects.** That single number governs the whole plan: every
tier below is a recall or precision play, and none of them can be evaluated while the ceiling
is 1/23. Two candidate causes are already eliminated, in writing, in BASELINE.md:

- *not coverage* — all 20 labeled defect files reach a model call in every row;
- *not chunk size* — 11 chunks / 14 calls at a 40k clamp produced the same 3 findings and the
  same single match as 2–3 chunks at 200k.

## Existing parts to build on, not around

`pr_agent/algo/repo_context.py` (config `repo_context_files`, per-PR and per-process caches) —
the seed for R-10/R-11. `model_routing.py` — the seat for R-12/R-18. `finding_verifier.py` —
R-8, extend for R-18. `prompt_fragments.py` and `skills_loader.py` — where lens prompts belong.
`ship_scope.py`, `review_coverage.py`, `run_ledger.py` — the accounting the metrics come from.
Commands register in `pr_agent/agent/pr_agent.py::command2class`.

---

## Step 1 — Recall. Blocks everything else.

**Hypothesis.** `pr_agent/settings/pr_reviewer_prompts.toml:123` describes the findings field as
`"A concise list (0-{{ num_max_findings }} issues) … Only include issues you are confident
about."` The *count* interpolates; the word **concise** and the confidence gate do not. Every
row lands at 1–3 findings regardless of budget, chunking or cap — which is what that sentence
asks for.

**Tasks.**
1. Three prompt variants, one eval row each, identical model/flags/seed:
   `A` control (today's wording); `B` drop "concise", keep the confidence gate;
   `C` drop both, replace with an explicit completeness instruction ("report every defect you
   can evidence; an empty list only if there are none").
2. Two reps per surviving variant — R-1's ±1 agreement band. Finding counts on this model line
   are known unstable (1 vs 3 across reps), so a single row decides nothing.
3. Watch the controls: `control-record-run`, `control-board-origin` must stay unflagged. A
   variant that buys recall with false flags is a regression, not a win.
4. If all three variants sit at 1–3 findings, the prompt is exonerated and the next suspect is
   the **schema**: R-22 server-side structured output, since free-form YAML lets the model stop
   early. Jump to Step 5's R-22 before P1.

**Acceptance.** A variant with recall > 0.043 at equal or lower budget and zero control false
flags, reproduced within ±1 finding across two reps, plus a BASELINE.md row.
**Metric.** Recall, severity-weighted recall, control false-flag rate, prompt tokens per finding.
**Risk.** Wording that inflates finding counts by lowering the confidence bar — which is why the
control rows and precision are part of the acceptance, not an afterthought.

## Step 2 — Deep web research. Do it before designing P2/P3.

Not yet done at all, and it is cheap next to building the wrong lens. Target: how CodeRabbit and
peers actually get recall (retrieval strategy, lens decomposition, per-finding verification,
structured decoding, cheap-model routing); published evaluation methodology for review tools;
tree-sitter indexing practice for Dart.

**Tasks.** Delegate breadth-first (read-only agents, cheap tier, one question each, ~1-paragraph
hand-backs). Write findings into the spec's *Weaknesses measured* section with citations, and
amend R-16/R-19/R-22 before implementing them.
**Acceptance.** Spec amendments citing sources; at least one design decision in P2/P3 changed or
explicitly confirmed by what was found.

## Step 3 — P1 `/setup` repo profile (R-10…R-15). The user's own idea.

**Goal.** The tool understands the business, stack, architecture, conventions and risk surface of
a repo before it reviews a line.

**Tasks.**
1. **R-10** `/setup` command (new tool class + `command2class` entry) producing
   `.pr_agent/repo_profile.md` **as a PR**: stack and versions, architecture map, shipped vs
   unshipped paths, domain glossary and business invariants (mined from doc comments, `docs/`,
   decision files, test names), risk map (path glob → tier), conventions (lint config,
   `AGENTS.md`/`CLAUDE.md`/`.cursorrules`, merged-PR review threads), toolchain commands. Every
   mined statement carries provenance (file, line or PR) and a source hash.
2. **R-11** inject profile + system prompt **first** and byte-stable, per-PR content last, so the
   provider can cache the prefix. Verify with the `cached_tokens` column already in the ledger.
3. **R-12** `[pr_reviewer.risk_tiers]` drives per-chunk model selection and context depth through
   `model_routing.py` — high-risk paths get the strong tier and verification, low-risk get the
   weak tier or a skip.
4. **R-13** reactions, maintainer replies and "resolved without a code change" produce *proposed*
   rules with provenance and scope glob, requiring approval. A 👎 creates a proposal, never an
   active suppression.
5. **R-14 — security, not polish.** Mined repo text is **data**. It cannot suppress a check or
   run a command. Global profile bounded by `repo_context_max_lines`; path-specific sections
   retrieved per file. Test: an instruction in a PR comment or source file saying "skip security
   review" has no effect.
6. **R-15** GitHub App `installation.created` opens the setup PR; unit-tested against a fixture
   payload.

**Acceptance.** Generated profile for block_rush names Riverpod and in_app_purchase, marks
`design/**` unshipped and `lib/src/iap/**` high risk; two consecutive reviews of different PRs
show an identical prompt prefix up to the diff; the R-14 injection test passes.
**Metric.** `cached_tokens` share; serious-defect recall per dollar versus all-cheap and
all-strong baselines (this is the "cheap model finds hard issues" number).

## Step 4 — P2 context beyond the diff (R-16…R-18).

1. **R-16** tree-sitter index for **Dart only** to start: per changed symbol, definition +
   callers + callees inside a reserved token cap, package-qualified, generated code skipped,
   traversal depth capped.
2. **R-17** when a sandbox is available, run the repo's analyzer and feed results as verification
   targets; dedupe findings the linter already reports. Ledger stage `static`.
3. **R-18** findings in high-risk paths touching ordering, concurrency or money escalate to the
   strong tier for verification.

**Acceptance.** The ads load-vs-show timeout asymmetry is visible in retrieved context for
`ads_service.dart`; `dart analyze` findings appear in the ledger as stage `static`.
**Metric.** Cross-file FP rate and serious-defect recall at fixed budget. R-16 is the single
most expensive item here — gate it on Step 2's research and on the corpus showing the gain.

## Step 5 — P3 lenses and PR-level view (R-19…R-22). Answers "all logical and business issues".

**One lens at a time, each kept only on measured corpus gain at fixed budget.** Order:
business-logic/invariants → async/lifecycle → persistence/atomicity → security/trust-boundary →
test quality. The corpus misses are concentrated in exactly these categories (IAP grant
duplication, starter-guard shape, save atomicity, missing verification, run identity), so this
is where recall should move if anywhere.

- **R-20** "No security concerns" may only be emitted when the security lens ran over **all**
  high-risk files and returned none; otherwise "not assessed".
- **R-21** PR-level assessment: 3–5 bullets on architecture, state boundaries, test hygiene,
  documentation drift, with file anchors. Acceptance: block_rush replay produces a bullet about
  the non-notifying service-holder providers.
- **R-22** server-side structured output (`response_format` / tool-call schema) where the
  provider supports it. Promote this ahead of the lenses if Step 1 exonerates the prompt.

**Metric.** Recall delta per lens at fixed budget; completion tokens per finding for R-22.

## Step 6 — P4 self-improvement (R-23, R-24).

**R-23** outcome capture: reactions, replies, and whether flagged lines changed before merge,
stored per finding as weak labels, with preference feedback kept separate from correctness.
**R-24** shadow self-tune: proposed config changes (ignore globs, path instructions, lens
weights, tiers) run in shadow against the corpus with repo **and time** holdouts, applied only
on measured gain, rollback kept, and **suppression never inflating reported precision**.

Build this last. A self-tuner on a 1/23 baseline optimises noise.

## Step 7 — P5 hardening (R-25).

No repo code executes outside an isolated VM; comment-driven config changes limited to an
allow-list.

---

## Rules that carry over

- **Never two writers on `pr_agent/tools/pr_reviewer.py`.**
- Do not switch the branch of `/Users/samer/dev/pr-agent` — another session owns it on
  `feature/pr-dashboard-s3-s4-s5`.
- No secrets in the tree; env vars or the gitignored `.secrets.toml` only. **The Cursor key used
  for the baseline was pasted into a chat transcript and needs rotating.**
- Never `cd` before a file-reading command; pass absolute paths.
- Gates before every commit: `PYTHONPATH=. uv run pytest tests/unittest -q -p no:cacheprovider`
  and `uv run ruff check`. One failure is pre-existing and unrelated (worktree-only):
  `test_eval_harness.py::test_a_reverted_fix_run_from_inside_the_checkout_is_refused`.
- **A behaviour change with no BASELINE.md row is a hypothesis, not an acceptance.** Prefer a
  deterministic metric (token share, coverage) over a finding count, which this model line does
  not reproduce within ±1.
- Reviews go to Codex (`codex:codex-rescue`) — it found two real defects in the per-file cap that
  the whole test suite missed.

## Running an eval row

Gemini's free tier is spent; rows go through the Cursor CLI shim.

```bash
CURSOR_API_KEY=... python3 tests/eval/cursor_openai_shim.py --port 8899 \
  --model gemini-3.7-flash-high --raw-dir /tmp/cursor-raw &
```

Then `--set config.model=openai/cursor-gemini-3.7-flash-high`,
`--set openai.api_base=http://127.0.0.1:8899/v1`, `--set openai.key=cursor-cli-shim`,
`--set config.custom_model_max_tokens=1048576`, and always `--keep-reviews`.

The model **must** have a 1M context — a smaller one truncates long prompts with no error.
Runner examples live in `.delegate/runs/task-10/` (git-excluded). Adjudicate `unknown` findings
against the labels before quoting any score.
