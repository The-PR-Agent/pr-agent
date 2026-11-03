"""Manual tools reuse the provider prepared by the request dispatcher."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette_context import context, request_cycle_context

from pr_agent import git_providers
from pr_agent.agent.pr_agent import PRAgent, command2class
from pr_agent.agent.request_policy import RULE_FIELDS, RequestOutcome
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider


@pytest.mark.parametrize("action", ["ask", "ask_line", "add_docs", "update_changelog", "generate_labels"])
async def test_manual_tools_reuse_request_provider(monkeypatch, action):
    original_settings = get_settings()
    original_settings.get("config")  # Initialize Dynaconf before copying the request settings.
    settings = deepcopy(original_settings)
    settings.set("config.git_provider", "github")
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.publish_output", False)
    settings.set("config.propagate_tool_errors", True)
    settings.set("github.deployment_type", "user")
    settings.set("github.user_token", "stub-token")
    settings.set("pr_update_changelog.push_changelog_changes", False)
    settings.set("pr_questions.use_conversation_history", False)
    settings.set("skills.enabled", False)

    pr_url = "https://github.com/example/repo/pull/1"
    repo = MagicMock(full_name="example/repo", default_branch="main")
    repo.get_languages.return_value = {"Python": 1}
    pr = MagicMock(title="Example", body="Description", html_url=pr_url, changed_files=1)
    pr.head = SimpleNamespace(sha="head", ref="feature")
    pr.base = SimpleNamespace(sha="base", ref="main")
    pr.get_commits.return_value = [SimpleNamespace(sha="head", commit=SimpleNamespace(message="Change"))]
    pr.get_files.return_value = [SimpleNamespace(filename="app.py")]
    repo.get_pull.return_value = pr
    clients = []

    def github_client(**_kwargs):
        client = MagicMock()
        client.get_repo.return_value = repo
        clients.append(client)
        return client

    monkeypatch.setattr("pr_agent.git_providers.github_provider.Github", github_client)
    tool_providers = []

    async def capture_tool(tool):
        tool_providers.append(tool.git_provider)

    monkeypatch.setattr(command2class[action], "run", capture_tool)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(pr_url, [action, "Explain."])
        assert tool_providers == [context["git_provider"][pr_url]]

    assert len(clients) == 1


@pytest.fixture
def hosted_provider_with_repo_diff(monkeypatch):
    original_settings = get_settings()
    original_settings.get("config")
    settings = deepcopy(original_settings)
    settings.set("config.git_provider", "github")
    settings.set("config.use_repo_settings_file", True)
    settings.set("config.use_per_directory_settings_file", False)
    settings.set("config.publish_output", False)
    settings.set("config.propagate_tool_errors", True)
    settings.set("skills.enabled", False)
    settings.set("pr_questions.use_conversation_history", False)
    for rule in RULE_FIELDS.values():
        settings.set(f"config.{rule}", [])
    hosted = MagicMock()
    hosted.get_repo_settings.return_value = (
        b"[plain_diff]\ncontent = '''diff --git a/a.py b/a.py\n"
        b"--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n'''\n"
    )
    hosted.get_request_policy_metadata.return_value = {}
    monkeypatch.setitem(git_providers._GIT_PROVIDERS, "github", lambda _: hosted)
    return settings, hosted


async def test_repository_diff_content_uses_plain_diff_provider(hosted_provider_with_repo_diff, monkeypatch):
    settings, _ = hosted_provider_with_repo_diff
    seen = []

    async def capture(tool):
        seen.append(tool.git_provider)

    monkeypatch.setattr(command2class["ask"], "run", capture)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(
            "https://github.com/example/repo/pull/1", ["ask", "Why?"]
        )

    assert len(seen) == 1
    assert isinstance(seen[0], PlainDiffGitProvider)


async def test_extra_host_config_cannot_hide_stale_bootstrap_provider(
    hosted_provider_with_repo_diff, monkeypatch, tmp_path,
):
    settings, _ = hosted_provider_with_repo_diff
    settings.set("config.use_repo_settings_file", False)
    settings.set("github.api_retries", 0)
    extra = tmp_path / "host.toml"
    extra.write_text('[config]\nextra_config_url = ""\n[github]\napi_retries = 2\n')
    settings.set("config.extra_config_url", str(extra))
    created = []

    class HostedProvider:
        reuse_for_manual_tool = True

        def __init__(self, _url):
            self.pr = SimpleNamespace(title="Example")
            self.retries = get_settings().get("github.api_retries", 0)
            created.append(self)

        def get_request_policy_metadata(self, _fields):
            return {}

        def get_pr_branch(self):
            return "feature"

        def supports_thread_resolution(self):
            return False

        def supports_threaded_pr_questions(self):
            return False

    monkeypatch.setitem(git_providers._GIT_PROVIDERS, "github", HostedProvider)
    used = []

    async def capture(tool):
        used.append(tool.git_provider)

    monkeypatch.setattr(command2class["ask_line"], "run", capture)
    pr_url = "https://github.com/example/repo/pull/1"
    with request_cycle_context({"settings": settings}):
        bootstrap = git_providers.get_git_provider_with_context(pr_url)
        assert bootstrap.retries == 0
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(pr_url, ["ask_line", "Why?"])
        assert get_settings().get("config.extra_config_url") == ""
        assert used[0] is not bootstrap
        assert used[0].retries == 2

    assert len(created) == 2


async def test_repository_provider_change_uses_final_provider(hosted_provider_with_repo_diff, monkeypatch):
    settings, bootstrap = hosted_provider_with_repo_diff
    bootstrap.get_repo_settings.return_value = b'[config]\ngit_provider = "alternate-host"\n'
    selected = MagicMock()
    selected.get_languages.return_value = {"Python": 1}
    selected.get_files.return_value = ["app.py"]
    monkeypatch.setitem(git_providers._GIT_PROVIDERS, "alternate-host", lambda _: selected)
    seen = []

    async def capture(tool):
        seen.append(tool.git_provider)

    monkeypatch.setattr(command2class["ask"], "run", capture)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(
            "https://github.com/example/repo/pull/1", ["ask", "Why?"]
        )

    assert seen == [selected]


async def test_hosted_dry_run_is_not_overridden_by_repo_diff_setting(hosted_provider_with_repo_diff, monkeypatch):
    settings, _ = hosted_provider_with_repo_diff
    observed = []

    class CaptureTool:
        def __init__(self, *_args, **_kwargs):
            observed.append(get_settings().get("config.publish_output"))

        async def run(self):
            pass

    monkeypatch.setitem(command2class, "ask", CaptureTool)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(
            "https://github.com/example/repo/pull/1", ["ask", "Why?", "--config.publish_output=false"]
        )

    assert observed == [False]


async def test_hosted_ignore_policy_still_runs_before_tool_provider_switch(hosted_provider_with_repo_diff):
    settings, hosted = hosted_provider_with_repo_diff
    settings.set("config.ignore_repositories", ["^example/repo$"])
    hosted.get_request_policy_metadata.return_value = {"repo_full_name": "example/repo"}

    with request_cycle_context({"settings": settings}):
        result = await PRAgent(ai_handler=SimpleNamespace).handle_request(
            "https://github.com/example/repo/pull/1", ["ask", "Why?"]
        )

    assert result is RequestOutcome.SKIPPED


@pytest.mark.asyncio
async def test_github_head_refreshes_after_automatic_command_boundary(monkeypatch):
    settings = deepcopy(get_settings())
    settings.set("config.git_provider", "github")
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.publish_output", False)
    settings.set("github.deployment_type", "user")
    settings.set("github.user_token", "stub-token")
    settings.set("skills.enabled", False)
    settings.set("pr_questions.use_conversation_history", False)
    url = "https://github.com/example/repo/pull/1"
    repo = MagicMock(full_name="example/repo", default_branch="main")
    repo.get_languages.return_value = {"Python": 100}
    old_pr = MagicMock(title="PR", body="", changed_files=1)
    new_pr = MagicMock(title="PR", body="", changed_files=2)
    for pr, sha in ((old_pr, "old-head"), (new_pr, "new-head")):
        pr.head = SimpleNamespace(sha=sha, ref="feature")
        pr.base = SimpleNamespace(sha="base", ref="main")
        pr.get_commits.return_value = [SimpleNamespace(sha=sha, commit=SimpleNamespace(message="Update"))]
        pr.get_files.return_value = [SimpleNamespace(filename="app.py")]
    active_pr = {"value": old_pr}
    repo.get_pull.side_effect = lambda _number: active_pr["value"]

    def github_client(**_kwargs):
        client = MagicMock()
        client.get_repo.return_value = repo
        return client

    monkeypatch.setattr("pr_agent.git_providers.github_provider.Github", github_client)
    observed = []

    async def capture(tool):
        observed.append((tool.git_provider.pr.head.sha, tool.git_provider))

    monkeypatch.setattr(command2class["ask"], "run", capture)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(url, ["ask", "Why?"])
        context["git_provider"][url].reset_diff_cache_for_command()
        active_pr["value"] = new_pr
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(url, ["ask", "Why?"])

    assert [sha for sha, _provider in observed] == ["old-head", "new-head"]
    assert observed[0][1] is not observed[1][1]
    assert repo.get_pull.call_count >= 2


@pytest.mark.asyncio
async def test_gitea_push_between_commands_rebuilds_head_and_file_list(monkeypatch):
    settings = deepcopy(get_settings())
    settings.set("config.git_provider", "gitea")
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.publish_output", False)
    settings.set("gitea.personal_access_token", "stub-token")
    settings.set("skills.enabled", False)
    settings.set("pr_questions.use_conversation_history", False)
    url = "https://gitea.example/org/repo/pulls/1"
    repo_api = MagicMock()
    old_pr = MagicMock(title="PR", body="")
    new_pr = MagicMock(title="PR", body="")
    for pr, sha in ((old_pr, "old-head"), (new_pr, "new-head")):
        pr.head = SimpleNamespace(sha=sha, ref="feature")
        pr.base = SimpleNamespace(sha="base", ref="main")
    repo_api.get_pull_request.side_effect = [old_pr, old_pr, new_pr]
    repo_api.get_languages.return_value = {"Python": 100}
    repo_api.get_pr_commits.return_value = []
    repo_api.get_change_file_pull_request.side_effect = [
        [{"filename": "old.py"}], [{"filename": "old.py"}, {"filename": "new.py"}]
    ]
    monkeypatch.setattr("pr_agent.git_providers.gitea_provider.RepoApi", lambda _client: repo_api)
    observed = []

    async def capture(tool):
        observed.append((tool.git_provider.get_pr_head_sha(), tool.git_provider.get_files()))

    monkeypatch.setattr(command2class["ask"], "run", capture)
    with request_cycle_context({"settings": settings}):
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(url, ["ask", "Why?"])
        context["git_provider"][url].reset_diff_cache_for_command()
        assert await PRAgent(ai_handler=SimpleNamespace).handle_request(url, ["ask", "Why?"])

    assert observed == [("old-head", ["old.py"]), ("new-head", ["old.py", "new.py"])]
    assert repo_api.get_pull_request.call_count == 3
