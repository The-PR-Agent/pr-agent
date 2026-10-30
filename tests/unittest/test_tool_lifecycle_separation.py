from types import SimpleNamespace

import pytest

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
def test_deferred_init_does_no_provider_io(module, provider_calls):
    tool = _make_tool(module, _defer_prepare=True)

    assert provider_calls == []
    assert tool._prepared is False
    assert not hasattr(tool, "git_provider")
    assert not hasattr(tool, "vars")
    assert not hasattr(tool, "token_handler")


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_prepare_initializes_context_once(module, provider_calls):
    tool = _make_tool(module, _defer_prepare=True)

    tool.prepare()
    git_provider, token_handler = tool.git_provider, tool.token_handler
    tool.prepare()

    assert provider_calls == [PR_URL]
    assert tool._prepared is True
    assert isinstance(git_provider, FakeGitProvider)
    assert tool.vars["title"] == "A pull request"
    assert tool.token_handler is token_handler
    assert token_handler.prompt_tokens > 0


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_default_init_prepares_eagerly(module, provider_calls):
    tool = _make_tool(module)

    assert provider_calls == [PR_URL]
    assert tool._prepared is True
    assert tool.vars["title"] == "A pull request"
    assert tool.token_handler.prompt_tokens > 0

    tool.prepare()
    assert provider_calls == [PR_URL]


@pytest.mark.parametrize("module", TOOL_MODULES)
async def test_run_prepares_a_deferred_tool(module, provider_calls, monkeypatch):
    tool = _make_tool(module, _defer_prepare=True)

    # Stop run() right after its prepare() step; the rest of run() is covered by the tools' own tests.
    def stop():
        raise RuntimeError("stop")

    monkeypatch.setattr(module, "init_run_details", stop)

    with pytest.raises(RuntimeError, match="stop"):
        await tool.run()

    assert provider_calls == [PR_URL]
    assert tool._prepared is True
