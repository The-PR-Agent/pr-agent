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
- Per-call ledger rows carry an empty `run_id` under the eval harness (no PR URL). **Fixed** (00fcd5f1): rows now take `config.run_ledger_run_id`, which `run_eval.py` fills with `eval:<labels-stem>:<utc-timestamp>`, or a per-run `local-<hex>` id.

## 2026-09-11: repetition attempt, and what changed since these rows

The repetition R-1 asks for (two same-flag runs agreeing within ±1 finding) **has not been run**. The 2026-09-11 attempt hit the Google AI Studio free-tier request quota — `429 RESOURCE_EXHAUSTED ... generate_content_free_tier_requests, limit: 20` — after a single model call. Those four rows are quarantined under `.delegate/runs/task-10/failed-quota/` with the error text; they are not data. Repeating this needs a paid key or another provider.

Two default/behavior changes landed after the rows above, so **both rows are now stale as a description of stock behavior**, and neither has a row of its own yet:

- `config.max_model_tokens` 32,000 → 200,000 and `pr_reviewer.enable_large_pr_chunking` false → true. The starved-default runs in `.delegate/runs/task-10/run1_starved_*.json` are what the old defaults produced: 1 and 4 findings respectively, 0 of them matching a label, recall 0.00.
- `pr_reviewer.low_priority_max_tokens_per_file` (new, default 3,000) summarizes a low-priority file whose patch exceeds it regardless of whether the budget binds, on both the single-call and chunked paths. This is the R-9 fix for `design/**` taking 44–53% of tokens; **the target is not yet demonstrated** — it needs a row measuring the design/** token share with the cap on.

## 2026-09-11: the Cursor CLI model line (4 rows)

The Gemini free tier stayed spent, so these rows ran through the Cursor CLI instead:
`.delegate/runs/task-10/cursor_openai_shim.py` serves `/v1/chat/completions` and pipes each
prompt to `cursor-agent -p --output-format json --mode ask --sandbox enabled`, model
`gemini-3.7-flash-high`. **This is a separate model line. Do not compare these rows to the two
`gemini-3.5-flash` rows above** - different model, different provider path, different prompt
adherence. They are internally comparable only.

Two properties were verified before the rows ran, because either would have made them worthless:

- **Prompt fidelity.** argv cannot carry an 800KB prompt, so the prompt goes on stdin. stdin was
  measured to deliver 313k tokens intact on a 1M-context model; a smaller-context model silently
  truncates (a 960KB prompt arrived as 17.9k tokens on `composer-2.5`). Do not point the shim at
  a small-context model.
- **No corpus contamination.** With an empty workspace the agent still read local files through
  its tools. `--sandbox enabled` blocks shell and network, and `samer2373/block_rush` is not
  checked out on this machine, so the PR under review is unreachable. The `--mode ask` agent
  narrates before answering; the shim returns the fenced block when there is one, which is
  format normalization - content is never edited. Every prompt and raw result is dumped to
  `.delegate/runs/task-10/raw/`.

| row | flags | calls | prompt tokens | findings | matched | precision | recall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `old-default` | 32k clamp, chunking off | 2 | 110,357 | 0 | - | 0.00 | 0.000 |
| `new-default` | 200k clamp, chunking on | 4 | 890,093 | 1 | 0 | 0.00 | 0.000 |
| `p0-cap` | p0 flags, cap 3,000 | 3 | 446,885 | 1 | `test-debug-leftovers` | 1.00 | 0.043 |
| `p0-nocap` | p0 flags, cap disabled | 7 | 1,002,748 | 2 | 0 | 0.00 | 0.000 |

"p0 flags" is not `local_profile.toml`: the runner sets that file's structural keys
(`num_max_findings=12`, chunking, the three `require_*` toggles, finding verification) but not
its `extra_instructions` block, and runs at `temperature=0` rather than the file's `0.1`. That
prompt block plausibly moves finding counts, so these rows do not measure the profile as written.
`old-default` is likewise not a pure pre-change picture - `low_priority_max_tokens_per_file` sits
at its new default of 3,000 there, which is inert at a 32k clamp but was not reverted.

Adjudication of the three findings the scorer left `unknown` (raw text in `raw/`, reports in
`cursor_*.json`): none of them changes a score.

- `new-default`'s `lib/src/shell/shop_screen.dart:134-142` "Ownership Detection Bug" is a
  different defect from either labeled item in that file (`shop-nonnotifying-provider` at 31-98,
  `store-flow-triplicated` at 145-161). Out-of-label, so unmatched; whether it is real was not
  verified, because the corpus repo is deliberately not on this machine.
- Both `p0-nocap` findings are in `design/_s_play.html`. No label lives under `design/**`.

### R-9a (the per-file cap) - acceptance met

R-9's acceptance is a **`design/**` token share below 5%**, not a token total. `p0-cap` and
`p0-nocap` are the same flags with only `low_priority_max_tokens_per_file` changed, so the
comparison is clean. Share computed by attributing each file's patch tokens (o200k_base, the
tokenizer the run used) to the files that actually reached a model call, per the `files` field
of each ledger row:

| row | files reached | patch tokens reaching a call | `design/**` | all low-priority |
| --- | --- | --- | --- | --- |
| `p0-cap` | 172 | 304,120 | **0 (0.0%)** | 8,932 (2.9%) |
| `p0-nocap` | 190 | 743,370 | 414,976 (55.8%) | 448,182 (60.3%) |

0.0% against a 5% bar, from 55.8% uncapped. This is arithmetic over the diff and the ledger, not
a sample of model behaviour, so it does not depend on n or on model nondeterminism - which is
why it, and not the finding counts, is what settles R-9a. The 55% drop in total prompt tokens
(446,885 vs 1,002,748, 3 calls vs 7) is the same fact seen from the budget side.

`new-default` also shows 0.0%, because the cap ships as a stock default.

Both excluded files were reported in the coverage ledger and the review footer
(`design/*.html` ... `(low-priority file, not reviewed)`), so nothing was dropped silently.

Observed but **not** part of the acceptance: `p0-nocap`'s only two findings were both in
`design/_s_play.html`, while `p0-cap`'s one finding matched a labeled defect. That is a 1-vs-2
delta at n = 1, which this corpus says to treat as noise until R-1's repetition agrees within
+/-1.

### R-9b (the raised defaults) - measured, and the honest reading

`old-default` is the pre-change stock: on a 2.4MB diff the 32k clamp produced a well-formed
review with `key_issues_to_review: []` - the raw model output is in `raw/`, so this is a genuine
empty review, not a parse failure. `new-default` reaches the whole PR in 2 merged chunks and
reports a finding. The defaults no longer starve a large PR, which is what the change claimed.

What it does **not** show is a recall improvement: the new default's one finding is out-of-label,
so both default rows score recall 0.000.

### What these rows do not establish

- **n = 1 per config.** R-1's repetition (two same-flag runs agreeing within +/-1 finding) is
  still unrun. A one-finding difference is inside the noise these rows cannot measure.
- **Absolute recall is poor on this model line** - the best row found 1 of 23. Findings-per-run
  is low across all four rows, which points at the prompt/provider path, not at the two knobs
  under test. That is the next thing worth investigating, ahead of more knob tuning.
- Finding verification was on for both p0 rows; its contribution was not isolated.

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
