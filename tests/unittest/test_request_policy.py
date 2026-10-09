"""Exercise the central guard through public entrypoints without provider/model I/O."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent import git_providers
from pr_agent.agent import pr_agent as agent
from pr_agent.agent.request_policy import RULE_FIELDS, RequestOutcome
from pr_agent.config_loader import global_settings

URL = "https://github.com/org/repo/pull/7"
METADATA = dict(title="Regular PR", sender="author", repo_full_name="org/repo", labels=["skip"],
                source_branch="feature/test", target_branch="main")


@pytest.fixture
def environment(monkeypatch):
    settings = copy.deepcopy(global_settings)
    for rule in RULE_FIELDS.values():
        settings.set(f"config.{rule}", [])
    provider = Mock()
    provider.get_request_policy_metadata.return_value = METADATA.copy()
    monkeypatch.setattr(git_providers, "get_git_provider_with_context", lambda *_a, **_kw: provider)
    monkeypatch.setattr(agent, "get_git_provider_with_context", lambda *_a, **_kw: provider)
    monkeypatch.setattr(agent, "apply_repo_settings", Mock())
    monkeypatch.setattr(agent, "flush_telemetry", Mock())
    with request_cycle_context({"settings": settings, "git_provider": {}}):
        yield settings, provider


@pytest.mark.parametrize(("rule", "value"), [
    ("ignore_repositories", "^org/repo$"), ("ignore_pr_authors", "^author$"),
    ("ignore_pr_title", "^Regular"), ("ignore_pr_labels", "skip"),
    ("ignore_pr_source_branches", "^feature/"), ("ignore_pr_target_branches", "^main$"),
])
async def test_ignored_request_returns_skip_before_tool_notify_and_followup(environment, monkeypatch, rule, value):
    settings, _ = environment
    settings.set(f"config.{rule}", [value])
    tool, notify, after, cleanup = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)

    async def transport():
        try:
            result = await agent.PRAgent().handle_request(URL, "/review", notify=notify)
            assert result is RequestOutcome.SKIPPED
            if result is RequestOutcome.SKIPPED:
                return
            after()
        finally:
            cleanup()

    await transport()
    tool.assert_not_called()
    notify.assert_not_called()
    after.assert_not_called()
    cleanup.assert_called_once()
    agent.flush_telemetry.assert_called_once()


@pytest.mark.parametrize("failure", [False, True])
async def test_allowed_commands_preserve_success_failure_and_notifications(environment, monkeypatch, failure):
    tool = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("tool failed") if failure else None))
    factory, notify = Mock(return_value=tool), Mock()
    monkeypatch.setitem(agent.command2class, "review", factory)
    result = await agent.PRAgent().handle_request(URL, ["review"], notify=notify)
    assert result is (not failure)
    notify.assert_called_once()
    tool.run.assert_awaited_once()
    agent.apply_repo_settings.assert_called_once_with(URL)


async def test_repo_settings_are_loaded_before_policy_and_argument_overrides(environment, monkeypatch):
    settings, _ = environment
    def load(_url):
        settings.set("config.ignore_pr_labels", ["skip"])
    monkeypatch.setattr(agent, "apply_repo_settings", load)
    tool = Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)
    result = await agent.PRAgent().handle_request(URL, ["review", "--config.ignore_pr_labels=[]"])
    assert result is RequestOutcome.SKIPPED
    tool.assert_not_called()


@pytest.mark.parametrize("missing_form", ["omitted", "none"])
@pytest.mark.parametrize("title_matches", [False, True])
async def test_unavailable_author_skips_only_author_rule(environment, monkeypatch, missing_form, title_matches):
    settings, provider = environment
    settings.set("config.ignore_pr_authors", ["author"])
    settings.set("config.ignore_pr_title", ["^Regular" if title_matches else "^Unmatched$"])
    metadata = METADATA.copy()
    if missing_form == "omitted":
        metadata.pop("sender")
    else:
        metadata["sender"] = None
    provider.get_request_policy_metadata.return_value = metadata
    tool = SimpleNamespace(run=AsyncMock())
    factory, notify = Mock(return_value=tool), Mock()
    monkeypatch.setitem(agent.command2class, "review", factory)
    result = await agent.PRAgent().handle_request(URL, "/review", notify=notify)
    if title_matches:
        assert result is RequestOutcome.SKIPPED
        tool.run.assert_not_awaited()
        notify.assert_not_called()
    else:
        assert result is True
        tool.run.assert_awaited_once()
        notify.assert_called_once()


@pytest.mark.parametrize("failure_at", ["provider", "metadata"])
async def test_policy_lookup_error_allows_command(environment, monkeypatch, failure_at):
    settings, provider = environment
    settings.set("config.ignore_pr_title", ["Regular"])
    if failure_at == "provider":
        monkeypatch.setattr(git_providers, "get_git_provider_with_context", Mock(side_effect=RuntimeError("outage")))
    else:
        provider.get_request_policy_metadata.side_effect = RuntimeError("outage")
    tool = SimpleNamespace(run=AsyncMock())
    notify = Mock()
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=tool))
    assert await agent.PRAgent().handle_request(URL, "/review", notify=notify) is True
    tool.run.assert_awaited_once()
    notify.assert_called_once()
    agent.flush_telemetry.assert_called_once()


async def test_provider_without_metadata_uses_empty_default(environment, monkeypatch):
    from pr_agent.git_providers.git_provider import GitProvider
    settings, provider = environment
    settings.set("config.ignore_pr_title", ["Regular"])
    provider.get_request_policy_metadata.side_effect = (
        lambda fields: GitProvider.get_request_policy_metadata(None, fields))
    tool = SimpleNamespace(run=AsyncMock())
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=tool))
    assert await agent.PRAgent().handle_request(URL, "/review") is True
    tool.run.assert_awaited_once()


def test_every_builtin_and_mosaico_provider_implements_policy_contract():
    from pr_agent.git_providers.git_provider import GitProvider
    from pr_agent.mosaico.diff_provider import DiffInputProvider
    classes = [git_providers._GIT_PROVIDERS[name] for name in git_providers._BUILTIN_GIT_PROVIDERS]
    for cls in [*classes, DiffInputProvider]:
        assert cls.get_request_policy_metadata is not GitProvider.get_request_policy_metadata, cls.__name__


async def test_github_comment_skip_has_no_outcome_reaction(environment, monkeypatch):
    from pr_agent.identity_providers.identity_provider import Eligibility
    from pr_agent.servers import github_app
    settings, provider = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_: provider)
    identity = Mock()
    identity.verify_eligibility.return_value = Eligibility.ELIGIBLE
    monkeypatch.setattr(github_app, "get_identity_provider", lambda: identity)
    body = {"action": "created", "sender": {"login": "human", "id": 1, "type": "User"},
            "issue": {"pull_request": {"url": URL}}, "comment": {"body": "/review", "id": 42}}
    await github_app.handle_request(body, "issue_comment")
    provider.add_eyes_reaction.assert_not_called()
    provider.react_to_outcome.assert_not_called()


async def test_azure_skip_does_not_close_thread_or_delete_comment(environment, monkeypatch):
    from pr_agent.servers import azuredevops_server_webhook as azure
    settings, provider = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    monkeypatch.setattr(azure, "get_git_provider_with_context", lambda **_: provider)
    monkeypatch.setattr(azure, "handle_line_comment", lambda body, *_: body)
    await azure.handle_request_comment(URL, "/review", 1, 2, {})
    provider.reply_to_thread.assert_not_called()
    provider.set_thread_status.assert_not_called()
    provider.remove_initial_comment.assert_not_called()


@pytest.mark.parametrize("verb", ["review", "ask"])
async def test_mosaico_skip_returns_explicit_skip_without_stale_output(environment, monkeypatch, verb):
    from pr_agent.mosaico import dispatch
    settings, _ = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    settings.set("data.artifact", "old output")
    tool = Mock()
    monkeypatch.setitem(agent.command2class, verb, tool)
    result = await (dispatch._run_ask(URL, "why?") if verb == "ask" else dispatch._run_pr_agent(URL, verb))
    assert result.ok
    assert result.text == "Request ignored by policy."
    tool.assert_not_called()


def test_cli_skip_does_not_print_usage_or_exit_as_failure(environment, monkeypatch, capsys):
    from pr_agent import cli
    settings, _ = environment
    # CLI intentionally snapshots global settings into its own request scope.
    monkeypatch.setattr(cli, "_cli_settings_scope", lambda: request_cycle_context({"settings": settings}))
    settings.set("config.ignore_pr_labels", ["skip"])
    settings.set("config.propagate_tool_errors", True)
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    tool = Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)
    assert cli.run(inargs=[f"--pr_url={URL}", "review"]) is None
    assert "usage:" not in capsys.readouterr().out
    tool.assert_not_called()


@pytest.mark.parametrize("provider_id", ["github", "gitea", "gitlab", "bitbucket", "bitbucket_server", "azure"])
def test_real_provider_metadata_uses_pr_author_and_branches(provider_id):
    cls = git_providers._GIT_PROVIDERS[provider_id]
    provider = cls.__new__(cls)
    provider.get_pr_labels = Mock(return_value=["skip"])
    if provider_id in ("github", "gitea"):
        provider.repo = "repo" if provider_id == "gitea" else "org/repo"
        provider.owner = "org"
        provider.pr = SimpleNamespace(title="Regular PR", user=SimpleNamespace(login="author"),
                                      head=SimpleNamespace(ref="feature/test"), base=SimpleNamespace(ref="main"))
    elif provider_id == "gitlab":
        provider.id_project = "org/repo"
        provider.mr = SimpleNamespace(title="Regular PR", author={"username": "author"},
                                      source_branch="feature/test", target_branch="main")
    elif provider_id == "bitbucket":
        provider.pr = SimpleNamespace(data={"title": "Regular PR", "author": {"nickname": "author"},
            "source": {"branch": {"name": "feature/test"}},
            "destination": {"branch": {"name": "main"}, "repository": {"full_name": "org/repo"}}})
    elif provider_id == "bitbucket_server":
        provider.workspace_slug, provider.repo_slug = "org", "repo"
        provider.pr = SimpleNamespace(title="Regular PR", author={"user": {"name": "author"}},
                                     fromRef={"displayId": "feature/test"}, toRef={"displayId": "main"})
    else:
        provider.workspace_slug, provider.repo_slug = "org", "repo"
        provider.pr = SimpleNamespace(title="Regular PR", created_by=SimpleNamespace(unique_name="author"),
                                      source_ref_name="refs/heads/feature/test", target_ref_name="refs/heads/main")
    fields = provider.get_request_policy_metadata(set(RULE_FIELDS))
    assert {key: value for key, value in fields.items() if key != "labels"} == {
        key: value for key, value in METADATA.items() if key != "labels"}
    provider.get_pr_labels.reset_mock()
    provider.get_request_policy_metadata({"title"})
    provider.get_pr_labels.assert_not_called()


@pytest.mark.parametrize("source", [URL, "https://gitlab.com/org/repo/-/merge_requests/7"])
async def test_mosaico_public_diff_retains_repository_filtering(monkeypatch, source):
    from pr_agent.mosaico import dispatch, provider_registration  # noqa: F401
    settings = copy.deepcopy(global_settings)
    settings.set("config.ignore_repositories", ["^org/repo$"])
    settings.set("config.use_repo_settings_file", False)
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setattr(dispatch, "_fetch_public_diff", AsyncMock(return_value=patch))
    monkeypatch.setattr(agent, "flush_telemetry", Mock())
    tool = Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)
    with request_cycle_context({"settings": settings, "git_provider": {}}):
        result = await dispatch.route_and_run_result(f"review {source}")
    assert result.text == "Request ignored by policy."
    tool.assert_not_called()


async def test_github_auto_skip_opens_no_check_and_stops_batch(environment, monkeypatch):
    from pr_agent.servers import github_app
    settings, provider = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    settings.set("github_app.pr_commands", ["/review", "/describe"])
    settings.set("github.publish_as_check_run", True)
    monkeypatch.setattr(github_app, "get_git_provider_with_context", lambda **_: provider)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _: True)
    result = await github_app._perform_auto_commands_github(
        "pr_commands", agent.PRAgent(), {"pull_request": {"draft": False}}, URL, {})
    assert result is RequestOutcome.SKIPPED
    provider.start_check_run.assert_not_called()
    provider.finish_check_run.assert_not_called()
    provider.get_request_policy_metadata.assert_called_once()


async def test_skip_is_not_recorded_as_a_failed_command_span(environment, monkeypatch):
    from opentelemetry.trace import StatusCode

    from tests.unittest._telemetry_helpers import build_in_memory_tracer
    settings, _ = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    settings.set("OTEL.INCLUDE_ERROR_DETAILS", True)
    tracer, exporter = build_in_memory_tracer()
    monkeypatch.setattr(agent, "get_tracer", lambda: tracer)
    assert await agent.PRAgent().handle_request(URL, "/review") is RequestOutcome.SKIPPED
    span, = exporter.get_finished_spans()
    assert span.attributes["pr_agent.request.ignored"] is True
    assert span.status.status_code != StatusCode.ERROR
    assert not span.events


async def test_gitea_comment_uses_central_policy(environment, monkeypatch):
    from pr_agent.servers import gitea_app
    settings, _ = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    tool = Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)
    body = {"action": "created", "pull_request": {"url": URL}, "comment": {"body": "/review"}}
    await gitea_app.handle_request(body, "issue_comment")
    tool.assert_not_called()


async def test_action_skip_does_not_mark_job_failed(environment, monkeypatch):
    from pr_agent.servers import github_action_runner as action
    settings, _ = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    failed = Mock()
    monkeypatch.setattr(action, "_mark_action_failed", failed)
    notify = Mock()
    await action._handle_request(URL, "/review", notify=notify)
    failed.assert_not_called()
    notify.assert_not_called()


async def test_mosaico_question_stays_literal_and_cannot_enable_publishing(environment, monkeypatch):
    from pr_agent.algo.utils import decode_user_text_args
    from pr_agent.config_loader import get_settings
    from pr_agent.mosaico import dispatch
    observed = []
    class Question:
        def __init__(self, _url, ai_handler=None, args=None):
            observed.append(decode_user_text_args(args))
        async def run(self):
            assert get_settings().config.publish_output is False
            get_settings().set("data.answer", "answer")
    monkeypatch.setitem(agent.command2class, "ask", Question)
    question = '--config.publish_output=true why does #123 use "quotes"?'
    result = await dispatch._run_ask(URL, question)
    assert result.text == "answer"
    assert observed == [question]


def test_skipped_result_is_distinct_from_success_and_failure():
    assert RequestOutcome.SKIPPED is not True
    assert RequestOutcome.SKIPPED is not False
    assert not RequestOutcome.SKIPPED


async def test_invalid_policy_preserves_shared_matcher_error_fallback(environment, monkeypatch):
    settings, _ = environment
    settings.set("config.ignore_pr_title", ["["])
    tool, notify = SimpleNamespace(run=AsyncMock()), Mock()
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=tool))
    assert await agent.PRAgent().handle_request(URL, "/review", notify=notify) is True
    tool.run.assert_awaited_once()
    notify.assert_called_once()
    agent.flush_telemetry.assert_called_once()


@pytest.mark.parametrize("provider_name", ["gitea", "gitlab", "azure", "bitbucket", "bitbucket_server"])
async def test_automatic_batches_stop_on_skip(environment, monkeypatch, provider_name):
    from pr_agent.servers import (
        azuredevops_server_webhook,
        bitbucket_app,
        bitbucket_server_webhook,
        gitea_app,
        gitlab_webhook,
    )
    settings, _ = environment
    settings.set("config.disable_auto_feedback", False)
    settings.set("gitea.pr_commands", ["/review", "/describe"])
    settings.set("gitlab.pr_commands", ["/review", "/describe"])
    settings.set("azure_devops_server.pr_commands", ["/review", "/describe"])
    fake_agent = SimpleNamespace(handle_request=AsyncMock(return_value=RequestOutcome.SKIPPED))
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda _: True)
    monkeypatch.setattr(azuredevops_server_webhook, "apply_repo_settings", lambda _: None)
    monkeypatch.setattr(bitbucket_server_webhook, "PRAgent", lambda: fake_agent)
    monkeypatch.setattr(bitbucket_server_webhook, "_process_command", lambda command, _: command)
    if provider_name == "gitea":
        result = await gitea_app._perform_commands_gitea("pr_commands", fake_agent, {}, URL)
    elif provider_name == "gitlab":
        result = await gitlab_webhook._perform_commands_gitlab("pr_commands", fake_agent, URL, {}, {})
    elif provider_name == "azure":
        result = await azuredevops_server_webhook._perform_commands_azure("pr_commands", fake_agent, URL, {})
    elif provider_name == "bitbucket":
        result = await bitbucket_app._run_commands_bitbucket(["/review", "/describe"], fake_agent, URL, {})
    else:
        result = await bitbucket_server_webhook._run_commands_sequentially(["/review", "/describe"], URL, {})
    assert result is RequestOutcome.SKIPPED
    fake_agent.handle_request.assert_awaited_once()


async def test_action_configured_command_preserves_skip(environment, monkeypatch):
    from pr_agent.servers import github_action_runner as action
    fake = AsyncMock(return_value=RequestOutcome.SKIPPED)
    monkeypatch.setattr(action, "_handle_request", fake)
    assert await action._handle_configured_command(URL, "/review") is RequestOutcome.SKIPPED
    fake.assert_awaited_once()


async def test_gitlab_comment_skip_has_no_followup_reaction(environment, monkeypatch):
    from starlette.background import BackgroundTasks

    from pr_agent.servers import gitlab_webhook
    settings, provider = environment
    settings.set("config.ignore_pr_labels", ["skip"])
    monkeypatch.setattr(gitlab_webhook, "global_settings", settings)
    monkeypatch.setattr(gitlab_webhook, "authenticate_gitlab_webhook", lambda *_: None)
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda **_: provider)
    body = {"object_kind": "note", "event_type": "note", "user": {"username": "human", "name": "Human", "id": 1},
            "merge_request": {"url": URL}, "object_attributes": {"id": 42, "note": "/review"}}
    background = BackgroundTasks()
    await gitlab_webhook.gitlab_webhook(background, SimpleNamespace(json=AsyncMock(return_value=body)))
    await background()
    provider.add_eyes_reaction.assert_not_called()
    provider.react_to_outcome.assert_not_called()


@pytest.mark.parametrize("input_mode", ["file", "stdin"])
def test_plain_diff_cli_does_not_filter_synthetic_title(monkeypatch, tmp_path, input_mode):
    import io

    from pr_agent import cli
    settings = copy.deepcopy(global_settings)
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.ignore_pr_title", [".*"])
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setattr(cli, "_cli_settings_scope", lambda: request_cycle_context({"settings": settings}))
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    monkeypatch.setattr(agent, "flush_telemetry", Mock())
    tool = SimpleNamespace(run=AsyncMock())
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=tool))
    if input_mode == "file":
        path = tmp_path / "change.diff"
        path.write_text(patch)
        arguments = ["--diff-file", str(path), "review"]
    else:
        monkeypatch.setattr("sys.stdin", io.StringIO(patch))
        arguments = ["--stdin", "review"]
    cli.run(inargs=arguments)
    tool.run.assert_awaited_once()


async def test_gitlab_numeric_project_policy_matches_namespace(environment, monkeypatch):
    settings, policy_provider = environment
    settings.set("config.ignore_repositories", ["^org/subgroup/repo$"])
    cls = git_providers._GIT_PROVIDERS["gitlab"]
    provider = cls.__new__(cls)
    provider.id_project = "1234"
    provider.gl = SimpleNamespace(projects=SimpleNamespace(get=Mock(
        return_value=SimpleNamespace(path_with_namespace="org/subgroup/repo"))))
    provider.mr = SimpleNamespace(title="Regular PR", author={"username": "author"},
                                  source_branch="feature", target_branch="main")
    policy_provider.get_request_policy_metadata.side_effect = provider.get_request_policy_metadata
    tool = Mock()
    monkeypatch.setitem(agent.command2class, "review", tool)
    result = await agent.PRAgent().handle_request("https://gitlab.com/projects/1234/-/merge_requests/7", "/review")
    assert result is RequestOutcome.SKIPPED
    tool.assert_not_called()
    provider.gl.projects.get.assert_called_once_with("1234")


@pytest.mark.parametrize("source", [None, URL])
async def test_mosaico_display_title_is_not_pr_policy_title(monkeypatch, source):
    from pr_agent.mosaico import dispatch, provider_registration  # noqa: F401
    settings = copy.deepcopy(global_settings)
    settings.set("config.use_repo_settings_file", False)
    settings.set("config.ignore_pr_title", ["^Supplied diff$", "github.com"])
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    monkeypatch.setattr(dispatch, "_fetch_public_diff", AsyncMock(return_value=patch))
    monkeypatch.setattr(agent, "flush_telemetry", Mock())
    tool = SimpleNamespace(run=AsyncMock())
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=tool))
    with request_cycle_context({"settings": settings, "git_provider": {}}):
        result = await dispatch.route_and_run_result(f"review {source}" if source else f"review\n{patch}")
    assert result.ok
    assert result.text != "Request ignored by policy."
    tool.run.assert_awaited_once()
