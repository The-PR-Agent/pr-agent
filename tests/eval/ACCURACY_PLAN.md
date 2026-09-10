# PR-Agent: raising review accuracy on small, locally-hosted models

Target machine (measured, this box): **Apple M4, 16 GB unified memory**, `llama-server`
(llama.cpp) installed at `/opt/homebrew/bin/llama-server`. No Ollama, no vLLM, no MLX server.
No Semgrep / CodeQL / Bandit / Ruff / mypy / mutmut installed.

---

## Implementation status (branch `fix/review-finding-loss`)

Landed as code, with tests, defaults preserved:

| § | Fix | What shipped |
|---|---|---|
| 2.2 | Parse failure discarded the whole review | `_get_review_data` parses inside the retried callable and raises `UnparsableReview`, so an unparsable review now advances `config.fallback_models` instead of ending the run. `tests/unittest/test_review_unparsable_prediction.py` |
| 2.4 | Failed chunks dropped silently | One retry per failed chunk (`CHUNK_REVIEW_ATTEMPTS = 2`), covering both no-answer and unrepairable-YAML failures; chunks merge in diff order; the first error is the one reported. `test_review_large_diff_chunking.py` |
| 2.5 | Reflection outage was invisible | Restored the suppressed `get_logger().error` and each suggestion's `score_why` now says the score is unavailable, which the publish body renders. Score stays 7 so real findings aren't dropped. (A `self_reflection_failed` flag was added and then removed: nothing read it, and it would have gone sticky across `parallel_calls` chunks.) `test_self_reflection_failure_visibility.py` |
| 2.1 | Token undercount for foreign tokenizers | A one-per-model warning naming `config.max_model_tokens` as the remedy. `test_token_encoder_tokenizer_warning.py` |

**Two model-side knobs landed, off by default, each with tests.** `pr_reviewer.num_samples` +
`min_votes` (consensus over independent samples, matched by location — `review_merge.vote_review_samples`,
`test_review_consensus_sampling.py`) and `litellm.response_format = "json_object"` (grammar-enforced
JSON on OpenAI-compatible servers; JSON is valid YAML so nothing downstream changes —
`test_litellm_response_format.py`). Both work inside the chunked flow too, which is where a 20k-context
model lives. Neither is a recommendation yet: the eval exists to decide.

**The eval grew teeth.** Findings now match on *line overlap* with the seeded hunk (derived from the diff,
never hand-written) with wording as secondary evidence, and the summary says which earned each hit. An AST
mutation engine (`tests/eval/mutate.py`, 7 operators, ~2,300 candidates in this checkout, balanced and
seed-reproducible) and `--mine-fixes` replace the 11-item ceiling; `--set KEY=VALUE` makes every knob an
A/B with the settings recorded in the report.

**Self-review round (independent reviewer + executor agents).** Found and fixed: `lstrip("./")` stripping the
dot of hidden paths in both the voter and the scorer (`.env` and `env` voted as one finding); path-suffix
matching without a segment boundary (`_agent/x.py` counted as `pr_agent/x.py`); the consensus clusterer
absorbing two *distinct* findings from one sample when they sat within tolerance — recall loss caused by the
recall feature, fixed by never letting a cluster take two votes from one sample; `invert-comparison`
mapping `<`→`>` (a direction swap) instead of the logical negation `<`→`>=` its class label claims;
`drop-none-guard` mislabelling `is not None` guards; and a SHA-reachability test that would have failed on
CI's shallow checkout. Every fix has a regression test.

**Phase 0 (§5) is built.** `tests/eval/` holds an 11-item labelled corpus — four reversed fix
commits from this repository plus seven hand-written single-site mutants across seven defect
classes — a scorer that keeps `parse_fail` distinct from `miss` (they are different failures and
folding them together hides the one small models actually hit), and a runner that drives each
item through the `plain_diff` provider. `tests/unittest/test_eval_harness.py` covers the scoring
rules and drives the whole pipeline once with a stubbed model, so the harness is verified even
though the eval itself needs a live model and stays out of CI. `tests/eval/local_profile.toml`
carries the §4 values as an opt-in file rather than a repo default; its keys were validated
against `configuration.toml`. **No numbers were produced** — that needs a served model, and a
fabricated or cloud-only baseline reported as "the eval" would be worse than none.

**§2.3 was implemented and then reverted.** Clipping an oversized patch to the remaining budget
consumed the room every smaller file after it still needed — the repo's own
`test_generate_full_patch_records_too_large_patch_files` caught the starvation. The skip is now
documented as deliberate, with ignore rules named as the actual remedy. The behaviour is
unchanged from before this branch.

**§2.1 deliberately did not change the counting.** Passing `force_accurate=True` in the packing
path routes Anthropic models through `_calc_claude_tokens`, which makes an API call — and that
path counts tokens once *per file*. The fix is the config margin plus the new warning, not a
patch to the counter.

Not done, and why: the tuning profile (§4) and the eval harness (§5) are additive work needing
decisions — `.pr_agent.toml` in this repo feeds Qodo's **hosted** bot rather than the OSS engine,
so the tuning belongs in a separate profile, and the deterministic layer (§3) needs dependencies
that AGENTS.md requires approval for.

Verification: `4466 passed, 1 skipped, 1 xfailed`; `ruff check pr_agent/ tests/unittest/` clean;
pre-commit hooks pass. Note your global `uv` is 0.10.9 while `pyproject.toml:141` pins
`==0.12.10`, so `uv run` and the pre-commit ruff hook fail until that's reconciled — the runs
above used a locally fetched 0.12.10.

## 0. Reframe the goal first — "leave no issue not found" is not reachable by prompting

100% recall from an LLM reviewer does not exist at any model size, and a 7–14B local model has
*materially* lower per-pass recall than the cloud model PR-Agent's prompts were tuned against.
Published SWE-bench-Verified numbers put the best open models that fit large machines at
47–68%, versus frontier closed models far above that ([Devstral 46.8%](https://mistral.ai/news/devstral/),
[Qwen3-Coder-30B-A3B 59.2%](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/discussions/30),
[GLM-4.5-Air 57.6% / GLM-4.6 68.0%](https://llm-stats.com/models/compare/glm-4.5-air-vs-glm-4.6),
[Kimi-Dev-72B 60.4%](https://moonshotai.github.io/Kimi-Dev/)). Every one of those models is
**too big for 16 GB**. Whatever runs on this box sits below them.

The most useful calibration point comes from Qodo's own benchmark: 100 real merged PRs across
seven languages with **580 verified injected issues**, scored Hit/FP/FN. Their best commercial
multi-agent product scored **60.1% F1** — the highest of eight tools tested, with competitors
trading high precision for very low recall
([Qodo](https://www.qodo.ai/blog/how-we-built-a-real-world-benchmark-for-ai-code-review/)).
No standalone figure is published for the open-source engine. So the state of the art, with
frontier models and a multi-agent pipeline, finds roughly six issues in ten. Plan against that
number, not against zero misses. (Note also that their methodology is *injected issues* — which
is exactly the seeded-defect approach proposed in §5, independently arrived at.)

So recall has to be bought architecturally, not by picking a better prompt:

1. **Stop dropping context.** Today's biggest source of missed findings is PR-Agent's own
   compression path silently discarding files — that is configuration and code, not model quality.
2. **Guarantee the output parses.** A weak model that emits malformed YAML loses the *entire*
   review, not one finding (see §2.2). Grammar-constrained decoding removes this class outright.
3. **Decompose into narrow passes.** One bug class per call. Specialised-agent decomposition
   beat naive ensembling in [arXiv 2606.15689](https://arxiv.org/html/2606.15689); a
   wide-net-then-filter two-pass split is also what a production review tool converged on
   ([G-Research](https://www.gresearch.com/news/building-a-code-review-tool-the-llm-patterns-that-actually-work/)).
4. **Let deterministic tools own what they can prove.** Ruff / Bandit / Semgrep / mypy find
   their covered rule classes at ~100% recall. Hybrid SAST→LLM-triage pipelines report ~11×
   signal-to-noise improvement and up to 91% false-positive reduction
   ([SAST-Genius, arXiv 2509.15433](https://arxiv.org/pdf/2509.15433),
   [LPNU SAST+LLM CI/CD](https://science.lpnu.ua/ictee/all-volumes-and-issues/volume-6-number-1-2026/sast-improvements-using-llm-cicd-pipelines)).
   On a 16 GB box this is where most of your real recall will come from.
5. **Measure, or none of the above is a claim.** §5.

---

## 1. What actually fits on 16 GB M4

Budget ~11–12 GB for weights+KV after OS. Memory bandwidth on base M4 is roughly 120 GB/s,
which caps generation speed and makes long-diff *prefill* the dominant cost.

| Option | Approx. weights | Usable ctx | Verdict |
|---|---|---|---|
| Qwen2.5-Coder-7B-Instruct Q5_K_M | ~5.5 GB | 24–32k | Safe. Best latency for multi-pass. |
| Qwen2.5-Coder-14B-Instruct Q4_K_M | ~9 GB | 12–16k | Best quality that fits. Tight. |
| Qwen3-Coder-30B-A3B Q3_K_M | ~14 GB | ~8k | Does **not** fit reliably on 16 GB. Skip. |
| Devstral-Small-24B Q4 | ~14 GB | — | Does not fit. Skip. |

Two evidence-based cautions on the choice:

- **Don't go below Q5 on a small model.** 4-bit costs ~4 points absolute on code-gen suites
  (51.8% vs 56.1% FP16, and AWQ/GPTQ/Q4_K_M/BnB all cluster together)
  ([SitePoint](https://www.sitepoint.com/quantization-q4km-vs-awq-fp16-local-llms/)), and the
  degradation is **worse the smaller the model** — 70B holds up at Q4, a 7–14B does not
  ([jarvislabs](https://jarvislabs.ai/blog/vllm-quantization-complete-guide-benchmarks)).
  Prefer 7B@Q5_K_M/Q6_K over 14B@Q4 if a measured A/B doesn't show the 14B winning.
- **Don't trust the advertised context.** RULER found recall degrading with input length on
  all 17 long-context models tested
  ([summary](https://onnyunhui.medium.com/evaluating-long-context-lengths-in-llms-challenges-and-benchmarks-ef77a220d34d)),
  and lost-in-the-middle is still measurable in 2026 even on 1M-token models
  ([writeup](https://dev.to/gabrielanhaia/lost-in-the-middle-is-still-real-in-2026-even-on-1m-token-models-2ehj)).
  YaRN/NTK extension shows sharp downstream drops when the needed fact sits deep in the
  extended region (Phi3-mini: −15.2% / −9.3%)
  ([LongRoPE2](https://arxiv.org/pdf/2502.20082)).
  Practical rule: **keep each LLM call under ~8k tokens of diff** and put the diff adjacent to
  the instruction, not buried mid-prompt. Chunk aggressively rather than widening context.

**Known-bad data point on the recommended model.** Upstream issue
[#1623](https://github.com/qodo-ai/pr-agent/issues/1623) reports Ollama `qwen2.5-coder:7b` (and
`codellama`) producing output that isn't valid YAML; `try_fix_yaml` then mis-recovers, collapses
content to `.`, and fails with `'NoneType' object is not subscriptable`. The same request
against OpenAI `o3-mini` worked. That is precisely the §2.2 failure mode, on precisely the model
size recommended above — which is why grammar-constrained decoding is Phase 4 and not optional.
Treat the model choice as "start here, A/B in Phase 2", and consider a current Qwen3-family
coder at the same size class as the alternate arm; the research pass surfaced Qwen3 numbers only
at 30B, so the 7–8B comparison is unverified and must be measured, not assumed.

Also, if you later switch to Ollama instead of llama.cpp: its default context is **2048
tokens**, far too small for PR-Agent's prompt plus any diff. Set `OLLAMA_CONTEXT_LENGTH` (8192+)
or every review silently truncates.

**Honest verdict for this box:** 16 GB M4 is below the point where a local model alone gives
review recall you'd trust as a gate. Design for *local-first with escalation*, not local-only —
and put the deterministic layer in front. If local-only is a hard requirement, accept that the
LLM's job is triage and obvious-defect sweeps, and that deep cross-file reasoning is out of reach.

---

## 2. Where PR-Agent loses findings **today** — fix these before touching models

All verified against source in this checkout. These matter regardless of which model you run,
and several get *much* worse with a local model.

### 2.0 `num_max_findings = 3` — the hardest recall cap in the system, and it's a default

`pr_reviewer.num_max_findings` defaults to **3** (`configuration.toml:139`) and is enforced
twice over:

- It is interpolated into the prompt itself — `key_issues_to_review` is described as
  *"A concise list (0-{{ num_max_findings }} issues)"* (`pr_reviewer_prompts.toml:123`), so the
  model is instructed to stop looking after three.
- It is read again in the publish path at `pr_reviewer.py:723`, alongside `dropped_findings`
  bookkeeping (`pr_reviewer.py:726-729`).

Any PR with more than three real defects **cannot** report them at default settings, no matter
how good the model is. If the goal is "leave no issue unfound," this is the first line to change
and it costs nothing. Raise it (10–15) and re-measure — but note it directly increases output
tokens, which matters against the 1500-token output reserve (§2.1).

The `/improve` equivalents: `num_code_suggestions_per_chunk = 3` (`configuration.toml:218`) is
the real volume knob — **not** `num_code_suggestions`, which belongs to the separate
`/improve_component` tool. `max_suggestions_per_file = 0` (`:220`) is already uncapped.

### 2.1 Token counting uses a GPT tokenizer with no safety margin — silent overflow

`TokenHandler.count_tokens(patch, force_accurate=False)` returns the raw
`tiktoken o200k_base` estimate (`pr_agent/algo/token_handler.py:179-196`). The compensating
inflation `model_token_count_estimate_factor` (default `0.3` → `factor = 1 + 0.3`,
`token_handler.py:135-137`) lives in `_apply_estimation_factor`, reachable **only** through
`force_accurate=True` — and the sole caller passing that is
`pr_agent/tools/pr_help_docs.py:466`. Every count in the diff-packing path
(`pr_processing.py:200,217,248,306,477`) uses the default.

`TokenEncoder` falls back to `o200k_base` for any unrecognised model name
(`token_handler.py:44-49`), so a Qwen/Llama/Mistral model is budgeted with OpenAI's tokenizer.
Code tokenises differently across vocabularies; when the estimate runs low, the assembled
prompt exceeds the server's real window and llama.cpp truncates — **no error, findings gone.**

*Fix (config-only, do this first):* set `config.max_model_tokens` well below the server's
`-c`. If `llama-server -c 32768`, set `max_model_tokens = 20000`. `get_max_tokens` clamps with
`min(max_model_tokens, max_tokens_model)` (`algo/utils.py:1439-1441`), so this is a hard ceiling
and buys a ~35% margin against tokenizer skew.
*Fix (code, separate minimal PR):* pass `force_accurate=True` in the packing path, or make the
estimation factor apply whenever the model isn't OpenAI/Anthropic.

### 2.2 A YAML parse failure discards the **whole** review

`load_yaml` (`algo/utils.py:1010`) tries `try_fix_yaml` with nine heuristic repairs
(`utils.py:1048-1283`) and returns `{}` on failure (`utils.py:1042-1044`). Then:

- `/review`: missing `data['review']` → `_prepare_pr_review` returns `""`
  (`pr_reviewer.py:889-895`) → `run()` raises `ValueError("Failed to prepare review output")`
  (`pr_reviewer.py:313-314`) → outer handler posts a generic "Failed to review PR"
  (`pr_reviewer.py:125-136,449-452`). **Everything is lost.**
- `/improve`: returns `{"code_suggestions": []}` and increments `parse_failure_count`
  (`pr_code_suggestions.py:949-959`) — degrades to empty rather than erroring.

Critically, **parse failure never triggers `fallback_models`** — it happens *after*
`retry_with_fallback_models` (`pr_processing.py:330-360`) has already returned successfully. So
the fallback chain protects you against transport errors only, never against a weak model
emitting malformed output. This is the single highest-risk failure mode for local models.

*Fix:* **grammar-constrained decoding.** **Verified available on this box** — `llama-server`
build `9430 (d48a56eff)` exposes `--grammar`, `--grammar-file`, `-j/--json-schema` and
`-jf/--json-schema-file`. Constrain output to the review schema and schema-invalid output
becomes structurally impossible. This eliminates an entire class of total-review-loss and is
worth more than any model upgrade at this size.

Caveat on the schema: PR-Agent's review output is **YAML**, not JSON, so a JSON-schema
constraint means either (a) constrain to JSON and add a JSON→YAML shim before `load_yaml`, or
(b) hand-write a GBNF grammar for the YAML shape. (a) is less work and lower risk; `load_yaml`
already tolerates JSON since valid JSON is valid YAML. Confirm on your first run rather than
assuming.

### 2.3 Compression drops whole files, and drops the *small* ones

`pr_generate_compressed_diff` (`pr_processing.py:209`) sorts files by token count **descending**,
then `generate_full_patch` (`pr_processing.py:283`) hard-stops once
`total_tokens > max_tokens_model - 1000` (`pr_processing.py:296-299`) — remaining files are
reduced to a name-only list. A single patch that alone exceeds the soft threshold is **skipped
entirely, not trimmed** (`pr_processing.py:312-319`; the code's own TODO admits this). Once
pruning starts, all extra-context lines are discarded first (`pr_processing.py:81-92`).

**Correction after re-reading the loop:** both budget checks `continue` rather than `break`, and
files are processed largest-first, so smaller later files *do* still fill the remaining budget.
It is a greedy largest-first fill, not a cliff. Two real defects remain:

- A single patch larger than the whole budget is **never partially shown** — it is skipped
  outright, so a big-but-important file contributes nothing rather than its first N hunks.
- **Largest-first is a questionable priority.** It spends budget on the biggest diffs, which in
  practice are often generated code, lockfiles and fixtures, before smaller hand-written logic
  files. The fix here is mostly *exclusion* (ignore rules), not reordering.

*Fixes, config-only:*
- Extend `pr_agent/settings/ignore.toml` / `generated_code_ignore.toml` so lockfiles, snapshots,
  generated code and vendored dirs never enter the budget.
- Turn **on** chunking so overflow becomes more calls instead of dropped files:
  `pr_reviewer.enable_large_pr_chunking = true` (default `false`,
  `configuration.toml:153`) and raise `pr_reviewer.max_number_of_calls`.
- Keep `large_patch_policy = "clip"` (default) so the chunked path clips rather than skips
  (`pr_processing.py:481-497`) — note the single-call path has no clip option at all.

### 2.4 Chunked review silently loses failed chunks

`_prepare_chunked_prediction` (`pr_reviewer.py:792`) fires chunk calls with
`asyncio.gather(..., return_exceptions=True)` (`pr_reviewer.py:810-812`). Failed chunks are
logged and dropped (`pr_reviewer.py:816-820`); chunks parsing to empty are dropped with a
warning (`823-827`); only if *zero* parse does it fall back to a single call (`831-835`).
`merge_review_chunks` (`tools/review_merge.py:37`) is well-built — unions and dedupes findings
by fingerprint, takes the worst value for `score`/`risk_level`/`merge_recommendation` — but it
only ever sees chunks that parsed. `review_failed_chunk_count` (`pr_reviewer.py:841`) surfaces
as a footer sentence (`942-944`), never a retry. Commit `a6484241` correctly stopped a partial
merge from marking findings resolved (`pr_reviewer.py:734-738`), which bounds the damage but
doesn't recover the chunk.

With a weak model, chunk parse failure is the *common* case, not the rare one — which is why
§2.2's grammar constraint is the load-bearing fix here too.

### 2.5 Self-reflection fails open at score 7

`/improve`'s self-reflection is mandatory (`pr_code_suggestions.py:834-843`). If the whole
`ModelType.REASONING` fallback chain fails, **every suggestion is assigned a default score of 7**
with an empty rationale (`pr_code_suggestions.py:838-842`), which then passes the default
threshold of 1. So a total reflection failure looks identical to "all suggestions are decent."
Index-based mapping also skips silently if the returned list length doesn't match
(`pr_code_suggestions.py:884`), and missing line ranges clamp to `score=0` (`895-896`).

*Fix — with a genuine tradeoff.* Setting `suggestions_score_threshold` to 8 makes a reflection
blackout produce *nothing* rather than everything. But `configuration.toml:212` carries an
explicit upstream warning: *"recommend not to set this value above 8, since above it may clip
highly relevant suggestions."* Default is `0`. So threshold 8 buys fail-closed behaviour at the
cost of clipping real score-7 findings — the wrong trade if you're optimising recall.

Pick one deliberately:
- **Recall-first (recommended here):** leave the threshold low (0–3) and instead *monitor* for
  reflection failure. `parse_failure_count` and the reflection-fallback path
  (`pr_code_suggestions.py:846-879`) log it; treat a nonzero count as "this run's scores are
  meaningless" rather than silently trusting them.
- **Fail-closed:** threshold 8, accept the clipping.

Better still, fix it properly in Phase 5: make total reflection failure raise rather than
default to 7.

Also correcting a common misreading: `focus_only_on_problems` defaults to **true**
(`configuration.toml:200`), and in this fork its effect at the filter stage is only to relabel
`critical` → `possible issue` (`pr_code_suggestions.py:977-980`). It steers the *prompt* toward
bugs over style; it does not filter content post-hoc. Don't expect it to cut noise on its own.

### 2.6 Known upstream defects that show up as bad findings

Not your bugs, but they'll pollute your eval numbers if you don't account for them:

- **`...` leaks into `improved_code`** — [#2086](https://github.com/The-PR-Agent/pr-agent/issues/2086),
  open. The system prompt tells the model to shorten long code with `...` "for brevity"; the
  literal ellipsis lands in the suggestion, producing an invalid non-committable inline comment.
- **Duplicate suggestions on every push** — [#2184](https://github.com/qodo-ai/pr-agent/issues/2184),
  closed. `/improve` has no memory of prior runs and re-posts the same suggestions each time.
- **Suggestion volume capped despite raised knobs** — [#1963](https://github.com/qodo-ai/pr-agent/issues/1963),
  closed; matches the §2.0 cap behaviour.
- The suggestion prompt itself instructs the model to "avoid making suggestions that might
  duplicate existing functionality" *because it only sees the diff, not the codebase*
  (`pr_code_suggestions_prompts.toml`) — an in-repo admission that ungrounded suggestions are an
  expected failure mode. This is a structural argument for the Layer-1/Layer-2 split in §3:
  static analysis has whole-repo context the LLM does not.

### 2.7 Dead config to not waste time on

`relevant_best_practices` is hard-set to `""` (`pr_code_suggestions.py:191`) and referenced by
no prompt TOML. The `[best_practices]` / `[auto_best_practices]` sections
(`configuration.toml:514,520`) are read by no `.py` in this repo — vestigial in this fork
(likely consumed by the hosted Qodo Merge service). **Use `extra_instructions` instead**, which
*is* injected: review at `pr_reviewer_prompts.toml:27-41`, improve at
`pr_code_suggestions_prompts.toml:36-50`, alongside `skills_context` from `skills_loader.py`.

Two more non-existent or inert keys, so you don't chase them: `final_clip_factor = 0.8` is
declared at `configuration.toml:224` and referenced by **no** `.py` in the repo (verified by
grep). `enable_intra_pr_learning`, `secondary_model` and `enable_chat_in_code_suggestions`
appear in no source or config here — the real key for the last concept is `enable_chat_text`.

---

## 3. Target architecture: deterministic first, narrow LLM passes second

```
PR diff
  │
  ├─► Layer 1  DETERMINISTIC (fast, ~100% recall on covered classes, zero LLM)
  │     ruff check / ruff format --check   → style, unused, import order
  │     bandit -r                          → Python security patterns
  │     mypy / pyright                     → type + None errors
  │     semgrep --config auto              → taint, injection, authz patterns
  │     └─ output: structured candidate findings
  │
  ├─► Layer 2  LLM TRIAGE  (grammar-constrained, ~1k tokens/call, cheap)
  │     For each Layer-1 candidate: true positive? severity? one-line why?
  │     This is the highest-value use of a small model — evidence shows
  │     LLM-as-triage over SAST beats LLM-as-detector on precision.
  │
  ├─► Layer 3  LLM NARROW SWEEPS (one bug class per call, ≤8k tokens of diff)
  │     pass A: null/bounds/unchecked-return
  │     pass B: error swallowing, bare except, silent fallback
  │     pass C: resource leaks, unclosed handles, missing context managers
  │     pass D: auth/permission/tenant-scoping regressions
  │     pass E: concurrency, async/await misuse, shared mutable state
  │     └─ each pass emits schema-valid JSON; union + dedupe
  │
  └─► Layer 4  ESCALATION (risky paths only)
        auth/, migrations, money math, concurrency, crypto → cloud model
```

Why decomposition rather than one big "find all bugs" prompt: specialised parallel agents
(correctness/security/performance/style) outperformed naive union-ensembles, which *hurt* F1
because generic models overlap on the same easy bugs while each adds distinct false positives
([arXiv 2606.15689](https://arxiv.org/html/2606.15689)).

Two techniques to **not** reach for reflexively:

- **Chain-of-thought.** CoT *decreased* performance versus zero/few-shot on code-*understanding*
  tasks for GPT-4o and Llama3-70B; its gains concentrate on math/symbolic reasoning
  ([arXiv 2409.12183](https://arxiv.org/pdf/2409.12183)). Test it per pass, don't assume it.
- **Self-consistency / majority voting.** Suits exactly-matchable answers, and is
  *non-monotonic* — accuracy can rise then fall with more samples, worst for small models on
  hard queries ([arXiv 2608.11403](https://arxiv.org/html/2608.11403)). Open-ended bug finding
  is a poor fit. A critic/verifier second pass is the better shape.

Few-shot with worked examples *is* worth it, and PR-Agent already ships two in
`pr_reviewer_prompts.toml` (~lines 150-190, 300-340) for the model to pattern-match. Few-shot+CoT
was the best combination in a bug-classification study, but note the recall/precision swing:
Gemini-2.0-flash few-shot reached 0.989 recall at 0.723 precision while GPT-4o few-shot got
0.903 precision at 0.833 recall ([arXiv 2505.08263](https://arxiv.org/pdf/2505.08263)) — pick
your bias deliberately per layer (Layer 3 = recall-biased, Layer 2 = precision-biased).

---

## 4. Concrete local profile

Serving (`llama-server`, already installed):

```bash
llama-server \
  -m ~/models/qwen2.5-coder-7b-instruct-q5_k_m.gguf \
  -c 32768 -ngl 99 --flash-attn \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0.1 --top-p 0.9 \
  --host 127.0.0.1 --port 8080 --alias local-review
```

- `temperature 0.1` — code review is graded on correctness, so favour the highest-confidence
  completion; 0–0.2 is the converged recommendation for code and for schema-conforming output
  ([Medium](https://medium.com/@glanzz/stop-using-temperature-1-0-385cb51ac863),
  [SurePrompts](https://sureprompts.com/blog/llm-temperature-sampling-complete-guide-2026)).
  Note temperature 0 does **not** give byte-identical reruns — batching and kernel
  nondeterminism persist ([analysis](https://www.vincentschmalbach.com/does-temperature-0-guarantee-deterministic-llm-outputs/)),
  so eval runs need multiple samples, not one.
- `q8_0` KV cache — 4-bit KV showed "almost no degradation" on Qwen3-8B/Llama3-8B while 2-bit
  "greatly deteriorates accuracy" ([Kaitchup](https://kaitchup.substack.com/p/qwen36-27b-kv-cache-quantization)),
  so q8_0 is a free win. If you enable speculative decoding, **re-tune it after** — FP8/INT8 KV
  shifts target logits enough to roughly halve acceptance gains
  ([dev.to](https://dev.to/tech_nuggets/kv-cache-quantization-what-fp8int8-k-and-v-actually-buy-you-and-where-they-break-4fnl)).

`.pr_agent.toml` (repo-level override — the sanctioned place per AGENTS.md):

```toml
[config]
model = "openai/local-review"
fallback_models = []                 # no cloud fallback in local-only mode
custom_model_max_tokens = 20000      # model not in MAX_TOKENS registry -> this supplies it
max_model_tokens = 20000             # global clamp; see budget note below
temperature = 0.1
duplicate_prompt_examples = true     # see tradeoff below

[openai]
api_base = "http://127.0.0.1:8080/v1"
key = "sk-local"                     # llama-server ignores it; LiteLLM requires non-empty

[litellm]
drop_params = true                   # llama-server rejects some OpenAI-style params

[pr_reviewer]
num_max_findings = 12                # §2.0 — THE recall cap. Default 3 hides everything past #3.
enable_large_pr_chunking = true      # §2.3 — overflow becomes calls, not dropped files
max_number_of_calls = 5
require_score_review = false         # fewer schema fields = fewer parse failures
require_tests_review = false
require_can_be_split_review = false
require_security_review = true
extra_instructions = """
Report ONLY defects that would change runtime behaviour or security posture.
For each: exact file and line, the concrete failing input or state, and the consequence.
Do not report style, naming, formatting, or missing comments — other tools own those.
If you are not confident a finding is a real defect, omit it.
"""

[pr_code_suggestions]
suggestions_score_threshold = 2      # §2.5 — recall-first; monitor reflection failure instead
num_code_suggestions_per_chunk = 6
parallel_calls = false               # 16GB: serialise, don't thrash memory
```

**Do the budget arithmetic before trusting these numbers.** `get_max_tokens` resolves
`custom_model_max_tokens` then clamps with `min(max_model_tokens, max_tokens_model)`
(`algo/utils.py:1369,1439-1441`), so the *lower* of the two wins — set them equal to avoid a
line that silently does nothing. From that ceiling PR-Agent subtracts a 1500-token output
reserve (`OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD`) or 1000 (`HARD_THRESHOLD`)
(`pr_processing.py:27-28,76,296,312`), **and** the rendered system+user prompt. The review
prompt file is ~15 KB on disk with two full worked examples; the system prompt alone plausibly
costs several thousand tokens. So a 20000 ceiling may leave well under 14k for actual diff.

That makes `duplicate_prompt_examples = true` a real tradeoff, not a free win: it repeats the
example block *after* the diff (`pr_reviewer_prompts.toml:287`, injected at
`pr_reviewer.py:237`), which helps a weak model hold the schema but directly consumes diff
budget. **Once grammar-constrained decoding (§2.2) is in place it is largely redundant — turn it
off then.** Measure the prompt overhead on your profile in Phase 0 rather than guessing.

**Latency reality on this box.** Every recall technique multiplies inference calls. Layer 3's
five passes on a 5-file PR is 5–15 calls; on ~120 GB/s bandwidth with 8k-token prompts that is
minutes of wall clock, dominated by prefill. Design tiered, not maximal:

| Tier | When | Layers |
|---|---|---|
| Fast | every push | 1 + 2 (deterministic + triage). Seconds. |
| Deep | PR marked ready / label `deep-review` | 1 + 2 + 3. Minutes. |
| Escalated | diff touches auth, migrations, money, concurrency | + 4 cloud call |

---

## 5. The eval harness — build this first, or nothing above is a claim

There is **no accuracy eval in this repo**. `tests/health_test/main.py` runs
describe/review/improve against one hardcoded PR and asserts the output *starts with* the
expected header — a smoke test, no recall scoring, no golden findings.
`pr_agent/settings/pr_evaluate_prompt_response.toml` is an LLM-as-judge prompt template with
**no calling code**. So today you cannot tell whether any knob in §4 helped.

Two labelled corpora that cost no annotation effort:

1. **Seeded defects.** Inject known bugs into `pr_agent/` mechanically — off-by-one, flipped
   comparison, swapped args, removed None check, dropped `await`, widened except, removed
   permission check — generate the diff, run `/review`, and score how many seeded defects appear
   in `key_issues_to_review`. Unlimited labelled data, measures recall directly. Mutation tools
   (mutmut, cosmic-ray) can generate the mutants; a hand-written seeder gives you control over
   the bug *classes* you care about, which matters because Layer 3 is organised by class.
2. **Reverse the repo's own fix commits.** `8bd15328`, `a6484241`, `d5c15c49`, `00b8c9d4`,
   `70d3f276` each removed a real bug. Revert each into a synthetic PR whose known defect is
   exactly what the fix deleted. Small, real, and already in your history.

Score per config: **recall** (seeded defects found), **precision** (findings that map to a
seeded defect vs. invented), **parse-failure rate** (how often `load_yaml` returns `{}` — the
§2.2 killer), **wall clock**, and **truncation events**. Then every §4 value gets an A/B number.

**Read the number narrowly.** Seeded defects are mechanical and single-site — flipped
comparisons, dropped None checks, widened excepts. The harness measures *the classes you seed
and nothing else*. A model can score well on those and still miss exactly the multi-file,
cross-module reasoning failures flagged as unverified in §7 — which is where a small local model
is weakest. So a good seeded-recall score is **not** evidence of general recall; it is evidence
that one bug class is covered. Grow the seeded classes deliberately (and note Qodo's own
benchmark used the same injected-issue method and still topped out at 60.1% F1, §0).

Built at `tests/eval/` as a new additive package (see its README) — do not modify `health_test`, and note that
`pyproject.toml` sets `testpaths = ["tests/unittest"]`, so an eval directory won't run in the
default suite (which is what you want; it needs a live model).

**Baseline order:** measure cloud-model recall on the corpus first. That is your ceiling. Then
measure the local model unchanged. Then apply §2 fixes. Then §3 layers. Any recommendation that
doesn't move the number gets dropped.

---

## 6. Sequenced plan

**Phase 0 — measurement (no model changes)**
1. Build `tests/eval/` seeder + scorer. Score = recall / precision / parse-fail / wall clock.
2. Baseline the current cloud model on the corpus. Record it.

**Phase 1 — config-only fixes (works on cloud too, likely the biggest single win)**
3. **`num_max_findings = 12`** (§2.0). Do this one first and alone — it is the cheapest and
   probably largest recall change available, and it applies to your current cloud setup today.
4. `max_model_tokens` / `custom_model_max_tokens` set equal, with margin (§2.1).
5. Expand `ignore.toml` / `generated_code_ignore.toml` for lockfiles and generated code (§2.3).
6. `enable_large_pr_chunking = true`, raise `max_number_of_calls` (§2.3).
7. Decide the score-threshold trade (§2.5), tighten `extra_instructions` (§2.7).
8. Re-score after **each** change, not in a batch. Keep only what moved the number.

**Phase 2 — local serving**
8. `llama-server` with Qwen2.5-Coder-7B Q5_K_M; confirm grammar/JSON-schema support in your build.
9. Wire `.pr_agent.toml` at the local endpoint; score. Expect a large drop vs baseline — that
   drop is the number the rest of the plan has to close.
10. A/B 7B@Q5_K_M vs 14B@Q4_K_M on the corpus. Don't assume the bigger one wins at 4-bit.

**Phase 3 — deterministic layer (probably your largest recall gain on 16 GB)**
11. `uv add --dev ruff bandit mypy` (**ask before adding deps** per AGENTS.md) + install Semgrep.
12. Wire Layer 1 into CI ahead of PR-Agent; feed candidates to a Layer-2 triage prompt.

**Phase 4 — narrow passes + constrained decoding**
13. Grammar-constrained output for review/improve (code change — separate minimal PR).
14. Add Layer-3 per-class passes as new prompt TOMLs, each registered in
    `pr_agent/config_loader.py`'s `settings_files=[...]` or they won't load.
15. Re-score after each pass added; drop passes that add only false positives.

**Phase 5 — code changes, each its own reviewable PR**
16. `force_accurate=True` (or non-OpenAI-aware factor) in the packing path (§2.1).
17. Retry-on-parse-failure inside `retry_with_fallback_models` so malformed output reaches the
    fallback chain (§2.2).
18. Partial-trim instead of whole-file-skip in `generate_full_patch` — the existing TODO (§2.3).
19. Retry failed chunks once before dropping (§2.4).

---

## 7. Conflicts resolved, and what I could not verify

**One research conflict worth recording.** A docs-and-issues pass concluded that
`model_token_count_estimate_factor = 0.3` makes PR-Agent *prune more aggressively* for local
models (inflating estimates by 30%, squeezing an already-small context). Reading the source
directly contradicts this for the path that matters: the factor lives in
`_apply_estimation_factor`, reachable only via `count_tokens(..., force_accurate=True)`, and the
**only** caller passing that flag is `pr_help_docs.py:466`. Every count in the diff-packing path
uses the default `force_accurate=False` and gets the raw tiktoken estimate with no inflation
(`token_handler.py:179-196`). So the risk is the opposite: **under**-estimation and silent
overflow, not over-pruning. Primary source wins — but this is the kind of thing to confirm with
a logged token count in Phase 0 rather than trusting either reading.

Same resolution applies to `best_practices.md`: upstream docs describe it feeding `/improve`,
but in *this* checkout `relevant_best_practices` is hard-set to `""`
(`pr_code_suggestions.py:191`) and the `[best_practices]` sections are read by no `.py`. Trust
the checkout.

Verified defaults, for the record (`configuration.toml`): `num_max_findings = 3` (:139),
`enable_large_pr_chunking = false` (:153), `max_number_of_calls = 3` (:154),
`focus_only_on_problems = true` (:200), `suggestions_score_threshold = 0` (:212),
`num_code_suggestions_per_chunk = 3` (:218), `max_suggestions_per_file = 0` (:220),
`duplicate_prompt_examples = false` (:56), `max_model_tokens = 32000` (:39),
`custom_model_max_tokens = -1` (:40).

Still unverified:


- No published head-to-head of local open-weight models vs GPT/Claude **specifically on PR
  review** was found. The "small models miss subtle concurrency/auth bugs" claim is an inference
  from SWE-bench gaps and bug-classification studies, not a measured PR-review result. Your
  Phase-0 harness is what settles it for your repo.
- No source combining **mutation testing or fuzzing with LLM triage** was found. The seeded-defect
  harness in §5 is a design proposal, not a cited practice.
- Codestral and Seed-Coder surfaced no code-review/bug-detection benchmark numbers.
- FP8 weight quantization for code tasks specifically: no figures found (the KV-cache numbers
  above are separate and do hold).
- Batch/parallel serving effects on review accuracy specifically: no direct study.
- Throughput and memory figures for this box are estimates from M4 bandwidth, not measured —
  Phase 2 measures them.
