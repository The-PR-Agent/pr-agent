# Handoff — PR-Agent review-quality work (P0 done, not merged)

Written 2026-09-11 by the session that executed the P0 plan. Read this first, then the ledger files it points to.

## Where things are

| What | Where |
|---|---|
| Worktree with ALL the work | `/Users/samer/dev/pr-agent-p0`, branch `fix/review-p0`, HEAD `49857ec2`, 31 commits on top of `208aec46` (= `fix/review-finding-loss`) |
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

## Session 2 addendum — Codex review of the session-2 diff

Adversarial read-only pass (Codex, `task-mtx02yzm-ibsh0k`) over `bbb14015..bcce8ebe` found two
real holes in the per-file cap; both verified against the file, fixed and tested in `fb543d32`:

1. A PR of only oversized low-priority files capped to an empty diff → `prediction is None` →
   `run()` returned before publishing anything, so the PR was silently skipped and the coverage
   ledger that recorded the exclusion never reached a reader. `cap_low_priority_files` now
   reviews the files when capping would empty the review.
2. The chunked path's failure rollback cleared `_ship_scope_summary_paths`, erasing the capped
   files' footer lines even though the single-call result it rolls back to also excludes them.
   The previous paths are restored with `previous_coverage`.

`9b274152` also closed the untested `len(plans) < 2` fallback and caches the cap result per
attempt. Suite: 4692 pass, 1 pre-existing worktree-only failure
(`test_a_reverted_fix_run_from_inside_the_checkout_is_refused`); ruff clean.

## Session 2 (2026-09-11 16:15-16:40) — what changed

Tests: 4689 pass, ruff clean; the one failure is still the worktree-only `test_eval_harness.py::test_a_reverted_fix_run_from_inside_the_checkout_is_refused`.

- **Item 4 done** (`00fcd5f1`): ledger rows get a run id even without a commit URL — `config.run_ledger_run_id`, else a per-reviewer `local-<hex>`; `run_eval.py` fills `eval:<labels-stem>:<utc-timestamp>` on its own. Verified live: the one call that got through wrote `run_id=baseline:rep1:default`.
- **Items 2 and 3 done** (`49857ec2`), as the user's call, with spec amendments R-9a/R-9b: `config.max_model_tokens` 32000→200000, `pr_reviewer.enable_large_pr_chunking` false→true, and a new `pr_reviewer.low_priority_max_tokens_per_file` (3000) that summarizes an oversized low-priority file regardless of budget on **both** the single-call and chunked paths (`get_pr_diff` now takes `diff_files`). Docs and the chunking-default tests were updated with them.
- **Item 1 still blocked, and now with the exact reason.** Four rows (rep1/rep2 × default/p0) ran at 16:25 and died on `429 RESOURCE_EXHAUSTED — generate_content_free_tier_requests, limit: 20` after a single model call. They are quarantined with the error text and a README in `.delegate/runs/task-10/failed-quota/` — **they are not data**. `run_baseline_rep.sh <model> <rep>` (new, next to `run_baseline.sh`) runs one repetition to `baseline_<rep>_{default,p0}.json` and now tees a full `row_<rep>_*.log`; the old runner's `| tail -15` is what hid the 429s for four rows.
- **BASELINE.md** carries all of this: the two existing rows are stale as a description of stock behavior, and neither new change has a row.

Repeating the baseline needs a paid Google key or another provider — nothing else blocks it.

## Open items, in priority order

1. **Re-run the baseline with a key that has quota** (free tier is spent; see Session 2) (or with another key/model): `.delegate/runs/task-10/run_baseline.sh <model>` runs both rows and writes `baseline_default.json` / `baseline_p0.json` + ledgers into `.delegate/runs/task-10/`. Run each row twice; R-1 wants ±1 finding agreement. Then update BASELINE.md. The 4 "unknown" findings from the first baseline **cannot** be adjudicated offline: those runs predate `--keep-reviews` on the labels path, so `baseline_*.json` holds only the count. Re-run with `--keep-reviews` (both runners pass it) and adjudicate from `review` in the `--out` JSON before scoring, so labels are not added after the run they are scored against.
2. ~~**Stock defaults starve large PRs.**~~ **Done** (`49857ec2`) — needs a BASELINE row on the new defaults.
3. ~~**R-9 target not met**~~ **Partly done** (`49857ec2`, the per-file cap) — the < 5% design/** target is still unmeasured.
4. ~~**Ledger `run_id` empty under the eval harness**~~ **Done** (`00fcd5f1`).
5. **Merge**: held until the baseline is verified (user's call, 2026-09-11). Then `fix/review-p0` → `fix/review-finding-loss` or `main`. Two docs commits (spec+plan) are on the branch too.
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
