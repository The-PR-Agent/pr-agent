---
phase: 02-angular-convention-title-rewriting
secured: 2026-07-03T07:30:00Z
asvs_level: 1
block_on: high
threats_open: 0
status: passed
---

# Phase 02 Security: Angular-Convention Title Rewriting

## Threat Verification

| Threat ID | Status | Evidence |
|-----------|--------|----------|
| T-02-01 | CLOSED | `_normalize_angular_title()` strips decorations/collapses whitespace before parsing; adversarial fixtures pass. |
| T-02-02 | CLOSED | `_MAX_SUMMARY` and final regex gate cap title output; long-summary fixtures pass. |
| T-02-03 | CLOSED | Malformed titles return `None`, and publish seam passes `None` to leave GitLab title untouched. |
| T-02-04 | CLOSED | Publish seam uses `None`, never `""`, for invalid conventional titles; fallback tests assert identity `is None`. |
| T-02-05 | ACCEPTED | Auto-force publishing when `enable_conventional_title=true` is intended CFG-04 behavior; toggle defaults off. |
| T-02-06 | CLOSED | Runtime steering appends only to per-instance `self.vars["extra_instructions"]`; global settings are not mutated. |
| T-02-07 | CLOSED | Defaults-off tests and output-core regression suite pass after title wiring. |

## Accepted Risks

| Threat ID | Rationale |
|-----------|-----------|
| T-02-05 | Operator enabling `enable_conventional_title` explicitly opts into AI-title publish behavior; config remains default-off. |

## Security Audit

| Metric | Count |
|--------|-------|
| Threats found | 7 |
| Closed | 7 |
| Open | 0 |

