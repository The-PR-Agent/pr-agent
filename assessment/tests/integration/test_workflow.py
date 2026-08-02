from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(".github/workflows/ai-review.yml")


def test_workflow_has_minimum_permissions_and_internal_head_guard() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "contents: read" in text
    assert "pull-requests: write" in text
    assert "write-all" not in text
    assert "pull_request_target:" in text
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in text
    )
    assert "timeout-minutes: 10" in text
    assert 'python-version: "3.12"' in text
    assert "persist-credentials: false" in text


def test_workflow_executes_only_the_trusted_base_checkout() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "path: _trusted_reviewer" in text
    assert "ref: ${{ github.event.pull_request.head.sha }}" in text
    assert "path: _pr_data" in text
    assert "working-directory: _trusted_reviewer" in text
    assert (
        "$GITHUB_WORKSPACE/_trusted_reviewer/assessment/"
        "requirements-agent.txt"
    ) in text
    assert "PYTHONPATH: ${{ github.workspace }}/_trusted_reviewer" in text
    assert '--repo-root "$GITHUB_WORKSPACE/_pr_data"' in text
    assert "working-directory: _pr_data" not in text


def test_workflow_uses_secrets_by_reference_and_always_uploads() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}" in text
    assert "if: always()" in text
    assert "assessment/requirements-agent.txt" in text
    assert "requirements.txt" not in text
    assert "sk-" not in text
