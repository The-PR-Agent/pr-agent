import pytest

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers.github_app import handle_new_pr_opened
from pr_agent.tools.pr_add_docs import PRAddDocs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,draft,state,should_run",
    [
        ("opened", False, "open", True),
        ("edited", False, "open", False),
        ("opened", True, "open", False),
        ("opened", False, "closed", False),
    ],
)
async def test_add_docs_trigger(monkeypatch, action, draft, state, should_run):
    # Mock settings to enable the "/add_docs" auto-command on PR opened
    settings = get_settings()
    settings.github_app.pr_commands = ["/add_docs"]
    settings.github_app.handle_pr_actions = ["opened"]

    # Define a FakeGitProvider for both apply_repo_settings and PRAddDocs
    class FakeGitProvider:
        def __init__(self, pr_url, *args, **kwargs):
            self.pr = type("pr", (), {"title": "Test PR"})()
            self.get_pr_branch = lambda: "test-branch"
            self.get_pr_description = lambda: "desc"
            self.get_languages = lambda: ["Python"]
            self.get_files = lambda: []
            self.get_commit_messages = lambda: "msg"
            self.publish_comment = lambda *args, **kwargs: None
            self.remove_initial_comment = lambda: None
            self.publish_code_suggestions = lambda suggestions: True
            self.diff_files = []
            self.get_repo_settings = lambda: {}

    # Patch Git provider lookups
    monkeypatch.setattr(
        "pr_agent.git_providers.utils.get_git_provider_with_context",
        lambda pr_url: FakeGitProvider(pr_url),
    )
    monkeypatch.setattr(
        "pr_agent.tools.pr_add_docs.get_git_provider",
        lambda: FakeGitProvider,
    )

    # Ensure identity provider always eligible
    monkeypatch.setattr(
        get_identity_provider().__class__,
        "verify_eligibility",
        lambda *args, **kwargs: Eligibility.ELIGIBLE,
    )

    # Spy on PRAddDocs.run()
    ran = {"flag": False}

    async def fake_run(self):
        ran["flag"] = True

    monkeypatch.setattr(PRAddDocs, "run", fake_run)

    # Build minimal PR payload
    body = {
        "action": action,
        "pull_request": {
            "url": "https://example.com/fake/pr",
            "state": state,
            "draft": draft,
        },
    }
    log_context = {}

    # Invoke the PR-open handler
    agent = PRAgent()
    await handle_new_pr_opened(
        body=body,
        event="pull_request",
        sender="tester",
        sender_id="123",
        action=action,
        log_context=log_context,
        agent=agent,
    )

    assert ran["flag"] is should_run, (
        f"Expected run() to be {'called' if should_run else 'skipped'}"
        f" for action={action!r}, draft={draft}, state={state!r}"
    )
