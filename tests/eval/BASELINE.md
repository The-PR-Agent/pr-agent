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
`tests/eval/cursor_openai_shim.py` serves `/v1/chat/completions` and pipes each
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

### R-1's repetition, run at last - and what it retracts

Both verdict rows were repeated (`p0-cap-rep2`, `p0-nocap-rep2`, same flags, same shim):

| row | calls | prompt tokens | findings | matched | precision | recall | `design/**` share |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `p0-cap` | 3 | 446,885 | 1 | `test-debug-leftovers` | 1.00 | 0.043 | 0.0% |
| `p0-cap-rep2` | 5 | 502,981 | 3 | `test-debug-leftovers` | 1.00 | 0.043 | 0.0% |
| `p0-nocap` | 7 | 1,002,748 | 2 | - | 0.00 | 0.000 | 55.8% |
| `p0-nocap-rep2` | 6 | 998,012 | 1 | `test-debug-leftovers` | 1.00 | 0.043 | 55.8% |

What replicates and what does not, and this is the point of running it:

- **The `design/**` share and the budget do.** 0.0% / 0.0% capped against 55.8% / 55.8%
  uncapped; ~450-500k prompt tokens capped against ~1.00M uncapped. Both are functions of the
  diff, not of the model, so R-9a's acceptance holds on both reps.
- **Finding counts do not.** `p0-cap` gave 1 then 3, `p0-nocap` 2 then 1 - a swing of 2 within
  a config, outside R-1's +/-1 band. Finding counts on this model line are not yet a measurable
  quantity, and no conclusion here rests on one.
- **The rep-1 observation is retracted.** Rep 1 showed the uncapped run's only findings in
  `design/_s_play.html` and the capped run's one finding matching a label, which looked like
  the cap improving finding quality. It did not replicate: `p0-nocap-rep2` produced exactly the
  same matched label as both capped rows. The cap's demonstrated effect is on budget and share,
  not on what the model finds.
- `test-debug-leftovers` is the only label matched in any row, in 3 of the 4 p0 rows. Recall is
  0.043 wherever anything matched.

### R-9b (the raised defaults) - measured, and the honest reading

`old-default` is the pre-change stock: on a 2.4MB diff the 32k clamp produced a well-formed
review with `key_issues_to_review: []` - the raw model output is in `raw/`, so this is a genuine
empty review, not a parse failure. `new-default` reaches the whole PR in 2 merged chunks and
reports a finding. The defaults no longer starve a large PR, which is what the change claimed.

What it does **not** show is a recall improvement: the new default's one finding is out-of-label,
so both default rows score recall 0.000.

### What these rows do not establish

- **Finding counts are not stable.** R-1's repetition was run (above) and the two same-flag
  pairs disagree by up to 2 findings. Treat any conclusion resting on a finding count as unmade;
  the claims here rest on the `design/**` share and the budget, which are deterministic.
- **Chunk size is not the lever - tested and refuted.** The obvious reading of the low recall
  was that findings collapse when a chunk is ~200k tokens. A `p0-smallchunk` row
  (`config.max_model_tokens=40000`, `max_number_of_calls=20`, cap on) split the same PR into
  **11 chunks / 14 calls** instead of 2-3, at a comparable total budget (530,897 prompt tokens
  against 446,885). Result: **3 findings, recall 0.043, the same single matched label** - inside
  the 1-3 findings every other p0 row produced. Shrinking the context 5x changed nothing, so
  context length is not what limits findings-per-run.
  What that leaves, untested: the prompt itself. The schema field is described to the model as
  "A concise list (0-3 issues)" even when `num_max_findings` is 12 (the count interpolates, the
  word "concise" does not), the review prompt says "Only include issues you are confident
  about", and these rows omit `local_profile.toml`'s `extra_instructions`. That is where to look
  next.
- **Absolute recall is poor on this model line** - the best row found 1 of 23. Findings-per-run
  is low across all four rows, which points at the prompt/provider path, not at the two knobs
  under test. That is the next thing worth investigating, ahead of more knob tuning.
- Finding verification was on for both p0 rows; its contribution was not isolated.

## 2026-09-11: Step 1 prompt-wording experiment - pre-registration

**Written before the rows ran.** Nothing below is a result; it is the rule the results will be
read against, recorded in advance so a marginal row cannot be talked into being a win.

**Question.** Does the key-issues field wording cap recall? `pr_reviewer_prompts.toml` asked for
"A concise list ... Only include issues you are confident about", and every row so far lands at
1-3 findings regardless of budget, chunking or cap - which is what that sentence asks for.

**Design.** The sentence moved to `prompt_fragments.findings_field` (commit `4e3f55a5`) so a
variant is a `--set` override and the tree under test is identical across rows. Three variants,
two reps each, one flag apart, run by `.delegate/runs/task-11/run_prompt_variants.sh`:

| Variant | Wording |
|---|---|
| A | today's wording - control, the shipped fragment, no override |
| B | "A concise list" -> "A list"; confidence gate kept, everything else byte-identical |
| C | no brevity cue, no confidence gate; "report every defect you can evidence ... an empty list is correct only if there are no defects"; the evidence and trigger-scenario requirements kept |

`(0-{{ num_max_findings }} issues)` is byte-identical in all three. The cap is enforced in code as
well as in the prompt (`pr_reviewer.py`, `review_merge.py`), so dropping it from one variant would
change the cap and not just the wording. All rows run at `num_max_findings=12`, where the cap does
not bind at these finding counts.

**Decision rule (pre-registered).** A variant wins only if it matches **>= 4 of the 23 labels on
both reps** with **zero control false flags** (`control-record-run`, `control-board-origin`). 1->2
or 1->3 is inside this line's measured rep-to-rep noise - identical flags already disagreed by 2
findings - and counts as **no signal**, not a small win. Budget must not exceed the control's.

**If no variant clears the bar**, the prompt is exonerated as the sole cause and the next suspect
is the schema: R-22 server-side structured output, promoted ahead of P1. Note the shim cannot test
R-22 at all - it concatenates system+user onto stdin and has no `response_format` - so that branch
needs a provider key, not this one.

**Schedule.** Rows run A,B,C then a second A,B,C pass - the same schedule task-10 used when
it measured the +/-2 rep-to-rep spread this rule is calibrated against, so provider drift is
spread the same way across both. `--set` reaching the model is not assumed: the runner greps
the shim's raw prompt dumps for each variant's wording and marks a row invalid if absent.

**Caveat carried from the rows above.** Same model line as the four Cursor rows (`cursor-agent`,
`gemini-3.7-flash-high`), flags identical to `p0-cap`, so variant A doubles as a replication of the
1-of-23 row. Not comparable to the `gemini-3.5-flash` rows at the top of this file.

## 2026-09-11: Step 1 prompt-wording experiment - result. The prompt is exonerated.

Six rows, `.delegate/runs/task-11/`, run id `variants:20260911T151849Z:*`. Read against the rule
pre-registered above, which was written before any row ran.

| Row | Findings | Matched/23 | Unknown (adjudicated) | Control FPs | Review calls | Files | Prompt tokens |
|---|---|---|---|---|---|---|---|
| A rep1 (control) | 1 | **1** | 0 | 0 | 2 | 172 | 452k |
| A rep2 (control) | 0 | **0** | 0 | 0 | 2 | 171 | 426k |
| B rep1 (no "concise") | 2 | **0** | 2 -> both out-of-label | 0 | 2 | 173 | 436k |
| B rep2 | 1 | **1** | 0 | 0 | 2 | 172 | 446k |
| C rep1 (completeness) | 2 | **1** | 1 -> out-of-label | 0 | 2 | 173 | 456k |
| C rep2 | 2 | **1** | 1 -> out-of-label | 0 | 2 | 173 | 439k |

Every row: 2 review calls, 171-173 of the PR's files reached, 426-456k prompt tokens. Budget and
coverage are equal across variants, so wording is the only thing that differs. Each row's raw
prompts were grepped for that variant's sentence and all six passed, so the override did reach the
model rather than silently falling back to the control.

**Verdict: no variant clears the pre-registered bar** (>= 4 of 23 on both reps). Best variant
result is 1 of 23 - the same as the control. The bar was set at +3 true positives because that is
the bottom of the range the literature says n=23 can support; the observed spread is +/-1 finding.

**The control disagrees with itself more than the variants disagree with the control.** A rep1
found 1, A rep2 found 0, on identical wording, flags and seed. That is the whole result: on this
model line, finding count is noise of size +/-1 around ~1, and no wording tested moves it.

**Adjudication.** Four findings were out-of-label, all in code the corpus does not label: a
mission-replacement duplication in `packages/engine/lib/src/game_engine.dart` (reported by B rep1,
C rep1 and C rep2 - three independent rows, so worth a look as a real defect) and a chroma
undershoot in `lib/src/theme/oklab.dart`. They are **not** counted as matches and no label was
added after the fact. If the engine finding is real, it belongs in a corpus revision scored by
later runs, not this one.

**The one label ever matched is `test-debug-leftovers` (severity 1)** - print/debugDumpApp left in
a test helper, a lexically local defect visible in the diff hunk alone. The severity-4 labels
(`ads-show-timeout`, `iap-starter-coin-loss`) were found by no row of any variant. Severity-weighted
recall is 0.022 across the board. What the tool misses is what needs context beyond the hunk.

**Consequence, per the work plan's own branch.** The prompt is exonerated as the sole cause of the
recall ceiling. Next suspect is R-22 server-side structured output, promoted ahead of P1 - and the
Cursor shim cannot test it (it concatenates system+user onto stdin and has no `response_format`),
so that branch needs a provider key with quota.

**What this does not establish.** Two reps per variant, not the >= 5 the literature recommends, so
this rules out a large wording effect, not a small one. A wording change worth < 3 true positives
would not be visible here and is not worth chasing at this recall level anyway.

## 2026-09-11: diff-order permutation + vote - pre-registration

**Written before the rows ran.** Step 1 exonerated the wording; R-22 is untestable on this provider
(no `response_format`). This is the next recall play that *is* testable here, and it has a vendor
precedent: BugBot runs passes over differently ordered diffs and combines them, crediting that plus
a validator for 52% -> 70% on its resolution-rate metric (<https://cursor.com/blog/building-bugbot>).

**Mechanism.** `pr_reviewer.permute_diff_order_across_samples` (commit `6cae88f7`) reorders the
per-file blocks between consensus samples. Sample 0 keeps the original order. Diversity comes from
ordering, not temperature, which stays at 0 - so the samples differ only in where each file sits in
the prompt.

**Rows.** Flags identical to the Step 1 rows except `num_samples=3` and the permutation flag:

| Row | num_samples | min_votes | Reads as |
|---|---|---|---|
| P-union rep1/rep2 | 3 | 1 | union of three orderings - the recall ceiling of this mechanism |
| P-vote rep1/rep2 | 3 | 2 | the same three samples filtered by agreement - the precision guard |

Control is the Step 1 A rows (num_samples=1): 1 and 0 of 23.

**Decision rule (pre-registered).**
- **Win:** >= 4 of 23 matched on both reps of P-union, with zero control false flags.
- **Promising, needs 3 more reps before it is called anything:** >= 3 on both reps.
- **No signal:** anything at or below 2, which is inside the +/-1 spread the six Step 1 rows showed.
- P-vote is not scored for a win. It answers a second question: how much of P-union's recall
  survives requiring two of three samples to agree. If union gains nothing, P-vote is moot.

**Cost.** Three review calls per chunk instead of one: roughly 1.3M prompt tokens per row, about
5.4M for the four rows. If union gains nothing at 3x the budget, the mechanism is dead on this
corpus and the next move is retrieval (R-16/R-17), not more sampling.

**What it cannot show.** Whether ordering or sheer repetition produced any gain - three samples in
the *same* order would separate those, and is the row to add if this one moves.

## 2026-09-11: diff-order permutation + vote - result. No signal.

Four rows, `.delegate/runs/task-12/`, run id `permute:20260911T155*:*`. Read against the rule
pre-registered above.

| Row | Samples | min_votes | Findings | Matched/23 | Control FPs | Review calls | Prompt tokens |
|---|---|---|---|---|---|---|---|
| union rep1 | 3 | 1 | 2 | 1 (`test-debug-leftovers`) | 0 | 6 | 1,323k |
| union rep2 | 3 | 1 | 3 | **2** (`+ store-flow-triplicated`) | 0 | 6 | 1,316k |
| vote rep1 | 3 | 2 | 2 | 1 | 0 | 6 | 1,303k |
| vote rep2 | 3 | 2 | 1 | 1 | 0 | 6 | 1,296k |

Control (Step 1's A rows, 1 sample): 1 and 0 of 23 at ~440k tokens.

**Verdict: no signal.** The union rows are 1 and 2 of 23; the pre-registered "promising" band was
>= 3 on *both* reps and the win band >= 4. Three times the budget (1.3M prompt tokens against 440k)
bought at most one extra label, and not reproducibly.

**The permutation itself worked** - verified from the raw prompt dumps, where the three samples of a
chunk lead with different files (`profile_store.dart`, `piece_director_ramp_test.dart`,
`music_tier.dart`) at an identical payload size. This is a real test of the mechanism, not a no-op
flag.

**What that buys us is a negative with teeth.** In rep1 the vote row returned *exactly* the union
row's findings: three passes with the labeled files in completely different prompt positions
produced the same two findings. The model is not missing these defects because they sat deep in a
long prompt - position bias / "lost in the middle" is not the mechanism here. It is not finding them
at all.

**`store-flow-triplicated` (severity 1) is new** - the first time any row matched a second label, and
it appeared in only one of two reps. Both severity-4 labels remain unfound by every row of every
configuration run to date.

**Consequence.** Sampling is dead on this corpus, as the pre-registration said it would be if union
gained nothing: *the next move is retrieval (R-16/R-17), not more passes over the same text.* Three
mechanisms are now eliminated with rows - coverage, chunk size, prompt wording, and sampling
diversity - and all of them were about how the *same* bytes are presented. What is left is changing
which bytes the model sees.

`pr_reviewer.permute_diff_order_across_samples` stays in the tree, default off: it costs nothing
when unused, and it is the honest way to re-test this once retrieval changes what a sample contains.

## 2026-09-11: R-17 - the repo's own analyzer contributes nothing on this corpus

Measured, not assumed. `block_rush` cloned at the labeled head `6e61389`, `flutter pub get` run,
then `dart analyze --format=machine lib test`: **0 diagnostics**. The same command on `main` is
also clean.

So every one of the 23 labeled defects is invisible to the repository's own toolchain. They are
semantic and business-logic defects - a missing timeout on a show path, a coin grant written before
the flag that guards it, a non-notifying provider - not the class of thing a linter names. Two
consequences:

- **R-17 cannot raise recall here.** Its stated value is verification targets plus deduplication of
  findings the linter already reports; with zero diagnostics there is nothing to feed and nothing to
  dedupe. It is still worth having for repos that are not analyzer-clean, and the plumbing is built
  (`pr_agent/algo/static_analysis.py`), but it must not be sold as a recall play on this corpus.
- **It sharpens what R-16 has to do.** If the toolchain that fully resolves types and imports finds
  nothing, then no amount of *syntactic* context will either. The retrieved context has to carry the
  code that makes a *behavioural* asymmetry visible - the load path next to the show path - not just
  definitions.

A practical constraint worth recording: `dart analyze` on a checkout without `flutter pub get`
reports one `URI_DOES_NOT_EXIST` per import and nothing else. `run_dart_analyze` treats that state
as "analyzer unavailable" rather than passing the noise into a prompt.

## 2026-09-11: R-16 cross-file retrieval - pre-registration

**Written before the rows ran.** Four mechanisms are now dead with rows - coverage, chunk size,
prompt wording, sampling diversity - and every one of them changed how the *same* bytes were
presented. This is the first experiment that changes **which bytes** reach the model.

**Mechanism.** `pr_reviewer.enable_symbol_retrieval` with a local checkout at the labeled head
(`6e61389`). For each identifier a hunk changes, the reviewer retrieves that identifier's definition
and its callers from files the diff does not touch (`4f59665b`). The index over this repo: 1,358
defined symbols, 4,409 referenced, built in 0.4s.

The prompt changes too, and that is deliberate: the system prompt otherwise says "you only see
changed code segments, not the entire codebase" and forbids speculating about other code "unless you
can identify the specific affected code path from the diff context". Both are conditioned on
retrieval being present; the off-state wording is byte-identical to the control rows.

**Rows.** Two reps, flags identical to the Step 1 A rows plus the three retrieval flags.
Control: A rep1/rep2 = 1 and 0 of 23.

**Decision rule (pre-registered).**
- **Win:** >= 4 of 23 on both reps, zero control false flags. Same bar as Step 1; the literature puts
  a real effect at +3 to +5 true positives on n=23.
- **Promising, needs 3 more reps:** >= 3 on both reps.
- **No signal:** at or below 2, which is inside the spread the control itself shows.

**The specific thing to look for**, beyond the count: `ads-show-timeout` (severity 4) is a timeout
present on the load path and missing on the show path. If retrieval works at all, that is the label
it should catch, because the two paths are in the same file's neighbours and the asymmetry is only
visible with both in front of the model. A win on count without that label would be luck; that label
without a win on count would still be evidence the mechanism is right and the budget is wrong.

**If this is null too**, the remaining explanation is not context volume but the model line itself,
and the next move is a stronger model on a subset rather than more retrieval - a conclusion worth
writing down before the rows rather than after.

## Reproduce

To reproduce the Cursor rows, start the shim first and point the run at it instead of Gemini:

```bash
CURSOR_API_KEY=... python3 tests/eval/cursor_openai_shim.py --port 8899 \
  --model gemini-3.7-flash-high --raw-dir /tmp/cursor-raw &
#   ... then --set config.model=openai/cursor-gemini-3.7-flash-high \
#            --set openai.api_base=http://127.0.0.1:8899/v1 --set openai.key=cursor-cli-shim \
#            --set config.custom_model_max_tokens=1048576
```

The model must have a 1M context; a smaller one truncates long prompts without saying so.

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
