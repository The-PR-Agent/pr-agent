import json

import pytest

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers.github_action_runner import run_action
from pr_agent.servers.github_app import handle_push_trigger_for_new_commits


@pytest.mark.asyncio
async def test_github_push_trigger_skips_when_disable_auto_feedback(monkeypatch):
    settings = get_settings()
    original_handle_push_trigger = settings.github_app.handle_push_trigger
    original_push_commands = list(settings.github_app.push_commands)
    original_disable_auto_feedback = settings.config.disable_auto_feedback
    settings.github_app.handle_push_trigger = True
    settings.github_app.push_commands = ["/review"]
    settings.config.disable_auto_feedback = True

    monkeypatch.setattr("pr_agent.servers.github_app.apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(
        get_identity_provider().__class__,
        "verify_eligibility",
        lambda *args, **kwargs: Eligibility.ELIGIBLE,
    )

    ran = {"flag": False}

    async def fake_handle_request(self, pr_url, request, notify=None):
        ran["flag"] = True
        return True

    monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

    body = {
        "before": "abc123",
        "after": "def456",
        "pull_request": {
            "url": "https://example.com/fake/pr",
            "state": "open",
            "draft": False,
            "created_at": "2026-04-20T00:00:00Z",
            "updated_at": "2026-04-21T00:00:00Z",
            "merge_commit_sha": None,
        },
    }

    try:
        await handle_push_trigger_for_new_commits(
            body=body,
            event="pull_request",
            sender="tester",
            sender_id="123",
            action="synchronize",
            log_context={},
            agent=PRAgent(),
        )
        assert ran["flag"] is False
    finally:
        settings.github_app.handle_push_trigger = original_handle_push_trigger
        settings.github_app.push_commands = original_push_commands
        settings.config.disable_auto_feedback = original_disable_auto_feedback


@pytest.mark.asyncio
async def test_github_action_runner_skips_when_disable_auto_feedback(monkeypatch, tmp_path):
    settings = get_settings()
    original_disable_auto_feedback = settings.config.disable_auto_feedback
    original_pr_actions = settings.get("GITHUB_ACTION_CONFIG.PR_ACTIONS", None)
    settings.config.disable_auto_feedback = False
    settings.set("GITHUB_ACTION_CONFIG.PR_ACTIONS", ["opened"])

    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "opened",
        "pull_request": {
            "url": "https://example.com/fake/pr",
            "html_url": "https://github.com/example/repo/pull/1",
        },
    }))

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_apply_repo_settings(pr_url):
        get_settings().config.disable_auto_feedback = True

    monkeypatch.setattr("pr_agent.servers.github_action_runner.apply_repo_settings", fake_apply_repo_settings)

    ran = {"describe": False, "review": False, "improve": False}

    class FakeDescription:
        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            ran["describe"] = True

    class FakeReviewer:
        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            ran["review"] = True

    class FakeCodeSuggestions:
        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            ran["improve"] = True

    monkeypatch.setattr("pr_agent.servers.github_action_runner.PRDescription", FakeDescription)
    monkeypatch.setattr("pr_agent.servers.github_action_runner.PRReviewer", FakeReviewer)
    monkeypatch.setattr("pr_agent.servers.github_action_runner.PRCodeSuggestions", FakeCodeSuggestions)

    try:
        await run_action()
        assert ran == {"describe": False, "review": False, "improve": False}
    finally:
        settings.config.disable_auto_feedback = original_disable_auto_feedback
        if original_pr_actions is None:
            settings.unset("GITHUB_ACTION_CONFIG")
        else:
            settings.set("GITHUB_ACTION_CONFIG.PR_ACTIONS", original_pr_actions)
