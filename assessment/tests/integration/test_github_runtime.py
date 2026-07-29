from __future__ import annotations

import json
from pathlib import Path

import pytest

from assessment.ai_reviewer.github_runtime import load_event

FIXTURE = Path(
    "assessment/tests/integration/fixtures/pull_request_event.json"
)


def test_loads_controlled_pull_request_event() -> None:
    event = load_event(FIXTURE)

    assert event.repository == "JiXia830/pr-agent"
    assert event.pr_number == 17
    assert event.same_repository is True
    assert event.base_sha == "a" * 40
    assert event.head_sha == "b" * 40


def test_rejects_unsupported_action(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["action"] = "closed"
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported"):
        load_event(event_path)
