# Handoff — PR-Agent review-quality work (P0 done, not merged)

Written 2026-09-11 by the session that executed the P0 plan. Read this first, then the ledger files it points to.

## Where things are

| What | Where |
|---|---|
| Worktree with ALL the work | `/Users/samer/dev/pr-agent-p0`, branch `fix/review-p0`, HEAD `bbb14015`, 29 commits on top of `208aec46` (= `fix/review-finding-loss`) |
| Main checkout | `/Users/samer/dev/pr-agent` — used by another session for `feature/pr-dashboard`. Do not switch its branch. |
| Spec (binding) | `docs/superpowers/specs/2026-09-10-review-quality-requirements.md` — weaknesses W1–W12, requirements R-1…R-25, P0 = R-1…R-9 |
| Plan (executed) | `docs/superpowers/plans/2026-09-10-p0-measure-and-fix-review-loop.md` |
| Audit that started this | https://claude.ai/code/artifact/ae4951fa-5af8-47e3-b3ce-3ed1e346b23b (block_rush#1 review: 3/19 findings visible, 4 hedged FPs, 18 misses) |
| Baseline numbers | `tests/eval/BASELINE.md` |
| Every ruling made during execution | `.delegate/work/rulings.txt` (12 lines) and the full SDD ledger `.delegate/work/sdd-progress-final.md` |
| Fleet ledger (sessions, decisions, constraints) | `.delegate/work/p0-review-loop.md`; raw Codex/Cursor run artifacts under `.delegate/runs/` (git-excluded) |
| Memory notes | `~/.claude/projects/-Users-samer-dev-pr-agent/memory/` — `block-rush-review-audit.md`, `codex-astra-adviser.md`, `use-fleet-not-claude-subagents.md` |

Tests: `cd /Users/samer/dev/pr-agent-p0 && PYTHONPATH=. uv run pytest tests/unittest -q -p no:cacheprovider` → 4680+ pass. The one failure, `test_eval_harness.py::test_a_reverted_fix_run_from_inside_the_checkout_is_refused`, is worktree-only (the `.git` file confuses the plain-diff provider); it passes in a plain clone of the branch.

`pr_agent/settings/.secrets.toml` in the worktree is a symlink to the main checkout's file (gitignored). Model key present: `[google_ai_studio]`.

## What P0 shipped (R-1…R-9)

- **R-1** `tests/eval/labels.py`, `labels/block_rush_pr1.json` (29 labels), `run_eval.py --labels/--diff-file/--keep-reviews`.
- **R-2** `pr_agent/algo/run_details.py` `CallRecord` + `run_ledger.py` JSONL when `config.run_ledger_path` set; `chat_completion(..., stage=, files=)` on all handlers.
- **R-3** `pr_agent/algo/review_coverage.py` `CoverageLedger` (line-weighted; clipped=0.5, failed=0); footer + top warning when < 95%.
- **R-4** `render_carried_section`, `append_review_state_paginated`; overflow → one persistent continuation comment (`CARRIED_CONTINUATION_HEADER`), retired in place when empty.
- **R-5** `reconcile_review_findings(..., fully_reviewed_files=)`, state `UNCONFIRMED`, schema v2 (v1 parses).
- **R-6** `same_finding_across_runs`: same path AND start lines within 2 AND (normalized header equal OR Jaccard ≥ 0.5). `ReconciliationResult.current_ids`.
- **R-7** `_review_chunk_plans`: retry → `split_chunk_plan` halves → `fallback_models[0]` → `chunk_failed`. Config `chunk_split_on_failure`, `chunk_fallback_model_on_failure` (default on, only matter when `enable_large_pr_chunking=true`).
- **R-8** `pr_agent/algo/finding_verifier.py` + `settings/pr_finding_verifier_prompts.toml`, behind `enable_finding_verification` (default off). Verdict needs quoted evidence; truncated context downgrades refuted→unverified; tag stored in `issue["verification"]`.
- **R-9** `pr_agent/algo/ship_scope.py`; `get_pr_multi_diffs_with_files(..., diff_files=, preserve_order=)`; `low_priority_globs` default docs/design/mockups/fixtures/md; `[ignore]` proposal in footer.
- Harness: `PlainDiffGitProvider.get_pr_file_content` rebuilds head content from the patch so the verifier has context in evals.

Default-behavior audit (Codex, whole branch): 10 intended changes, 0 accidental after fixes. With stock defaults chunking is off, so R-7/R-9 add no calls.

## Baseline (tests/eval/BASELINE.md) — the honest read

Model `gemini/gemini-3.5-flash`, shared budget `enable_large_pr_chunking=true max_model_tokens=200000 custom_model_max_tokens=1048576 max_number_of_calls=8`, `temperature=0`.

| Row | Calls | Tokens | Files reached | design/** tokens | Matched/23 | Unknown | Control FPs |
|---|---|---|---|---|---|---|---|
| verification off | 5 | 796,752 | 113 | 53% | 0 | 4 | 0 |
| verification on | 5 review, **0 verify (429 quota)** | 791,372 | 178 | 44% | 2 | 4 | 0 |

Caveats that the next session must not paper over: verification was never exercised live (Google quota exhausted mid-run); the two rows differ in chunk packing so the recall delta is noise until repeated; stock defaults (32k clamp, chunking off) reviewed 5/294 files and scored 0 — that is the real out-of-box experience today.

## Open items, in priority order

1. **Re-run the baseline when quota resets** (or with another key/model): `.delegate/runs/task-10/run_baseline.sh <model>` runs both rows and writes `baseline_default.json` / `baseline_p0.json` + ledgers into `.delegate/runs/task-10/`. Run each row twice; R-1 wants ±1 finding agreement. Then update BASELINE.md. Adjudicate the 4 "unknown" findings from the saved raw review (they may be new TPs → add labels).
2. **Stock defaults starve large PRs.** Decide whether `enable_large_pr_chunking` and a higher `max_model_tokens` should default on for models with big contexts; that's a product decision, spec-worthy (would change every user's call count).
3. **R-9 target not met**: design/** still 44–53% of tokens because the budget never bound. Options: summarize low-priority files when they exceed N tokens regardless of budget, or make `[ignore]` acceptance one click. Needs a spec line.
4. **Ledger `run_id` empty under the eval harness** (no PR URL). Small fix in `run_eval.py`/`_review_run_id`.
5. **Merge**: branch `fix/review-p0` → `fix/review-finding-loss` or `main` — user decision. Two docs commits (spec+plan) are on the branch too.
6. **Next tiers** per spec: P1 `/setup` repo profile (R-10…R-15), P2 symbol retrieval (R-16…R-18), P3 lenses/PR-level assessment (R-19…R-22), P4 self-tune (R-23/24), P5 sandbox (R-25). Each needs its own plan, and a BASELINE.md row before merge.

## How the work was run (repeat this)

- Skill flow: `superpowers:subagent-driven-development` with a fresh brief per task, task-scoped review after each, one whole-branch review at the end; rulings recorded, never stalled on questions.
- **User instruction mid-run: use the fleet, not Claude subagents.** Implementation → `~/.claude/skills/fast-wise-delegation/scripts/dispatch.mjs --lane feature` (Cursor); reviews → `--lane debate` (Codex, read-only; final review on `--model gpt-5.6-sol`); rework → `relay.mjs --session <id>` with a delta brief. Briefs live in `.delegate/briefs/`.
- Codex's read-only sandbox cannot run `uv`/pytest; the controller runs the full suite before every commit. Cursor never commits; the controller does, with trailers `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: <url>`.
- Launch long fleet runs with `nohup … &` (the 10-minute shell timeout kills foreground/background Bash) and watch them with a Monitor on the process, not by polling.
- Never two writers on `pr_agent/tools/pr_reviewer.py` at once. Never `cd` before a file-reading command (auto-mode classifier). Symlinking `.secrets.toml` is blocked for the agent; the user must do it.
- The user wants Codex (`gpt-6-astra` via `codex:codex-rescue`, or the debate lane) as adversarial adviser on roadmap/design work; its critiques caught spec-vs-plan conflicts every time.

## Rulings that a future reviewer might question (full text in rulings.txt)

- Cross-run identity uses start-line proximity (≤2) instead of any-range overlap; wording threshold 0.5, not the plan's 0.2.
- Resolution requires the file to be fully reviewed this run; old tests asserting optimistic RESOLVED were changed on purpose.
- Verifier context cap is configurable (`verify_max_context_chars`, 200k) and truncation downgrades "refuted" to "unverified".
- Carried findings paginate to a continuation comment; the plan said truncate; the spec won.
- `preserve_order=True` packs files in priority order but still applies `filter_bad_extensions`.
- Baseline rows share a non-default budget; stated in BASELINE.md rather than comparing to a starved default.
