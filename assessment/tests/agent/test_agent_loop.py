from __future__ import annotations

import time
from pathlib import Path

from assessment.ai_reviewer.agent_loop import review
from assessment.ai_reviewer.contracts import ReviewRequest
from assessment.ai_reviewer.model_adapter import ModelResponse


class FakeModel:
    def __init__(self) -> None:
        self.responses = [
            ModelResponse(
                content=(
                    '{"context_requests":[{"tool":"read_file",'
                    '"arguments":{"relative_path":"src/example.py",'
                    '"start_line":1,"end_line":20}}]}'
                ),
                model="deepseek-v4-pro",
                request_id="analyze",
                usage={},
            ),
            ModelResponse(
                content=(
                    '{"findings":[{"path":"src/example.py","line":1,'
                    '"category":"logic","severity":"high",'
                    '"confidence":0.91,"title":"Wrong return value",'
                    '"evidence":['
                    '"The function returns False unconditionally."],'
                    '"impact":"Valid calls always receive the wrong result.",'
                    '"suggestion":"Return the calculated boolean value.",'
                    '"source":"agent"}]}'
                ),
                model="deepseek-v4-pro",
                request_id="review",
                usage={},
            ),
        ]

    def complete(self, messages, deadline_monotonic):
        assert messages
        assert deadline_monotonic > time.monotonic()
        return self.responses.pop(0)


class NonFiniteModel:
    def complete(self, messages, deadline_monotonic):
        return ModelResponse(
            content=(
                '{"context_requests":[{"tool":"search_code",'
                '"arguments":{"query":"value","max_hits":1e999}}]}'
            ),
            model="deepseek-v4-pro",
            request_id="non-finite",
            usage={},
        )


def test_agent_runs_all_stages_and_a_read_only_tool(tmp_path: Path) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("def allowed(): return False\n", encoding="utf-8")
    request = ReviewRequest(
        repository="owner/repo",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Fix result",
        body="Synthetic request",
        diff="synthetic diff",
        changed_lines={"src/example.py": frozenset({1})},
        changed_files=("src/example.py",),
    )

    result = review(
        request,
        [],
        tmp_path,
        time.monotonic() + 90,
        model_client=FakeModel(),
    )

    stages = [item["stage"] for item in result.trace_summary]
    assert result.status == "success"
    assert len(result.findings) == 1
    assert stages == [
        "analyze",
        "gather",
        "context_request",
        "tool_result",
        "gather",
        "review",
        "verify",
        "return",
    ]
    assert result.trace_summary[0]["model_call"] == 1
    assert result.trace_summary[5]["model_call"] == 2
    assert "content" not in result.to_dict()["trace_summary"][3]


def test_agent_returns_controlled_failure_for_non_finite_model_number(
    tmp_path: Path,
) -> None:
    (tmp_path / "example.py").write_text("value = 1\n", encoding="utf-8")
    request = ReviewRequest(
        repository="owner/repo",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Non-finite response",
        body="Synthetic request",
        diff="synthetic diff",
        changed_lines={"example.py": frozenset({1})},
        changed_files=("example.py",),
    )

    result = review(
        request,
        [],
        tmp_path,
        time.monotonic() + 90,
        model_client=NonFiniteModel(),
    )

    assert result.status == "failed"
    assert result.findings == ()
    assert result.errors
    assert result.trace_summary[-1]["status"] == "failed"
