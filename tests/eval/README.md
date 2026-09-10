# Review-accuracy eval

Measures how many known defects `/review` actually reports. Nothing else in this repository
does: `tests/health_test/` asserts that output *starts with* the expected header, which cannot
tell a thorough review from an empty one.

This exists so every tuning claim in [ACCURACY_PLAN.md](ACCURACY_PLAN.md) has a number behind
it. It needs a live model, so it sits outside `testpaths` in `pyproject.toml` and never runs in
CI. The harness's own logic is covered by `tests/unittest/test_eval_harness.py`, which does run.

## Run it

```bash
# from a directory OUTSIDE any git checkout - see "The enrichment trap" below
cd "$(mktemp -d)"
PYTHONPATH=/path/to/pr-agent \
  uv run --project /path/to/pr-agent python /path/to/pr-agent/tests/eval/run_eval.py \
  --repo-root /path/to/pr-agent --out results.json
```

### Growing the corpus

The 11 hand-curated items in `corpus.py` cannot tell a 10-point recall change from noise — one
item flipping is a 9-point swing. Two flags add labelled items at scale:

| Flag | Source | Label quality |
|---|---|---|
| `--mutants N --seed S` | AST mutations of real files under `pr_agent/` (`mutate.py`): 7 operators, ~2,300 candidates, balanced across operators so comparisons don't drown out awaits. Same seed, same corpus. | Exact — the line label is derived from the edit |
| `--mine-fixes N` | The most recent small `fix:` commits, reversed | Real bugs someone shipped; label is the commit subject, scored on lines only |

For an A/B, 60–100 mutants at a fixed seed is the useful range. Compare the same seed across
configurations, never different seeds.

### Comparing configurations

`--set KEY=VALUE` overrides any setting for one run and records it in the report's `metadata`:

```bash
run_eval.py --mutants 60 --seed 1 --out base.json
run_eval.py --mutants 60 --seed 1 --out findings12.json --set pr_reviewer.num_max_findings=12
run_eval.py --mutants 60 --seed 1 --out vote3.json \
  --set pr_reviewer.num_samples=3 --set pr_reviewer.min_votes=2 --set config.temperature=0.4
run_eval.py --mutants 60 --seed 1 --out json.json --set litellm.response_format=json_object
```

`--only <id> <id>` runs a subset; `--keep-reviews` stores each raw review in the report.
Exit status is 1 only when *nothing* parsed — a low score is a result, not an error.

## What the numbers mean

| Metric | Reading |
|---|---|
| `parse_fail_rate` | Reviews that produced no structured output at all. **Read this first.** An unparsable review never reaches `publish_structured_review` (`pr_reviewer.py:938-946`), so it writes no JSON. For a small local model this is usually the dominant failure, and it is the one grammar-constrained decoding fixes. |
| `recall` | Hits over reviews that parsed. Meaningless without `parse_fail_rate` beside it: a model that answers only when confident scores well here and badly there. |
| `recall_no_rationale_leak` | Recall excluding defects whose reversed fix also deleted a comment naming the bug. The honest number. |
| `hits_by` | How hits were earned: `lines` (finding overlaps the seeded hunk ±2), `signal` (wording only), `both`. A corpus where every hit is `signal` is measuring vocabulary, not detection. |
| `wall_clock_seconds` | Total, plus `seconds` per item in `results`. Consensus sampling multiplies this by `num_samples`. |
| `file_only` | Right file, nothing identifying the defect. Not a find. |
| `unmatched_findings` | Findings matching no seeded defect. **Not** false positives — the diff can contain real problems nobody seeded. Do not report this as precision. |
| `by_class` | Per-defect-class recall. The actionable breakdown: it says *which* bug classes the configuration is blind to. |

## The two knobs built for small models

Both are off by default and both exist to be measured here, not assumed:

- **`pr_reviewer.num_samples` / `min_votes`** — review the diff N times at temperature > 0 and keep
  the findings that recur in ≥ `min_votes` samples (0 = a majority of them), matched by file and
  overlapping lines rather than wording, or by a fuzzy content match when a finding names no line
  (`review_merge.vote_review_samples`). The remaining fields take the samples' median or majority
  rather than their worst case, and a finding the vote drops blocks finding-state resolution. A small model reports a different subset of
  the real defects every run; the union raises recall and the vote restores precision. It also
  sidesteps `num_max_findings` — N samples of 3 yield up to 3N distinct candidates. Costs N model
  calls per chunk.
- **`litellm.response_format = "json_object"`** — asks an OpenAI-compatible server to enforce a
  JSON grammar. llama-server, vLLM and Ollama honour it; the output is valid YAML so `load_yaml`
  reads it unchanged. This is the direct fix for `parse_fail_rate`. Expect some hosted providers
  to reject the parameter.

## Labeled real PRs

```bash
tests/eval/fetch_pr_diff.sh samer2373/block_rush 1 /tmp/block_rush_pr1.diff
PYTHONPATH=. uv run python tests/eval/run_eval.py --labels tests/eval/labels/block_rush_pr1.json --diff-file /tmp/block_rush_pr1.diff --out /tmp/labels.json
```

Reports precision, recall, severity-weighted recall, control false flags. Labels come from the 2026-09-10 audit.

## What it does not measure

Every item is a single-site defect in one or two files. A model can score well here and still
miss the multi-file, cross-module reasoning failures where small local models are weakest — the
classes flagged unverified in ACCURACY_PLAN.md §7. Qodo's own benchmark used the same
injected-defect method and their frontier multi-agent product still topped out at 60.1% F1, so
treat a good score as coverage of the seeded classes and nothing more. Grow `corpus.py`
deliberately rather than reading the aggregate as general recall.

## The enrichment trap

`PlainDiffGitProvider` reads the real working-tree file whenever a diff path resolves under the
repository root (`plain_diff_provider.py:78`). For a reverted-fix item that file is the *fixed*
one, so the model would review a patch against content that already contains the fix — scoring
something that is not the seeded defect.

The mutants use paths (`svc/...`) that do not exist here, so they are always patch-only, and
`test_mutant_paths_do_not_exist_in_this_checkout` keeps it that way. The reverted-fix items use
real paths, so run from outside any checkout. The runner verifies this and raises
`EnrichedCorpusError` rather than scoring a poisoned corpus.

## Baseline order

Measure in this sequence, re-scoring after **each** change rather than in a batch — otherwise a
regression and an improvement cancel and you keep both:

1. Your current cloud model, unchanged. This is the ceiling.
2. `num_max_findings = 12` alone (ACCURACY_PLAN.md §2.0). Default 3 hides everything past the
   third finding, and it is capped twice — in the prompt and again in code.
3. The local model, unchanged (`local_profile.toml`). The drop from step 1 is what the rest of
   the plan has to close.
4. Everything else, one knob at a time. Keep only what moved the number.
