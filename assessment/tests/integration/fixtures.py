from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assessment.ai_reviewer import ReviewRequest, ReviewResult

FIXTURE_ROOT = Path(__file__).with_name("fixtures")


def load_json(name: str) -> Any:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def load_request() -> ReviewRequest:
    return ReviewRequest.from_dict(load_json("review_request.json"))


def load_result() -> ReviewResult:
    return ReviewResult.from_dict(load_json("review_result.json"))
