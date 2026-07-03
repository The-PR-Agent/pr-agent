---
phase: 02-angular-convention-title-rewriting
verified: 2026-07-03T04:23:53Z
status: passed
score: 11/11 must-haves verified
behavior_unverified: 0
overrides_applied: 0
re_verification:
  previous_status: passed
  previous_score: 5/5
  gaps_closed:
    - "CR-01 review blocker closed: publish_description_as_comment now uses sanitized title_to_publish instead of pr_title.strip() in conventional mode."
    - "Missing, empty, and non-string AI title cases publish body with title None / blank comment title under conventional mode for generate_ai_title false and true."
  gaps_remaining: []
  regressions: []
---

# Phase 2: Angular-convention Title Rewriting Verification Report

**Phase Goal:** When `enable_conventional_title` is on, `describe` publishes a valid Angular-convention title (`type(scope): summary`) to the GitLab MR - with a Python validator that repairs common defects and safely falls back to leaving the original title untouched when the AI output cannot be salvaged.
**Verified:** 2026-07-03T04:23:53Z
**Status:** passed
**Re-verification:** Yes - after fix commits through `f7f6b072`

MVP mode note: ROADMAP marks phase 2 `mode: mvp`, but `user-story.validate` returns `false` for the non-canonical ROADMAP goal. Per this final re-verification request, this report verifies the explicit ROADMAP success criteria plus prior gaps/review findings.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | With `enable_conventional_title=true`, the published MR title matches the target Angular regex. | VERIFIED | `_normalize_angular_title` returns output only after `_ANGULAR_TITLE_RE.fullmatch` in `pr_agent/tools/pr_description.py:140-143`; direct probe over fixture corpus found every non-None output regex-valid. |
| 2 | Enabling `enable_conventional_title` alone publishes the rewritten AI title without requiring `generate_ai_title=true`. | VERIFIED | Publish seam uses `self.ai_title` when `_conv_on` is true in `pr_agent/tools/pr_description.py:289-298`; matrix test covers `generate_ai_title=false`, conventional true. |
| 3 | Adversarial fixtures repair to valid title or `None`; GitLab never receives empty or malformed title. | VERIFIED | `tests/unittest/test_normalize_angular_title.py:18-44` includes adversarial corpus; fallback tests assert title arg `is None`, never empty. |
| 4 | Validator is a pure helper and `_prepare_pr_answer` inline logic is not mutated. | VERIFIED | `_normalize_angular_title` at `pr_agent/tools/pr_description.py:94-143` uses only string/re/constants; diff since phase start shows no prompt TOML edit and no `_prepare_pr_answer*` title-selection edits. |
| 5 | Toggle-off behavior remains byte-identical to Phase 1/upstream defaults. | VERIFIED | `tests/unittest/test_describe_byte_identical_when_off.py` included in focused regression run; suite passed. |
| 6 | `_normalize_angular_title("feat: AB")` returns `None`. | VERIFIED | Fixture at `tests/unittest/test_normalize_angular_title.py:34`; direct probe output: `feat: AB -> None`. |
| 7 | Every non-None normalizer output matches the roadmap target regex. | VERIFIED | Regex property test at `tests/unittest/test_normalize_angular_title.py:58-62`; direct probe also asserted every runtime output. |
| 8 | Missing, empty, whitespace, and non-string AI title under conventional mode publishes body with title `None`, no crash, and no human title fallback for `generate_ai_title=false` and `true`. | VERIFIED | `test_conventional_title_missing_or_malformed_ai_title_publishes_body_without_title` covers both generate modes and `_MISSING`, `""`, `"   "`, `["bad"]`; full run passed. |
| 9 | `publish_description_as_comment` path does not use `pr_title.strip()` in conventional mode and does not crash on non-string AI title. | VERIFIED | `title_to_publish` is computed before comment branch and comment body uses `title_to_publish or ''` in `pr_agent/tools/pr_description.py:289-309`; `test_conventional_title_comment_mode_uses_sanitized_ai_title` covers missing, empty, and list-valued AI title. |
| 10 | Global `extra_instructions` is not mutated or repeatedly appended. | VERIFIED | `__init__` writes only `self.vars["extra_instructions"]` in `pr_agent/tools/pr_description.py:192-193`; test asserts settings value unchanged and each instance has one block. |
| 11 | Toggle-off behavior still matches defaults-off tests after final fixes. | VERIFIED | Focused regression run included defaults-off and output-core tests: `74 passed in 20.65s`. |

**Score:** 11/11 truths verified (0 present, behavior-unverified)

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `pr_agent/tools/pr_description.py` | Pure validator, AI-title stash, conventional publish seam, comment-mode safe path, per-instance steering | VERIFIED | Lines 94-143 normalize; lines 224-228 sanitize AI title; lines 289-309 route normal/comment publish through `title_to_publish`. |
| `tests/unittest/test_normalize_angular_title.py` | Adversarial fixtures plus regex-conformance property | VERIFIED | Includes `feat: AB -> None` and property over every non-None expected output. |
| `tests/unittest/test_conventional_title_publish_seam.py` | Publish matrix, malformed AI fallback, comment-mode regression, steering non-mutation | VERIFIED | Covers generate false/true, conventional true/false, missing/empty/non-string AI titles, comment mode, and one-shot steering. |
| `tests/unittest/test_pr_description_output_core.py` | Regression coverage for describe output behavior | VERIFIED | Included in targeted `74 passed` run. |
| `tests/unittest/test_describe_byte_identical_when_off.py` | Defaults-off byte-identical fixture | VERIFIED | Included in targeted `74 passed` run. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| AI prediction title | `self.ai_title` | `raw_ai_title = self.data.get('title')`; only non-empty strings are stripped/stashed | VERIFIED | Missing, blank, whitespace, and list-valued titles become `None`. |
| `enable_conventional_title` | title publish argument | `_conv_on` branch selects `self.ai_title` before optional `_normalize_angular_title` | VERIFIED | Conventional-only matrix case publishes normalized AI title. |
| Validator failure | Git provider title untouched | `_normalize_angular_title(...)` returns `None`; provider path receives `publish_description(None, pr_body)` | VERIFIED | Missing/malformed tests assert body is published and title arg is `None`. |
| Conventional comment mode | comment body title section | Same `title_to_publish` feeds `full_markdown_description`; no `pr_title.strip()` in comment branch | VERIFIED | Regression test covers missing, empty, and non-string AI titles with `publish_description_as_comment=True`. |
| Extra-instructions steering | prompt vars only | `self.vars["extra_instructions"] = existing + _ANGULAR_TITLE_INSTRUCTIONS` | VERIFIED | Test asserts global settings value remains unchanged and no repeated append per instance. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `PRDescription.run` | `self.ai_title` | AI YAML loaded by `_prepare_data()` into `self.data["title"]` | Yes | FLOWING |
| Publish seam | `title_to_publish` | `self.ai_title` in conventional mode, `pr_title.strip()` only when conventional mode is off | Yes | FLOWING |
| Comment publish path | `full_markdown_description` title block | `title_to_publish or ''` | Yes | FLOWING - malformed conventional titles become blank title section, not human title fallback or crash. |
| `_normalize_angular_title` | normalized title | untrusted AI title string | Yes | FLOWING - final regex gate blocks malformed non-None output. |
| Extra-instructions steering | `self.vars["extra_instructions"]` | existing runtime setting plus local steering constant | Yes | FLOWING - local vars mutation only. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Focused phase regression suite | `$env:PYTHONPATH='.'; python -m pytest tests/unittest/test_normalize_angular_title.py tests/unittest/test_conventional_title_publish_seam.py tests/unittest/test_pr_description_output_core.py tests/unittest/test_describe_byte_identical_when_off.py -q -p no:cacheprovider` | `74 passed in 20.65s` | PASS |
| Direct normalizer probe | Inline Python importing `_normalize_angular_title`, `ANGULAR_TITLE_CASES`, `TARGET_TITLE_RE` | `direct-probe-ok: feat: AB -> None; all non-None outputs match target regex` | PASS |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| Phase probes | `Get-ChildItem scripts -Recurse -Filter probe-*.sh` and phase doc probe scan | No declared or conventional probe scripts found for this phase | SKIPPED |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| CFG-04 | `02-02-PLAN.md` | `enable_conventional_title` auto-forces AI title publishing | SATISFIED | `_conv_on` branch uses `self.ai_title`; behavior matrix passes without `generate_ai_title`. |
| TITLE-01 | `02-02-PLAN.md` | AI title generation steered to Angular format | SATISFIED | Effective `extra_instructions` contains type set and `type(scope): summary`; global setting unchanged. |
| TITLE-02 | `02-01-PLAN.md` | Commit type constrained to Angular set | SATISFIED | `_ANGULAR_TITLE_RE` and fixture tests constrain non-None output type set. |
| TITLE-03 | `02-01-PLAN.md` | Subject lowercase first letter and no trailing period | SATISFIED | Fixture tests cover capitalization and trailing period repair. |
| TITLE-04 | `02-01-PLAN.md` | Scope kebab-case or omitted, never empty parens | SATISFIED | Fixture tests cover empty, whitespace, spaced, and invalid scope handling. |
| TITLE-05 | `02-01-PLAN.md` | Validator repairs common defects or falls back untouched | SATISFIED | Unsalvageable inputs return `None`; publish seam passes `None`. |
| TITLE-06 | `02-01-PLAN.md` | Empty/whitespace AI title never sets empty GitLab title | SATISFIED | Missing/empty/whitespace/non-string title tests publish body with `None` / blank comment title. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| None | - | Anti-pattern scan found no TODO/FIXME/XXX/placeholders/empty implementation markers in phase files | - | - |

### Human Verification Required

None.

### Gaps Summary

No gaps remain. The final fix `f7f6b072` closes review finding CR-01 by routing comment-mode publishing through the same sanitized `title_to_publish` path as provider-title publishing. Targeted tests and direct probes pass.

---

_Verified: 2026-07-03T04:23:53Z_
_Verifier: the agent (gsd-verifier)_
