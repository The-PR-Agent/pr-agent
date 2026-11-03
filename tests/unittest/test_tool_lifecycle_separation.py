import threading
from types import SimpleNamespace

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.tools import pr_code_suggestions, pr_description, pr_reviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

PR_URL = "https://example/pr/1"
TOOL_MODULES = [pr_reviewer, pr_description, pr_code_suggestions]


class FakeGitProvider:
    pr = SimpleNamespace(title="A pull request")

    def get_languages(self):
        return {"Python": 1}

    def get_files(self):
        return ["example.py"]

    def get_diff_files(self):
        return []

    def get_pr_id(self):
        return "1"

    def get_pr_branch(self):
        return "feature"

    def get_commit_messages(self):
        return ""

    def get_pr_description(self, full=True, split_changes_walkthrough=False):
        if split_changes_walkthrough:
            return "Description", []
        return "Description"

    def get_user_description(self):
        return "Description"

    def get_num_of_files(self):
        return 1

    def get_issue_comments_newest_first(self):
        return []

    def supports_review_finding_state(self):
        return False

    def is_supported(self, capability):
        return True


@pytest.fixture
def provider_calls(monkeypatch):
    snapshot = snapshot_settings([
        "config.enable_ai_metadata",
        "pr_description.enable_semantic_files_types",
    ])
    calls = []

    def fake_get_git_provider_with_context(pr_url):
        calls.append(pr_url)
        return FakeGitProvider()

    for module in TOOL_MODULES:
        monkeypatch.setattr(module, "get_git_provider_with_context", fake_get_git_provider_with_context)
        monkeypatch.setattr(module, "get_skills_context", lambda: "")
        monkeypatch.setattr(module, "build_repo_context", lambda _provider: "")
    yield calls
    restore_settings(snapshot)


def _make_tool(module, **kwargs):
    ai_handler = lambda: SimpleNamespace()  # noqa: E731
    if module is pr_reviewer:
        return pr_reviewer.PRReviewer(PR_URL, ai_handler=ai_handler, **kwargs)
    if module is pr_description:
        return pr_description.PRDescription(PR_URL, ai_handler=ai_handler, **kwargs)
    return pr_code_suggestions.PRCodeSuggestions(PR_URL, ai_handler=ai_handler, **kwargs)


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_default_init_loads_pr_context(module, provider_calls):
    tool = _make_tool(module)

    assert provider_calls == [PR_URL]
    assert isinstance(tool.git_provider, FakeGitProvider)
    assert tool.vars["title"] == "A pull request"


@pytest.mark.parametrize("module, command", [
    (pr_reviewer, "/review"),
    (pr_reviewer, "/answer"),
    (pr_reviewer, "/auto_review"),
    (pr_description, "/describe"),
    (pr_code_suggestions, "/improve"),
])
async def test_dispatch_constructs_tools_off_the_event_loop(module, command, provider_calls, monkeypatch):
    loop_thread = threading.get_ident()
    provider_threads = []

    def get_provider(pr_url):
        provider_threads.append(threading.get_ident())
        return FakeGitProvider()

    async def run(tool):
        assert threading.get_ident() == loop_thread
        assert tool.vars["title"] == "A pull request"
        if module is pr_reviewer:
            assert tool.is_answer is (command == "/answer")
            assert tool.is_auto is (command == "/auto_review")

    tool_class = {
        pr_reviewer: pr_reviewer.PRReviewer,
        pr_description: pr_description.PRDescription,
        pr_code_suggestions: pr_code_suggestions.PRCodeSuggestions,
    }[module]
    monkeypatch.setattr(module, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(tool_class, "run", run)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)

    agent = pr_agent_module.PRAgent(ai_handler=lambda: SimpleNamespace())
    assert await agent.handle_request(PR_URL, command) is True
    assert provider_threads and loop_thread not in provider_threads
