# Review-quality eval

Replays `/review` on frozen pull-request commits of this repository and measures what it posts, so a change to the
review prompts, the model or the model handling shows its effect on review quality before it merges.

- `cases.json`: 25 catch cases (commits where a maintainer raised 44 real issues, the known findings) and 30
  precision cases (recent PRs). Each case keeps the PR's title, description and linked tickets as they were when the
  PR was opened.
- `replay.py`: runs pr-agent's own `/review` on one case with the local git provider and the live model.
- `judge.py`: a catch judge (two calls, does the review raise each known finding: caught, partial or missed) and a
  precision judge (reads the code at the PR head, then decides whether each posted finding is a real defect).
- `score.py`: scoring and the gate. A run fails on any failed review, on output-cap hits in more than 10% of runs, or
  on a catch drop beyond the noise floor (20% of the known findings divided by the square root of the repeats, about
  6 of 44 with two repeats). Precision and volume are reported, not gated, because their counts are small.
- `baseline_fast.json` and `baseline_full.json`: the metrics of the last accepted run of each tier. A comparison is
  only meaningful with the same judge model.

Two tiers. `--tier fast` (the default in CI) runs only the six catch cases that every Sonnet run caught (18 known
findings), with two repeats: about 12 review calls and $1 on Sonnet. It is a canary that a prompt or handler change
broke the review, not a measure of quality, because the cases were picked for being caught. `--tier full` runs all
55 cases, about $8.50 with two repeats, for model changes, releases and decisions.

Run it from the repository root with the model provider's key in the environment (`ANTHROPIC__KEY` or `OPENAI__KEY`):

```bash
uv run python scripts/review_eval/run_eval.py --model anthropic/claude-sonnet-5-5 \
    --judge-model anthropic/claude-sonnet-5-5 --repeats 2 --out /tmp/review-eval
```

The full tier with two repeats makes about 110 review calls and 220 judge calls. Runs and verdicts are cached under `--out`, so an
interrupted eval resumes. `--limit 1` runs one case of each kind as a smoke test, `--set SECTION__KEY=VALUE` tries a
setting, and `--write-baseline scripts/review_eval/baseline_<tier>.json` records an accepted run.

In CI, `.github/workflows/review-eval.yaml` runs it on manual dispatch, or on a same-repository PR that touches the
review prompts or model handling and carries the `review-eval` label (re-apply the label to run it again after a
push). It needs the `REVIEW_EVAL_MODEL` and `REVIEW_EVAL_JUDGE_MODEL` repository variables, and the matching
`REVIEW_EVAL_OPENAI_KEY` or `REVIEW_EVAL_ANTHROPIC_KEY` secret in a `review-eval` environment; give that environment
required reviewers so each run waits for a maintainer's approval. `--set` accepts only `CONFIG__*` and
`PR_REVIEWER__*` settings and refuses anything naming a key, token, endpoint or model.
