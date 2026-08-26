import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.agent.pr_agent import PRAgent, command2class


def test_help_docs_is_not_registered():
    """Security stopgap for issue #2445: the /help_docs command must be disabled
    (unregistered) until the clone-target validation fix is merged."""
    assert "help_docs" not in command2class
    assert "help_docs" not in pr_agent_module.commands


@pytest.mark.asyncio
async def test_help_docs_command_is_not_routed(monkeypatch):
    """An incoming /help_docs command resolves to an unknown command and is rejected."""
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: (True, ""))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)

    handled = await PRAgent()._handle_request(
        "https://github.com/owner/repo/pull/1",
        "/help_docs \"what docs exist\" --pr_help_docs.repo_url=https://github.com/x/y",
    )
    assert handled is False
