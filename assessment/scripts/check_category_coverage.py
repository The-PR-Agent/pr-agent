from __future__ import annotations

import json
from pathlib import Path

EXPECTED_CATEGORIES = {
    "architecture",
    "business_logic",
    "logic",
    "memory",
    "security",
    "static",
}
REQUIRED_FIELDS = {
    "case_id",
    "category",
    "target_file",
    "target_line",
    "expected_impact",
    "expected_detector",
}


def main() -> int:
    root = Path(__file__).parents[1] / "tests" / "fixtures" / "categories"
    cases = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    categories: set[str] = set()
    for index, case in enumerate(cases):
        missing = REQUIRED_FIELDS - set(case)
        if missing:
            errors.append(f"case {index} missing fields: {sorted(missing)}")
            continue
        categories.add(case["category"])
        if not (root / case["target_file"]).is_file():
            errors.append(f"missing fixture: {case['target_file']}")
        if case["expected_detector"] not in {"agent", "static"}:
            errors.append(f"invalid detector: {case['case_id']}")
        if not str(case["expected_impact"]).strip():
            errors.append(f"empty impact: {case['case_id']}")
    missing_categories = EXPECTED_CATEGORIES - categories
    if missing_categories:
        errors.append(f"missing categories: {sorted(missing_categories)}")
    if errors:
        print(json.dumps({"status": "failed", "errors": errors}, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "success",
                "categories": sorted(categories),
                "case_count": len(cases),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
