"""/improve and /improve_code run /review once, with a one-line notice under the review heading."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent import pr_agent as agent_module
from pr_agent.algo.i18n import t
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings

get_settings().get("config")


def _record_reviews(monkeypatch):
    created = []

    class RecordingReviewer:
        def __init__(self, pr_url, args=None, ai_handler=None, **kwargs):
            created.append({"args": args, "kwargs": kwargs})

        async def run(self):
            return None

    monkeypatch.setattr(agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(agent_module, "flush_telemetry", lambda: None)
    for name in ("review", "improve", "improve_code"):
        monkeypatch.setitem(agent_module.command2class, name, RecordingReviewer)
    return created


@pytest.mark.parametrize("name", ["improve", "improve_code"])
async def test_improve_comment_runs_one_review_with_notice(monkeypatch, name):
    created = _record_reviews(monkeypatch)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        handled = await agent_module.PRAgent()._handle_request("https://example/pr/1", f"/{name}")

    assert handled is True
    assert len(created) == 1
    assert created[0]["kwargs"] == {
        "alias_notice": "/improve has been merged into /review and will be removed in the next version."
    }


async def test_review_has_no_alias_notice(monkeypatch):
    created = _record_reviews(monkeypatch)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        await agent_module.PRAgent()._handle_request("https://example/pr/1", "/review")

    assert created[0]["kwargs"] == {}


async def test_improve_incremental_runs_incremental_review(monkeypatch):
    created = _record_reviews(monkeypatch)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        await agent_module.PRAgent()._handle_request("https://example/pr/1", "/improve -i")

    assert created[0]["args"] == ["-i"]
    assert PRReviewer.__new__(PRReviewer).parse_incremental(created[0]["args"]).is_incremental


def test_review_renders_alias_notice_under_the_heading():
    provider = MagicMock()
    provider.is_supported.return_value = False
    provider.get_diff_files.return_value = []
    provider.supports_review_finding_state.return_value = False
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.alias_notice = t("improve.alias_notice")
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = "review: {}"
    reviewer.set_review_labels = MagicMock()
    reviewer._review_state_block_reason = None

    with (
        patch("pr_agent.commands.pr_reviewer.load_yaml", return_value={"review": {"key_issues_to_review": []}}),
        patch("pr_agent.commands.pr_reviewer.github_action_output"),
        patch("pr_agent.commands.pr_reviewer.convert_to_markdown_v2", return_value="## PR Reviewer Guide\nbody"),
    ):
        review = reviewer._prepare_pr_review()

    lines = [line for line in review.splitlines() if line.strip()]
    assert lines[0] == "## PR Reviewer Guide"
    assert lines[1] == "/improve has been merged into /review and will be removed in the next version."
