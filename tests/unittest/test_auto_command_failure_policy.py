"""Regression tests for the opt-in automatic command failure policy."""

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import github_app


class _FailingAgent:
    def __init__(self):
        self.commands = []

    async def handle_request(self, _api_url, command):
        self.commands.append(command)
        return len(self.commands) > 1


@pytest.mark.asyncio
async def test_auto_commands_stop_after_a_failed_command_when_enabled(monkeypatch):
    settings = get_settings()
    original_github_app = settings.get("GITHUB_APP")
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    original_stop_on_failure = settings.get("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE")
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", True)
    settings.set("GITHUB_APP.PR_COMMANDS", ["/review", "/improve"])
    settings.set("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE", True)

    agent = _FailingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)

    try:
        await github_app._perform_auto_commands_github(
            "pr_commands",
            agent,
            {
                "action": "opened",
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    "draft": False,
                },
            },
            "https://api.github.com/repos/org/repo/pulls/1",
            {},
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)
        settings.set("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE", original_stop_on_failure)

    assert agent.commands == [["/review"]]


@pytest.mark.asyncio
async def test_auto_commands_continue_after_a_failed_command_by_default(monkeypatch):
    settings = get_settings()
    original_github_app = settings.get("GITHUB_APP")
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    original_stop_on_failure = settings.get("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE")
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", True)
    settings.set("GITHUB_APP.PR_COMMANDS", ["/review", "/improve"])
    settings.set("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE", False)

    agent = _FailingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)

    try:
        await github_app._perform_auto_commands_github(
            "pr_commands",
            agent,
            {
                "action": "opened",
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    "draft": False,
                },
            },
            "https://api.github.com/repos/org/repo/pulls/1",
            {},
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)
        settings.set("CONFIG.AUTO_COMMAND_STOP_ON_FAILURE", original_stop_on_failure)

    assert agent.commands == [["/review"], ["/improve"]]
