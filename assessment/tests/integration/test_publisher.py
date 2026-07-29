from __future__ import annotations

import pytest

from assessment.ai_reviewer.contracts import Finding
from assessment.ai_reviewer.publisher import (
    StaleHeadError,
    finding_fingerprint,
    marker,
    publish_findings,
)
from assessment.tests.integration.fixtures import load_json, load_request


class FakeAdapter:
    def __init__(self, head_sha: str, fail_after_create: bool = False) -> None:
        self.head_sha = head_sha
        self.fail_after_create = fail_after_create
        self.bodies: list[str] = []
        self.create_calls = 0

    def current_head_sha(self) -> str:
        return self.head_sha

    def existing_review_bodies(self) -> tuple[str, ...]:
        return tuple(self.bodies)

    def create_review(self, head_sha: str, comments: list[dict]) -> None:
        self.create_calls += 1
        self.bodies.extend(str(comment["body"]) for comment in comments)
        if self.fail_after_create:
            raise TimeoutError("response status unknown")


def test_publish_is_idempotent_for_the_same_head() -> None:
    request = load_request()
    finding = Finding.from_dict(load_json("agent_findings.json")[0])
    adapter = FakeAdapter(request.head_sha)

    first = publish_findings(adapter, request, (finding,))
    second = publish_findings(adapter, request, (finding,))

    assert first.published == 1
    assert second.skipped == 1
    assert adapter.create_calls == 1
    expected = marker(finding_fingerprint(request, finding))
    assert expected in adapter.bodies[0]


def test_unknown_response_is_reconciled_without_replay() -> None:
    request = load_request()
    finding = Finding.from_dict(load_json("agent_findings.json")[0])
    adapter = FakeAdapter(request.head_sha, fail_after_create=True)

    report = publish_findings(adapter, request, (finding,))

    assert report.published == 1
    assert adapter.create_calls == 1


def test_stale_head_prevents_all_writes() -> None:
    request = load_request()
    finding = Finding.from_dict(load_json("agent_findings.json")[0])
    adapter = FakeAdapter("c" * 40)

    with pytest.raises(StaleHeadError):
        publish_findings(adapter, request, (finding,))

    assert adapter.create_calls == 0
