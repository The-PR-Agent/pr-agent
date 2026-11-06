"""Compute /review's published merge recommendation from deterministic checks.

The model describes what it found, but a clean recommendation is only trustworthy when
facts it cannot see agree: CI on the reviewed head, a head that is current with its base,
human authors, no sensitive paths or risky shapes, and a bounded diff. A check that cannot
be evaluated fails, so a provider that does not expose it cannot publish
`no_concerns_found`. The model's own recommendation and risk level may only veto a clean
result, never raise a blocked one.
"""

from __future__ import annotations

import html
from typing import Any

from pr_agent.algo.deterministic_risk import build_risk_checks
from pr_agent.algo.git_patch_processing import count_patch_lines
from pr_agent.config_loader import get_settings

MERGE_RECOMMENDATIONS = ("no_concerns_found", "needs_review", "changes_required")
RISK_LEVELS = ("low", "medium", "high")
DEFAULT_MAX_CHANGED_LINES = 100
_RECOMMENDATION_RANK = {value: rank for rank, value in enumerate(MERGE_RECOMMENDATIONS)}
_RISK_RANK = {value: rank for rank, value in enumerate(RISK_LEVELS)}
_RANK_RECOMMENDATION = dict(enumerate(MERGE_RECOMMENDATIONS))

_UNSET = object()


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _read_provider_check(git_provider: Any, method_name: str) -> Any:
    """Call one optional provider capability, treating an error as unavailable."""
    method = getattr(git_provider, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _ci_check(git_provider: Any) -> dict:
    status = _read_provider_check(git_provider, "get_head_ci_status")
    if status == "passed":
        return _check("CI on reviewed head", True, "passed")
    if status is None:
        return _check("CI on reviewed head", False, "status unavailable")
    return _check("CI on reviewed head", False, str(status))


def _staleness_check(git_provider: Any) -> dict:
    stale = _read_provider_check(git_provider, "is_head_stale_against_base")
    if stale is False:
        return _check("Head is current with its base", True, "up to date")
    if stale is True:
        return _check("Head is current with its base", False, "base branch changed the reviewed files")
    return _check("Head is current with its base", False, "status unavailable")


def _authors_check(git_provider: Any) -> dict:
    human = _read_provider_check(git_provider, "are_all_authors_human")
    if human is True:
        return _check("Human authors", True, "all authors are human")
    if human is False:
        return _check("Human authors", False, "bot or unlinked author")
    return _check("Human authors", False, "status unavailable")


def _risk_check(diff_files: list | None, risk_checks: Any) -> dict:
    if risk_checks is _UNSET:
        try:
            risk_checks = build_risk_checks(diff_files)
        except Exception:
            risk_checks = None
    if not isinstance(risk_checks, dict) or not isinstance(risk_checks.get("has_findings"), bool):
        return _check("No sensitive paths or risky changes", False, "risk checks unavailable")
    if risk_checks["has_findings"]:
        count = sum(len(risk_checks.get(key) or []) for key in ("sensitive_paths", "risky_shapes", "deleted_files"))
        return _check("No sensitive paths or risky changes", False, f"{count} finding(s)")
    return _check("No sensitive paths or risky changes", True, "none detected")


def count_changed_lines(diff_files: list | None) -> int | None:
    """Count added and removed lines across the diff, or None when a patch cannot be counted."""
    if not isinstance(diff_files, (list, tuple)):
        return None
    total = 0
    for file in diff_files:
        plus = getattr(file, "num_plus_lines", -1)
        minus = getattr(file, "num_minus_lines", -1)
        if isinstance(plus, int) and plus >= 0 and isinstance(minus, int) and minus >= 0:
            total += plus + minus
            continue
        patch = getattr(file, "patch", None)
        if patch is None:
            return None
        try:
            counted = count_patch_lines(patch)
        except Exception:
            return None
        total += sum(counted)
    return total


def _max_changed_lines() -> int | None:
    value = get_settings().pr_reviewer.get("max_changed_lines", DEFAULT_MAX_CHANGED_LINES)
    if isinstance(value, bool):
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cap if cap >= 0 else None


def _changed_lines_check(diff_files: list | None, cap: int | None = None) -> dict:
    cap = _max_changed_lines() if cap is None else cap
    if cap is None:
        return _check("Changed lines within cap", False, "cap is invalid")
    total = count_changed_lines(diff_files)
    if total is None:
        return _check("Changed lines within cap", False, "changed lines unavailable")
    if total <= cap:
        return _check("Changed lines within cap", True, f"{total} changed lines (cap {cap})")
    return _check("Changed lines within cap", False, f"{total} changed lines exceeds cap {cap}")


def _model_recommendation(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in _RECOMMENDATION_RANK else None


def _model_risk(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in _RISK_RANK else None


def compute_merge_verdict(git_provider: Any,
                          diff_files: list | None,
                          model_recommendation: Any = None,
                          model_risk_level: Any = None,
                          risk_checks: Any = _UNSET,
                          max_changed_lines: int | None = None) -> dict:
    """Return the deterministic recommendation plus the checks that decided it.

    `risk_checks` lets the caller reuse the row already built for the review, so the diff
    is not scanned twice. Passing `max_changed_lines` overrides the configured cap.
    """
    checks = [
        _ci_check(git_provider),
        _staleness_check(git_provider),
        _authors_check(git_provider),
        _risk_check(diff_files, risk_checks),
        _changed_lines_check(diff_files, max_changed_lines),
    ]
    failures = [check["name"] for check in checks if not check["passed"]]
    base_rank = 0 if not failures else _RECOMMENDATION_RANK["needs_review"]

    recommendation = _model_recommendation(model_recommendation)
    risk = _model_risk(model_risk_level)
    recommendation_rank = _RECOMMENDATION_RANK.get(recommendation, 0)
    risk_rank = _RISK_RANK.get(risk, 0)

    vetoes = []
    if recommendation_rank > base_rank:
        vetoes.append(f"model merge recommendation: {recommendation}")
        checks.append(_check("Model merge recommendation", False, recommendation))
    if risk_rank > base_rank:
        vetoes.append(f"model risk level: {risk}")
        checks.append(_check("Model risk level", False, risk))

    final_rank = max(base_rank, recommendation_rank, risk_rank)
    return {
        "recommendation": _RANK_RECOMMENDATION[final_rank],
        "checks": checks,
        "decided_by": failures + vetoes,
    }


def render_merge_recommendation_checks(checks: Any, gfm_supported: bool) -> str:
    """Render the deterministic checks that decided the published recommendation."""
    if not isinstance(checks, list):
        return ""
    lines = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or "").strip()
        if not name:
            continue
        detail = str(check.get("detail") or "").strip()
        mark = "✅" if check.get("passed") is True else "⚠️"
        text = f"{html.escape(name)}: {html.escape(detail)}" if detail else html.escape(name)
        lines.append(f"{mark} {text}")
    if not lines:
        return ""
    if gfm_supported:
        return ("<tr><td><strong>Merge recommendation checks</strong><br>\n"
                + "<br>\n".join(lines) + "\n</td></tr>\n")
    return "### Merge recommendation checks\n\n" + "\n".join(f"- {line}" for line in lines) + "\n\n"
