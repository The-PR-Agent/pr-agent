# Review-quality baseline — block_rush#1 labeled corpus

Corpus: `tests/eval/labels/block_rush_pr1.json` (23 labeled positives: 4 TP, 3 OVERSTATED, 16 MISSED; 4 FP; 2 controls).
Diff: `samer2373/block_rush` PR 1, 294 files, 47,415 diff lines (`tests/eval/fetch_pr_diff.sh samer2373/block_rush 1 <out>`).
Model: `gemini/gemini-3.5-flash` (Google AI Studio), `temperature=0`, no fallback models.
Date: 2026-09-11. Commit: a075838d on `fix/review-p0`.

Every later plan (P1 `/setup`, P2 retrieval, …) adds a row here before merging.

## Budget used for both rows

`--set pr_reviewer.enable_large_pr_chunking=true --set config.max_model_tokens=200000 --set config.custom_model_max_tokens=1048576 --set pr_reviewer.max_number_of_calls=8`.
With stock defaults (`max_model_tokens=32000`, chunking off) the tool reviewed 5 of 294 files in one call and scored 0/23; that is a budget artifact, not a review-loop measurement, so both rows below share the budget above and differ only in the P0 flags.

## Results

| Run | Flags | Model calls | Total tokens | Files reviewed / 294 | Tokens in design/**-only calls | Findings | Matched | Unknown | Control false flags | Precision | Recall | Severity-weighted recall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| defaults + budget | verification off | 5 review | 796,752 | 113 | 419,659 (53%) | 4 | 0 | 4 | 0 | 0.00 | 0/23 = 0.00 | 0.00 |
| P0 flags + budget | `enable_finding_verification=true` | 5 review, 0 verify* | 791,372 | 178 | 348,747 (44%) | 6 | 2 (`ads-show-timeout`, `iap-dup-grant`) | 4 | 0 | 1.00 | 2/23 = 0.087 | 0.152 |

\* Every verifier call failed with HTTP 429 "exceeded your current quota" from Google AI Studio, so the whole-pass guard kept all findings untagged. R-8's acceptance (four labeled FPs refuted, five TPs confirmed) is **not measured** in this baseline. Unknown findings are not counted as false flags; adjudicate them from the raw review (`--keep-reviews` now stores it under `review` in the `--out` JSON).

## What moved and what did not

- Recall moved from 0 to 2/23 between the rows, but the two rows also differ in chunk packing (113 vs 178 files reached the model) and Gemini is not deterministic at `temperature=0`; treat the delta as noise until two same-flag runs agree within ±1 finding (R-1 metric).
- `design/**` still consumed 44–53% of tokens in both rows. Ship-scope ordering (R-9) puts those files last but, with 8 × 200k budget, nothing was cut, so nothing was summarized. R-9's "< 5% on design/**" needs the budget to bind or a `[ignore]` acceptance; the footer now proposes one.
- Coverage: 113 and 178 of 294 files reached a model call; the rest were budget-skipped or chunk-failed. The coverage footer reports this per run; it was not captured here because the run predates `--keep-reviews` on the labels path.
- Zero control false flags in both rows.
- Per-call ledger rows carry an empty `run_id` under the eval harness (no PR URL); fill it from the harness in a follow-up.

## Reproduce

```bash
tests/eval/fetch_pr_diff.sh samer2373/block_rush 1 /tmp/block_rush_pr1.diff
cd "$(mktemp -d)"   # outside any git checkout, see README "enrichment trap"
PYTHONPATH=/path/to/pr-agent uv run --project /path/to/pr-agent python /path/to/pr-agent/tests/eval/run_eval.py \
  --repo-root /path/to/pr-agent --labels /path/to/pr-agent/tests/eval/labels/block_rush_pr1.json \
  --diff-file /tmp/block_rush_pr1.diff --set config.model=gemini/gemini-3.5-flash --set config.temperature=0 \
  --set config.fallback_models=[] --set pr_reviewer.enable_large_pr_chunking=true --set config.max_model_tokens=200000 \
  --set config.custom_model_max_tokens=1048576 --set pr_reviewer.max_number_of_calls=8 \
  [--set pr_reviewer.enable_finding_verification=true] --set config.run_ledger_path=/tmp/ledger.jsonl \
  --keep-reviews --out /tmp/baseline.json
```
