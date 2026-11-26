"""Scoring and the pass/fail gate for the review-quality eval. Pure functions, no model calls."""
import math
import re

POINTS = {"caught": 1.0, "partial": 0.5, "missed": 0.0}
# Half-width of the run-to-run noise on the catch score: on the fuse model tests a change below about 20% of the
# known findings was indistinguishable from repeat-to-repeat variance with one repeat, shrinking with sqrt(repeats).
NOISE_FRACTION = 0.2
TITLE = re.compile(r"^\*\*([^*\n]+)\*\*\s*$", re.MULTILINE)
FINDINGS_START = "Recommended focus areas"
FINDINGS_END = "Agent run details"
RENDERED_FIELDS = ("relevant_file", "issue_header", "issue_content")


def findings_from_review(review):
    """[(title, body)] from the structured review pr-agent exports (review.json): the key issues in order, each body
    carrying its location and any suggested fix, independent of the comment's language and markdown."""
    issues = (review or {}).get("review", {}).get("key_issues_to_review") or []
    out = []
    for issue in issues if isinstance(issues, list) else []:
        # The same issues the comment renderer skips (pr_agent/algo/utils.py), so only posted findings count.
        if not issue or not isinstance(issue, dict) or any(
                f in issue and not isinstance(issue[f], str) for f in RENDERED_FIELDS):
            continue
        body = f"{str(issue.get('relevant_file', '')).strip()} lines {issue.get('start_line')}-" \
               f"{issue.get('end_line')}\n{str(issue.get('issue_content', '')).strip()}"
        if issue.get("improved_code"):
            body += f"\nSuggested fix:\n{issue.get('existing_code') or ''}\n->\n{issue['improved_code']}"
        out.append((str(issue.get("issue_header", "")).strip(), body.strip()))
    return out


def findings(review_md):
    """[(title, body)] of the findings in a /review comment, in order (fallback when no review.json exists). Fix
    sub-headings and bold lines inside code fences stay in their finding."""
    if FINDINGS_START not in review_md:
        return []
    text = review_md.split(FINDINGS_START, 1)[1].split(FINDINGS_END, 1)[0]
    fences = [i for i, line in enumerate(text.splitlines(keepends=True)) if line.lstrip().startswith("```")]
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    # An unclosed trailing fence has no pair and is ignored.
    fenced = [(offsets[a], offsets[b + 1]) for a, b in zip(fences[::2], fences[1::2], strict=False)]
    ms = [m for m in TITLE.finditer(text)
          if not m.group(1).strip().lower().startswith(("suggested fix", "⚙️"))
          and not any(a <= m.start() < b for a, b in fenced)]
    return [(m.group(1).strip(), text[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(text)].strip())
            for i, m in enumerate(ms)]


def finding_points(verdict_a, verdict_b):
    """Average of two judges' verdicts for one known finding; an unknown verdict counts as missed."""
    return (POINTS.get(str(verdict_a).lower(), 0.0) + POINTS.get(str(verdict_b).lower(), 0.0)) / 2


def noise_floor(n_findings, repeats):
    return NOISE_FRACTION * n_findings / math.sqrt(max(repeats, 1))


def gate(current, baseline, repeats):
    """(failures, notes) comparing this run's metrics with the baseline's.

    Fails on any failed run, on output-cap hits in more than 10% of runs, and on a catch drop beyond the noise floor.
    Precision and volume changes are reported, not failed: their counts are too small to gate on."""
    failures, notes = [], []
    if current["failed_runs"]:
        failures.append(f"{current['failed_runs']} of {current['runs']} runs failed")
    if current["runs"] and current["capped_runs"] / current["runs"] > 0.1:
        failures.append(f"{current['capped_runs']} of {current['runs']} runs hit the output-token cap")
    if not baseline:
        notes.append("no baseline: nothing to compare with")
        return failures, notes
    if baseline.get("cases_hash") != current.get("cases_hash") or baseline.get("tier") != current.get("tier"):
        notes.append("baseline was made on a different case set or tier: not compared")
        return failures, notes
    floor = noise_floor(current["known_findings"], repeats)
    delta = current["catch"] - baseline["catch"]
    line = f"catch {current['catch']:.2f} vs baseline {baseline['catch']:.2f} of {current['known_findings']} " \
           f"({delta:+.2f}; noise floor {floor:.1f})"
    (failures if delta < -floor else notes).append(line)
    for key, label in (("valid", "valid findings"), ("posted", "posted findings"), ("findings_per_run",
                                                                                     "findings per run")):
        if key in baseline and key in current and (baseline[key] or current[key]):
            notes.append(f"{label} {current[key]:.4g} vs baseline {baseline[key]:.4g}")
    if baseline.get("judge_model") and baseline["judge_model"] != current.get("judge_model"):
        notes.append(f"judge model differs from the baseline's ({baseline['judge_model']}): compare with care")
    return failures, notes
