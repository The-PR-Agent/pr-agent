# Review Quality Requirements

Source of weaknesses: Block Rush Review Audit (2026-09-10),
https://claude.ai/code/artifact/ae4951fa-5af8-47e3-b3ce-3ed1e346b23b.
PR-Agent reviewed samer2373/block_rush#1 (294 files, +42,006 / −1,574) with a Gemini Flash-class model in 4 chunks.
Two independent whole-repo reviews produced the labels; Codex (gpt-6-astra) critiqued the roadmap.

## Weaknesses measured

| ID | Weakness | Evidence | Root cause in code |
|---|---|---|---|
| W1 | Findings hidden, not lost | 3 of 19 ACTIVE findings visible | `_prepare_pr_review` renders only this run's `key_issues_to_review`; state marker keeps the rest (`pr_reviewer.py:1035-1156`) |
| W2 | Optimistic resolution | reconciler flips absent → RESOLVED after HEAD change | `reconcile_review_findings` (`review_finding_state.py:189`), guarded only by `allow_resolution` |
| W3 | Exact-hash identity | same test defect reported twice, reworded | `key_issue_fingerprint = sha256(path|body)[:12]`; `_same_finding` is overlap-first so unsafe to reuse across commits as-is (`review_merge.py:581`) |
| W4 | Partial disclosed, not recovered | 3 of 4 chunks failed, published with footnote | `_prepare_chunked_prediction` retries same chunk/model N times, then drops (`pr_reviewer.py:848`) |
| W5 | Coverage overstated | clipped patches counted as included | `large_patch_policy == 'clip'` branch keeps file in `files_in_patches` (`pr_processing.py:482-530`); per-file, never per-line |
| W6 | Cross-file hedged false positives | 4 of 4 FPs contain "unless / if X is not"; one-file lookup falsifies each | no verification step; no full-file or referenced-file fetch |
| W7 | Budget on unshipped files | 7 of 19 findings and ~40% of tokens on `design/*.html` mockups | nothing infers what ships; `[ignore]` is manual |
| W8 | Blind telemetry | one flat token total per run | `RunDetails` has no stage/file/chunk attribution (`run_details.py:22`) |
| W9 | No repo understanding | `repo_context` empty (no AGENTS.md); `best_practices` stubbed `""` | no onboarding; profile not generated |
| W10 | No PR-level assessment | senior-reviewer observations absent | prompt asks only for line-scoped issues |
| W11 | Security section wrong | "No security concerns" with unverified IAP | security is a boolean paragraph, not a lens with evidence |
| W12 | No ground truth | tool cannot say whether a change helped | eval harness covers mutants only (`tests/eval/`), no adjudicated real-PR labels |
| W13 | Recall ceiling is not the prompt | 6 rows, 3 wordings x 2 reps at equal coverage: best 1 of 23, control itself 1 then 0 (`tests/eval/BASELINE.md`, 2026-09-11) | wording of `key_issues_to_review` is not the limiter; the only label ever matched is hunk-local, every severity-4 label needs context beyond the diff |

## External evidence (web research, 2026-09-11)

Gathered before designing P2/P3, so the lens and retrieval work is not invented from scratch.

- **Recall in production tools comes from runtime context pulling plus multiple passes, not prompt
  wording.** CodeRabbit builds a *fresh per-PR* structural graph with shell tools (`cat`, `grep`,
  `ast-grep`) rather than pre-built embeddings, targets a ~1:1 code-to-context ratio, and runs a
  post-generation verification layer before posting
  (<https://www.coderabbit.ai/blog/context-engineering-ai-code-reviews>). Greptile pre-builds an AST
  code graph with per-node generated docstrings, then retrieves by vector + keyword + an agentic
  relevance step (<https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context>).
  Qodo 2.4 *removed* its RAG/indexing pipeline in favour of on-demand agentic fetching
  (<https://www.qodo.ai/blog/we-built-a-state-of-the-art-rag-system-for-code-review-in-qodo-2-4-we-took-most-of-it-out/>).
  This is consistent with W13: our wording experiment was always testing the wrong layer.
- **Cursor BugBot is a direct precedent for the over-caution problem and its fix.** Multiple parallel
  passes over *differently ordered* diffs combined by majority vote, plus a separate validator model
  for false positives; they state the agentic version became "too cautious". Their ground-truth
  metric is resolution rate (did the author fix it), raised 52% -> 70% over 40 experiments
  (<https://cursor.com/blog/building-bugbot>).
- **Precision in every vendor comes from an explicit second pass** - validator model (BugBot),
  verification layer (CodeRabbit), or thumbs-up/down retraining labels (CodeGuru,
  <https://aws.amazon.com/blogs/aws/new-for-amazon-codeguru-reviewer-detector-library-and-security-detectors-for-log-injection-flaws/>) -
  not from a single-pass "only report what you are sure about" instruction. R-8 already is that
  second pass; the open question is whether to default it on.
- **Lens decomposition has two vendor precedents.** Qodo runs specialised agents, one concern each,
  each emitting pass/fail rather than free text
  (<https://www.qodo.ai/blog/single-agent-vs-multi-agent-code-review/>); Greptile's agentic relevance
  step is the same idea applied to retrieval. Confirms R-19's direction.
- **Published recall is 12-44%, so our 4.3% is below the field but the field is not solved.**
  CodeReviewBench: best model 44.2% recall / 43.6% precision on 95 golden bugs, "no model finds even
  half" (<https://www.codereviewbench.com/>). SWR-Bench: best system 19.4% F1 over 1000 verified PRs
  with full-repo context, most approaches under 10% precision
  (<https://arxiv.org/html/2509.01494v1>).
- **Adjudication should be a two-pass judge**, deterministic first (normalised path + line-range
  overlap with +/-5 tolerance + type compatibility) resolving most cases, an LLM judge only for the
  remainder; SWR-Bench validated its LLM judge at ~90% agreement with three human experts. This is a
  better spec than R-1's current by-hand adjudication.
- **Power.** At n=23 labels a prompt/config A/B needs roughly +3 to +5 true positives before it is
  real, and >= 5 repetitions with a bootstrap CI; statistical testing is rare in this literature and
  N < 30 is flagged as needing caution. Recorded so future acceptances are not written at +1.
- **Gap:** no published work measures early stopping or position bias ("lost in the middle")
  specifically for code review on diffs. Our W13 result is, as far as this search found, the only
  measurement of it.

## Requirements

Format: **R-n (Wx)** requirement. *Acceptance:* checkable criterion. *Metric:* what proves it.

### Tier P0-A: measure before changing

- **R-1 (W12) Labeled real-PR corpus.** A fixture format for adjudicated findings on a real PR: path, line range, category, severity 1–4, label ∈ {TP, FP, OVERSTATED, MISSED}, and lowercase signal substrings. First entries: the 11 verdicts and 18 misses from block_rush#1. Include control entries (correct code the tool must not flag). *Acceptance:* `tests/eval/run_eval.py --labels tests/eval/labels/block_rush_pr1.json` prints precision, recall, severity-weighted recall, false-flag rate on controls, and cost. *Metric:* numbers reproducible across two runs with `temperature=0` within ±1 finding.
- **R-2 (W8) Per-call accounting.** Every model call records `run_id, tool, stage, chunk_index, sample_index, files, model, prompt_tokens, cached_tokens, completion_tokens, cost_usd, latency_ms, findings_emitted`. Written as JSONL when `config.run_ledger_path` is set. *Acceptance:* a chunked, sampled review yields one JSONL row per call and the sum equals `RunDetails.total_tokens`. *Metric:* 100% of tokens attributable to a stage and a file set.
- **R-3 (W5, W4) Line-level coverage ledger.** Per file: `changed_lines`, `status ∈ {reviewed, clipped, skipped_budget, chunk_failed, deletion_only, low_priority_summary, ignored}`. Footer shows `Reviewed X% of changed lines (N files not fully reviewed)`. *Acceptance:* a clipped file is counted as partially reviewed, never as reviewed; a failed chunk's files count as 0. *Metric:* zero silent omissions under injected clipping, budget cut and chunk failure in unit tests.

### Tier P0-B: fix the review loop

- **R-4 (W1) Render every ACTIVE finding.** Visible body has three groups: *New this run*, *Carried from earlier runs* (with first-seen date and whether the file was re-reviewed), *Resolved*. Human text gets the byte budget before the state marker; overflow drops RESOLVED entries from the marker first, then paginates carried findings to a second comment. *Acceptance:* visible count equals ACTIVE count on every run in tests. *Metric:* same on the block_rush replay.
- **R-5 (W2) Evidence-based resolution.** Absent finding becomes `UNCONFIRMED`, not RESOLVED, unless its file was fully reviewed this run (status `reviewed`, not clipped, chunk not failed) and it was not re-emitted. `UNCONFIRMED` findings stay visible under Carried with a tag. Schema version 2; v1 markers still parse. *Acceptance:* replay with an unchanged defect and a failed chunk keeps the finding ACTIVE/UNCONFIRMED. *Metric:* zero false resolutions in replay tests.
- **R-6 (W3) Safe cross-run identity.** Two findings are the same across runs when path matches AND line ranges overlap within tolerance AND (Jaccard wording ≥ 0.5 OR normalized `issue_header` equal). Fingerprint of the first observation stays the id. Same-root-cause findings across files merge into one with N locations only when header and wording match. *Acceptance:* the two `themes_screen_test.dart` debug-leftover findings collapse to one; two distinct issues 3 lines apart do not. *Metric:* duplicate rate and mistaken-merge rate on a moved-code fixture.
- **R-7 (W4) Bounded recovery to completion.** On a chunk failing all attempts: split its files in half and retry each half; then retry on `fallback_models[0]`; only then mark `chunk_failed`. When coverage < 95%, the partial notice renders at the top as a warning, not a footnote. *Acceptance:* a chunk that fails once at full size and succeeds at half size yields full coverage. *Metric:* coverage ≥ 95% or explicit pending state; p95 completion latency logged.
- **R-8 (W6) Premise verification.** Each finding (up to `verify_max_findings`) is checked by a cheap model given: the finding, full head content of its own file, and full content of any PR file whose basename appears in the finding text. Verdict ∈ {confirmed, refuted, unverified} with a quoted evidence line. Refuted findings are dropped and logged with the evidence; unverified are kept with a tag. Hedge phrases are a logged trigger signal, not the gate. *Acceptance:* on the block_rush replay, the four labeled FPs are refuted and the five TPs confirmed. *Metric:* cross-file FP rate on the corpus at fixed budget.
- **R-9 (W7) Ship-scope priority.** Files matching `pr_reviewer.low_priority_globs` (default: `docs/**`, `design/**`, `mockups/**`, `**/fixtures/**`, `**/*.md`) are ordered last in chunking and, when budget is tight, summarized in one line instead of reviewed; never silently dropped. Footer proposes `[ignore] glob` lines with estimated token savings for a human to accept. *Acceptance:* block_rush replay spends < 5% of tokens on `design/**` and recall on labeled TPs is unchanged. *Metric:* tokens on low-priority files; recall delta on the corpus.
    - **R-9a (amendment, 2026-09-11).** Ordering alone does not meet R-9's acceptance: the first
      baseline never bound its budget, so `design/**` still took 44-53% of tokens. A low-priority
      file whose patch exceeds `pr_reviewer.low_priority_max_tokens_per_file` (default 3000) is
      therefore summarized regardless of budget, on both the single-call and the chunked path.
      *Acceptance:* met (tests/eval/BASELINE.md, 2026-09-11). Attributing patch tokens to the
      files that reached a model call, `design/**` was 0.0% of the reviewed budget with the cap
      (2.9% for all low-priority files) against 55.8% without it, on the same flags with only
      this key changed - inside R-9's < 5% bar. The share is arithmetic over the diff and the
      ledger, so unlike a finding count it does not depend on n.
- **R-9b (amendment, 2026-09-11) Defaults that reach the whole PR.** `config.max_model_tokens`
  defaults to 200000 (was 32000) and `pr_reviewer.enable_large_pr_chunking` defaults to true. With
  the old defaults a 294-file PR reached 5 files and matched no label; the clamp is still applied
  per model, so a small-context model is unaffected. This raises the call count for large PRs on
  every install, bounded by `pr_reviewer.max_number_of_calls`. *Acceptance:* partially met
  (tests/eval/BASELINE.md, 2026-09-11). On the Cursor model line the old defaults produced a
  well-formed but empty review of a 2.4MB diff, while the new defaults reached the whole PR in
  2 merged chunks and reported a finding - so the defaults no longer starve a large PR. They
  did not improve recall: that finding is out-of-label, and both default rows score 0.000.

### Tier P1: `/setup` onboarding

- **R-10 (W9) Repo profile generation.** `/setup` produces `.pr_agent/repo_profile.md` as a PR with sections: stack and versions, architecture map, shipped vs unshipped paths, domain glossary and business invariants (mined from doc comments, docs/, decisions files, test names), risk map (path glob → tier), conventions (lint config, AGENTS.md / CLAUDE.md / .cursorrules, merged-PR review threads via `scan_repo_discussions`), toolchain commands. Each mined statement carries provenance (file, line or PR) and a source hash. *Acceptance:* generated profile for block_rush names Riverpod, in_app_purchase, the `design/` mockups as unshipped, and `lib/src/iap/**` as high risk.
- **R-11 (W9) Cached prefix injection.** Profile plus system prompt are placed first and byte-stable; per-PR content last. *Acceptance:* two consecutive reviews of different PRs on one repo show identical prompt prefix up to the diff. *Metric:* cached_tokens share in R-2 ledger.
- **R-12 Risk-map routing.** High-risk paths get more context lines, verification, and the strong model tier; low-risk get the weak tier or skip. *Acceptance:* config `[pr_reviewer.risk_tiers]` drives model selection per chunk. *Metric:* serious-defect recall per dollar versus all-cheap and all-strong baselines on the corpus.
- **R-13 Governable learnings.** Reactions, maintainer replies and "resolved without a code change" produce *proposed* rules with provenance, scope glob, and required approval; rules are editable, deletable and exportable; one PR cannot teach a global rule without approval. *Acceptance:* a 👎 on a finding creates a proposal entry, not an active suppression.
- **R-14 Untrusted repo content.** Mined text is data; the profile cannot suppress checks or run commands; global profile bounded at `repo_context_max_lines`, path-specific sections retrieved per file. *Acceptance:* an instruction embedded in a PR comment or source file to "skip security review" has no effect in tests.
- **R-15 Install hook.** GitHub App `installation.created` opens the setup PR per repo. *Acceptance:* handler branch exists in `github_app.py` and is unit-tested with a fixture payload.

### Tier P2: context beyond the diff

- **R-16 Symbol retrieval, one language first.** *Amended 2026-09-11 by research:* tree-sitter gives Dart **syntax only** - callers/callees need semantic resolution (imports, dispatch, type inference), which means the `analyzer` package / Dart analysis server, or SCIP on top of it. Use the analyzer as the spine and tree-sitter at most for fast symbol enumeration; reuse `scip_dart` (<https://pub.dev/packages/scip_dart>), Workiva/scip-dart or Infigraph's LSP->SCIP bridge rather than building an indexer. This also makes R-17 nearly free, since the analyzer is then already running. Prebuilt tree-sitter Dart wheels do exist (<https://pypi.org/project/tree-sitter-dart/>), so the grammar is not the obstacle - resolution is. Original text: tree-sitter index for Dart: for each changed symbol, definition + callers + callees within a reserved token cap, package-qualified, generated code skipped, traversal depth capped. *Acceptance:* the ad-timeout asymmetry (load vs show paths) is visible in retrieved context for `ads_service.dart` changes. *Metric:* cross-file FP rate and serious-defect recall on the corpus at fixed budget.
- **R-17 Static analysis first.** When a sandbox is available, run the repo's analyzer and feed results as verification targets; dedupe findings the linter already reports. *Acceptance:* `dart analyze` findings appear in the ledger as stage `static`.
- **R-18 Selective escalation.** Findings in high-risk paths touching ordering, concurrency or money go to the strong tier for verification. *Metric:* recall per dollar as in R-12.

### Tier P3: focused passes and PR-level view

- **R-19 Lenses added one at a time.** *Confirmed 2026-09-11 by research* - Qodo ships one agent per concern emitting pass/fail, and BugBot attributes its over-caution to asking a single prompt to be both broad and careful. Add a cheap precedent-backed variant first: permute diff order across `num_samples` consensus samples and vote, which is BugBot's mechanism and needs no new code path beyond ordering. Business-logic/invariants first, then async/lifecycle, persistence/atomicity, security/trust boundary, test quality; each kept only if the corpus shows recall gain at fixed budget.
- **R-20 (W11) Security as a lens with evidence.** "No security concerns" is emitted only when the security lens ran over all high-risk files and returned none. Otherwise "not assessed".
- **R-21 (W10) PR-level assessment.** 3–5 bullets about architecture, state boundaries, test hygiene, documentation drift, with file anchors. *Acceptance:* block_rush replay produces a bullet about the non-notifying service holder providers.
- **R-22 Structured output enforced server-side.** *Promoted 2026-09-11:* W13 exonerated the prompt wording, so this is the next suspect for the recall ceiling and comes before P1. Note the Cursor CLI shim cannot test it at all - it concatenates system+user onto stdin and has no `response_format` - so this needs a provider key with quota. `response_format` / tool-call schema for findings when the provider supports it. *Metric:* completion tokens per finding.

### Tier P4: self-improvement

- **R-23 Outcome capture.** Reactions, replies, and whether flagged lines changed before merge stored per finding as weak labels; preference feedback kept separate from correctness.
- **R-24 Shadow self-tune.** Proposed config changes (ignore globs, path instructions, lens weights, tiers) run in shadow against the corpus with repo and time holdouts; applied only on measured gain; rollback kept; suppression never inflates reported precision.

### Tier P5: hardening

- **R-25 Sandbox `/ask` and comment-driven overrides.** No repo code executes outside an isolated VM; comment-driven config changes limited to an allow-list.

## Global constraints

- Python ≥ 3.12, `uv sync`, tests via `PYTHONPATH=. uv run pytest tests/unittest -q`.
- New config keys default to current behavior (feature flags off) and are documented in `pr_agent/settings/configuration.toml`.
- New prompt files registered in `pr_agent/config_loader.py`.
- Ruff `E,F,B,I`; 120-char lines; no `ruff format`.
- Provider-specific behavior behind `provider.is_supported(...)` checks.
- Every P0 requirement ships with unit tests; R-1 corpus is the regression gate for everything after it.

## Out of scope for P0

Symbol indexing (R-16), sandbox execution (R-17, R-25), `/setup` (R-10–R-15), lenses (R-19–R-21). Each gets its own plan after P0 lands and the corpus baseline is recorded.
