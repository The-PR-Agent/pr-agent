from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import Finding, ReviewRequest

TEMPLATE_IMPACTS = {
    "may cause issues",
    "may cause a problem",
    "potential risk",
    "this could be risky",
}


def verify_findings(
    request: ReviewRequest,
    candidates: Iterable[Mapping[str, Any]],
    static_findings: Iterable[Finding] = (),
) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
    accepted: list[Finding] = []
    warnings: list[str] = []
    seen: set[tuple[str, int, str, str]] = set()
    static_locations = {
        (finding.path, finding.line, finding.category)
        for finding in static_findings
    }

    for index, candidate in enumerate(candidates):
        if candidate.get("protected_by_existing_logic") is True:
            warnings.append(f"candidate {index} already has protection")
            continue
        raw_finding = {
            key: value
            for key, value in candidate.items()
            if key in Finding.__dataclass_fields__
        }
        raw_finding["source"] = "agent"
        try:
            finding = Finding.from_dict(raw_finding)
        except (KeyError, TypeError, ValueError) as error:
            warnings.append(
                f"candidate {index} rejected: {type(error).__name__}: "
                f"{str(error)[:160]}"
            )
            continue
        if finding.path not in request.changed_files:
            warnings.append(f"candidate {index} targets an unchanged file")
            continue
        if finding.line not in request.changed_lines.get(
            finding.path,
            frozenset(),
        ):
            warnings.append(f"candidate {index} targets an unchanged line")
            continue
        if not _has_specific_text(finding.evidence):
            warnings.append(f"candidate {index} has empty evidence")
            continue
        if not finding.impact.strip() or _is_template_impact(finding.impact):
            warnings.append(f"candidate {index} has an empty/template impact")
            continue
        if not finding.suggestion.strip():
            warnings.append(f"candidate {index} has an empty suggestion")
            continue
        if (
            finding.path,
            finding.line,
            finding.category,
        ) in static_locations:
            warnings.append(f"candidate {index} duplicates a static finding")
            continue
        fingerprint = (
            finding.path,
            finding.line,
            finding.category,
            _normalize(finding.title),
        )
        if fingerprint in seen:
            warnings.append(f"candidate {index} is a duplicate")
            continue
        seen.add(fingerprint)
        accepted.append(finding)

    return tuple(accepted), tuple(warnings)


def _has_specific_text(values: Iterable[str]) -> bool:
    return any(len(value.strip()) >= 12 for value in values)


def _is_template_impact(value: str) -> bool:
    return _normalize(value) in TEMPLATE_IMPACTS


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()).rstrip(".")
