"""config.disable_auto_feedback stops every automatic command, not only pr_commands.

The docs say that with the flag set PR-Agent runs no automatic tools "when a new PR is opened, or when
new code is pushed to an open PR". Each server's auto-command runner must honour it for every command
list it can be asked to run (pr_commands, push_commands, review_commands, reviewer_commands).
"""
from copy import deepcopy

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import (
    azuredevops_server_webhook,
    bitbucket_app,
    gitea_app,
    github_app,
    gitlab_webhook,
)
from pr_agent.servers.utils import auto_feedback_disabled

API_URL = "https://example.test/org/repo/pull/1"


class RecordingAgent:
    def __init__(self):
        self.commands = []

    async def handle_request(self, api_url, command, **kwargs):
        self.commands.append(command)


async def _github(conf, agent):
    body = {"action": "opened", "pull_request": {"draft": False}}
    return await github_app._perform_auto_commands_github(conf, agent, body, API_URL, {})


async def _gitlab(conf, agent):
    return await gitlab_webhook._perform_commands_gitlab(conf, agent, API_URL, {}, {"object_attributes": {}})


async def _gitea(conf, agent):
    return await gitea_app._perform_commands_gitea(conf, agent, {}, API_URL)


async def _bitbucket(conf, agent):
    return await bitbucket_app._perform_commands_bitbucket(conf, agent, API_URL, {}, {})


async def _azure(conf, agent):
    return await azuredevops_server_webhook._perform_commands_azure(conf, agent, API_URL, {})


# (id, runner, section holding the command lists, command lists to try)
SERVERS = [
    ("github_app", _github, "github_app", ["pr_commands", "push_commands", "review_commands"]),
    ("gitlab", _gitlab, "gitlab", ["pr_commands", "push_commands", "reviewer_commands"]),
    ("gitea", _gitea, "gitea", ["pr_commands", "push_commands"]),
    ("bitbucket_app", _bitbucket, "bitbucket_app", ["pr_commands", "push_commands"]),
    ("azure_devops_server", _azure, "azure_devops_server", ["pr_commands", "push_commands"]),
]
CASES = [(name, runner, section, conf) for name, runner, section, confs in SERVERS for conf in confs]


@pytest.fixture
def auto_settings(monkeypatch):
    """Command lists configured, repository settings left alone, flag set by the test; restored afterwards."""
    settings = get_settings()
    sections = ("github_app", "gitlab", "gitea", "bitbucket_app", "azure_devops_server")
    originals = {name: deepcopy(settings.get(name)) for name in (*sections, "config")}
    monkeypatch.setattr(bitbucket_app, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", lambda url: None)
    # an empty payload would be rejected here, before the gate under test
    monkeypatch.setattr("pr_agent.servers.gitlab_webhook.should_process_pr_logic", lambda data: True)
    try:
        for section in sections:
            for conf in ("pr_commands", "push_commands", "review_commands", "reviewer_commands"):
                settings.set(f"{section}.{conf}", ["/review"])
            settings.set(f"{section}.handle_push_trigger", True)
        yield settings
    finally:
        for name, original in originals.items():
            settings.set(name, original)


def test_the_gate_follows_the_setting(auto_settings):
    auto_settings.set("config.disable_auto_feedback", False)
    assert auto_feedback_disabled() is False
    auto_settings.set("config.disable_auto_feedback", True)
    assert auto_feedback_disabled() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name,runner,section,conf", CASES, ids=[f"{c[0]}-{c[3]}" for c in CASES])
async def test_no_auto_command_runs_when_auto_feedback_is_disabled(auto_settings, name, runner, section, conf):
    auto_settings.set("config.disable_auto_feedback", True)
    agent = RecordingAgent()

    await runner(conf, agent)

    assert agent.commands == []
