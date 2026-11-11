import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette_context import request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
import pr_agent.git_providers.gitea_provider as gitea_module
import pr_agent.git_providers.gitlab_provider as gitlab_module
from pr_agent.algo import artifacts
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as provider_utils
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompleteProviderPullRequestFilesError,
    IncompletePullRequestFilesError,
)


def _identity_args(args):
    return args


@pytest.mark.parametrize(
    ("error_cls", "expected_marker"),
    [
        (IncompletePullRequestFilesError, "<!-- pr-agent:github-incomplete-files -->"),
        (IncompleteBitbucketPullRequestFilesError, "<!-- pr-agent:bitbucket-incomplete-files -->"),
        (gitea_module.IncompleteGiteaPullRequestFilesError, "<!-- pr-agent:gitea-incomplete-files -->"),
        (gitlab_module.IncompleteGitLabDiffError, "<!-- pr-agent:gitlab-incomplete-diff -->"),
        (gitlab_module.GitLabDiffNotReadyError, "<!-- pr-agent:gitlab-incomplete-diff -->"),
    ],
)
def test_incomplete_file_notices_use_configured_bot_display_name(monkeypatch, error_cls, expected_marker):
    monkeypatch.setitem(get_settings().config, "bot_display_name", "Acme Bot")

    error = error_cls("internal detail")

    assert "## Acme Bot command was not run" in error.notice
    assert "Acme Bot stopped" in error.notice
    assert "PR-Agent" not in error.notice
    assert error.notice_marker == expected_marker


def test_incomplete_file_error_subclass_without_template_keeps_class_notice():
    class LegacyIncompleteFilesError(IncompleteProviderPullRequestFilesError):
        notice = "Legacy incomplete-files notice"

    error = LegacyIncompleteFilesError("incomplete")

    assert error.notice == "Legacy incomplete-files notice"


def test_incomplete_file_base_error_without_template_preserves_exception_type():
    error = IncompleteProviderPullRequestFilesError("incomplete")

    assert isinstance(error, RuntimeError)
    assert str(error) == "incomplete"


def test_incomplete_file_errors_share_only_provider_neutral_base():
    assert issubclass(IncompletePullRequestFilesError, IncompleteProviderPullRequestFilesError)
    assert issubclass(IncompleteBitbucketPullRequestFilesError, IncompleteProviderPullRequestFilesError)
    assert not issubclass(IncompleteBitbucketPullRequestFilesError, IncompletePullRequestFilesError)
    assert issubclass(gitea_module.IncompleteGiteaPullRequestFilesError, IncompleteProviderPullRequestFilesError)
    assert not issubclass(gitea_module.IncompleteGiteaPullRequestFilesError, IncompletePullRequestFilesError)
    assert issubclass(gitlab_module.IncompleteGitLabDiffError, IncompleteProviderPullRequestFilesError)
    assert issubclass(gitlab_module.IncompleteGitLabDiffError, gitlab_module.DiffNotFoundError)
    assert not issubclass(gitlab_module.IncompleteGitLabDiffError, IncompletePullRequestFilesError)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["published", "inspection_failure", "publication_failure", "disabled"])
async def test_gitea_review_rejects_incomplete_real_provider_inventory(monkeypatch, outcome):
    """Verify a failed SDK files page aborts the real /review route before model use."""
    from giteapy.rest import ApiException

    import pr_agent.commands.pr_reviewer as reviewer_module

    settings = Mock()
    settings.get.side_effect = lambda key, default=None: {
        "GITEA.URL": "https://gitea.example.com",
        "GITEA.PERSONAL_ACCESS_TOKEN": "test-token",
    }.get(key, default)
    monkeypatch.setattr(gitea_module, "get_settings", lambda: settings)
    transport = Mock()
    call_api = transport.call_api
    call_api.side_effect = ApiException(status=502, reason="private/repo secret failure")
    monkeypatch.setattr(gitea_module.giteapy, "ApiClient", lambda _config: transport)
    monkeypatch.setattr(
        gitea_module.RepoApi, "get_pull_request", lambda *_args, **_kwargs: SimpleNamespace(
            head=SimpleNamespace(sha="head"), base=SimpleNamespace(sha="base", ref="main"))
    )
    monkeypatch.setattr(gitea_module.RepoApi, "get_pull_request_diff", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(gitea_module.RepoApi, "get_pr_commits", lambda *_args, **_kwargs: [{"sha": "head"}])
    monkeypatch.setattr(gitea_module.RepoApi, "get_languages", lambda *_args, **_kwargs: {})

    def comments(_provider):
        if outcome == "inspection_failure":
            raise RuntimeError("private comment lookup")
        return []

    monkeypatch.setattr(gitea_module.GiteaProvider, "get_issue_comments", comments)
    published = []

    def publish(_provider, body):
        published.append(body)
        if outcome == "publication_failure":
            raise RuntimeError("private publication error")

    monkeypatch.setattr(gitea_module.GiteaProvider, "publish_comment", publish)
    providers = []

    def provider_factory(_url):
        provider = gitea_module.GiteaProvider("https://gitea.example.com/owner/repo/pulls/1")
        providers.append(provider)
        return provider

    monkeypatch.setattr(reviewer_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", outcome != "disabled")
    model_factory = Mock()

    handled = await pr_agent_module.PRAgent(ai_handler=model_factory)._handle_request(
        "https://gitea.example.com/owner/repo/pulls/1", "/review"
    )

    assert handled is False
    model_factory.assert_not_called()
    assert len(providers) == (1 if outcome == "disabled" else 2)
    assert call_api.call_count == 1
    if outcome == "disabled":
        assert published == []
    else:
        assert len(published) == 1
        assert "Gitea returned incomplete or unavailable" in published[0]
        assert gitea_module.IncompleteGiteaPullRequestFilesError.notice_marker in published[0]
        assert "private/repo" not in published[0]
        assert "test-token" not in published[0]


@pytest.fixture(autouse=True)
def reset_response_language():
    settings = get_settings()
    original_response_language = settings.config.response_language
    settings.config.response_language = "en-us"
    try:
        yield
    finally:
        settings.config.response_language = original_response_language


def _patch_request_dependencies(monkeypatch, validate_result=(True, None), update_settings_fn=None):
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    if update_settings_fn is None:
        update_settings_fn = _identity_args

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: validate_result)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings_fn)


def _incomplete_files_provider(comments=()):
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = list(comments)
    provider._get_comment_body.side_effect = lambda comment: comment.get("body", "")
    provider.is_comment_authored_by_pr_agent.return_value = False
    provider.supports_html_comment_markers.return_value = True
    return provider


@pytest.mark.asyncio
async def test_handle_request_routes_known_command_and_notifies(monkeypatch):
    runs = []
    notify = Mock()

    class FakeCommand:
        def __init__(self, pr_url, ai_handler, args):
            self.pr_url = pr_url
            self.ai_handler = ai_handler
            self.args = args

        async def run(self):
            runs.append((self.pr_url, self.ai_handler, self.args))

    _patch_request_dependencies(monkeypatch, update_settings_fn=lambda args: ["--kept"])
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeCommand)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/custom --flag", notify
    )

    assert handled is True
    notify.assert_called_once_with()
    assert runs == [("https://example/pr/1", "fake-ai", ["--kept"])]


@pytest.mark.asyncio
async def test_handle_request_routes_list_request_without_string_parsing(monkeypatch):
    runs = []

    class FakeCommand:
        def __init__(self, pr_url, ai_handler, args):
            self.pr_url = pr_url
            self.ai_handler = ai_handler
            self.args = args

        async def run(self):
            runs.append((self.pr_url, self.ai_handler, self.args))

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeCommand)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1",
        ["/custom", "don't split", "--flag=value"],
    )

    assert handled is True
    assert runs == [("https://example/pr/1", "fake-ai", ["don't split", "--flag=value"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("action, command_result", [("custom", False), ("add_docs", None), ("add_docs", 0)])
async def test_legacy_command_results_keep_success_acknowledgement(monkeypatch, action, command_result):
    class FakeCommand:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            return command_result

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(pr_agent_module.command2class, action, FakeCommand)

    assert await pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
        "https://example/pr/1", f"/{action}"
    ) is True


@pytest.mark.asyncio
async def test_prepared_override_wins_after_repo_settings_and_next_command_reloads_defaults(monkeypatch):
    observed = []
    provider = SimpleNamespace(
        get_repo_settings=lambda: b'[pr_reviewer]\nextra_instructions = "repository default"\n'
    )

    class FakeReview:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            observed.append(get_settings().get("PR_REVIEWER.EXTRA_INSTRUCTIONS"))

    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FakeReview)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        get_settings().set("CONFIG.USE_REPO_SETTINGS_FILE", True)
        get_settings().set("CONFIG.ENABLE_PER_DIRECTORY_SETTINGS", False)
        get_settings().set("CONFIG.EXTRA_CONFIG_URL", None)
        get_settings().set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "initial")
        worker = pr_agent_module.PRAgent(ai_handler="fake-ai")
        for command in ['/review --pr_reviewer.extra_instructions="command value"', "/review"]:
            assert await worker.handle_request(
                "https://example.com/org/repo/pull/1", pr_agent_module.prepare_command(command)
            ) is True

    assert observed == ["command value", "repository default"]


@pytest.mark.parametrize("setting", ["USE_REPO_SETTINGS_FILE", "ENABLE_PER_DIRECTORY_SETTINGS"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_prepared_overrides_control_repository_loading(monkeypatch, setting, enabled):
    observed = []
    provider = SimpleNamespace(
        get_repo_settings=Mock(return_value=b'[pr_reviewer]\nrequire_score_review = true\n'
                                           b'extra_instructions = "root"\n'),
        get_repo_settings_tree=Mock(return_value=(["src/.pr_agent.toml"], "main")),
        get_pr_file_paths=lambda: ["src/app.py"],
        get_repo_settings_contents=Mock(return_value={
            "src/.pr_agent.toml": b'[pr_reviewer]\nrequire_tests_review = false\n'
                                 b'extra_instructions = "nested"\n',
        }),
    )

    class FakeReview:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            settings = get_settings()
            observed.append((settings.pr_reviewer.require_score_review,
                             settings.pr_reviewer.require_tests_review,
                             settings.pr_reviewer.extra_instructions))

    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(provider_utils, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FakeReview)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.EXTRA_CONFIG_URL", None)
        settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)
        settings.set("CONFIG.ENABLE_PER_DIRECTORY_SETTINGS", False)
        settings.set(f"CONFIG.{setting}", not enabled)
        settings.set("PR_REVIEWER.REQUIRE_SCORE_REVIEW", False)
        settings.set("PR_REVIEWER.REQUIRE_TESTS_REVIEW", True)
        command = pr_agent_module.prepare_command(
            f'/review --config.{setting.lower()}={str(enabled).lower()} '
            '--pr_reviewer.extra_instructions="command value"'
        )
        assert await pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
            "https://example.com/org/repo/pull/1", command
        ) is True

    root_enabled = enabled if setting == "USE_REPO_SETTINGS_FILE" else True
    nested_enabled = enabled if setting == "ENABLE_PER_DIRECTORY_SETTINGS" else False
    assert observed == [(root_enabled, not nested_enabled, "command value")]
    assert provider.get_repo_settings.call_count == int(root_enabled)
    assert provider.get_repo_settings_contents.call_count == int(nested_enabled)


def test_prepare_command_preserves_spaces_in_quoted_config_values():
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            '/review --pr_reviewer.extra_instructions="Focus on authentication and authorization"'
        )

        assert command[0] == "/review"
        assert len(command) == 2
        assert settings.get(setting_key) == "Focus on authentication and authorization"
    finally:
        settings.set(setting_key, original)


def test_prepare_command_preserves_quoted_non_setting_arguments():
    command = pr_agent_module.prepare_command('/ask "why is this change risky?"')

    assert command == ["/ask", "why is this change risky?"]


@pytest.mark.parametrize(
    ("quoted_value", "expected_value"),
    [
        ('"Focus on # authentication"', "Focus on # authentication"),
        ('"true"', "true"),
        ('"null"', "null"),
        ("'Focus on # authentication'", "Focus on # authentication"),
    ],
)
def test_prepare_command_preserves_quoted_yaml_sensitive_values(quoted_value, expected_value):
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            f"/review --pr_reviewer.extra_instructions={quoted_value}"
        )

        assert command[0] == "/review"
        assert len(command) == 2
        assert settings.get(setting_key) == expected_value
    finally:
        settings.set(setting_key, original)


def test_prepare_command_accepts_apostrophes_in_unquoted_arguments_and_values():
    settings = get_settings()
    setting_key = "PR_REVIEWER.EXTRA_INSTRUCTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            "/review --pr_reviewer.extra_instructions=O'Reilly"
        )

        assert command[0] == "/review"
        assert len(command) == 2
        assert settings.get(setting_key) == "O'Reilly"
        assert pr_agent_module.prepare_command("/ask What's wrong?") == [
            "/ask",
            "What's",
            "wrong?",
        ]
    finally:
        settings.set(setting_key, original)


def test_prepare_command_keeps_unquoted_value_type_when_key_is_quoted():
    settings = get_settings()
    setting_key = "PR_CODE_SUGGESTIONS.NUM_CODE_SUGGESTIONS"
    original = settings.get(setting_key)

    try:
        command = pr_agent_module.prepare_command(
            '/review --"pr_code_suggestions.num_code_suggestions"=3'
        )

        assert command[0] == "/review"
        assert len(command) == 2
        assert settings.get(setting_key) == 3
        assert isinstance(settings.get(setting_key), int)
    finally:
        settings.set(setting_key, original)


@pytest.mark.asyncio
async def test_handle_request_rejects_forbidden_cli_args(monkeypatch):
    class FakeCommand:
        async def run(self):
            raise AssertionError("command should not run")

    _patch_request_dependencies(monkeypatch, validate_result=(False, "secret"))
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeCommand)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/custom --secret=value")

    assert handled is False


@pytest.mark.asyncio
async def test_incomplete_github_files_constructor_error_publishes_sanitized_notice(monkeypatch):
    secret = "private/repo mismatch: expected 5001 files but fetched 3000"
    provider = _incomplete_files_provider()

    class IncompleteCommand:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError(secret)

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", IncompleteCommand)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider.publish_comment.assert_called_once()
    published = provider.publish_comment.call_args.args[0]
    assert "GitHub returned an incomplete or inconsistent changed-file set" in published
    assert "If this pull request changes more than 3,000 files" in published
    assert "Otherwise, retry the command" in published
    assert "command was not run" in published
    assert secret not in published
    assert pr_agent_module.IncompletePullRequestFilesError.notice_marker in published.splitlines()[:5]


@pytest.mark.asyncio
async def test_incomplete_gitlab_diff_constructor_error_publishes_sanitized_notice(monkeypatch):
    secret = "private/repository diff mismatch with request details"
    provider = _incomplete_files_provider()

    class IncompleteCommand:
        def __init__(self, pr_url, ai_handler, args):
            raise gitlab_module.IncompleteGitLabDiffError(secret)

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", IncompleteCommand)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider.publish_comment.assert_called_once()
    published = provider.publish_comment.call_args.args[0]
    assert "GitLab returned an incomplete or inconsistent merge-request diff" in published
    assert "command was not run" in published
    assert gitlab_module.IncompleteGitLabDiffError.notice_marker in published.splitlines()[:5]
    assert secret not in published


def test_incomplete_gitlab_diff_notice_deduplicates_trusted_agent_comment(monkeypatch):
    marker = gitlab_module.IncompleteGitLabDiffError.notice_marker
    existing = {"body": f"## Existing notice\n\n{marker}\n\nDetails"}
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.return_value = True
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", gitlab_module.IncompleteGitLabDiffError("incomplete")
    )

    provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["published", "disabled", "provider_failure", "publication_failure"])
async def test_incomplete_bitbucket_constructor_error_keeps_provider_specific_failure(monkeypatch, outcome):
    secret = "private/repository patch mismatch with secret request details"
    provider = _incomplete_files_provider()
    provider.supports_html_comment_markers.return_value = False
    provider_factory = Mock(return_value=provider)
    if outcome == "provider_failure":
        provider_factory.side_effect = RuntimeError("secondary provider failure")
    elif outcome == "publication_failure":
        provider.publish_comment.side_effect = RuntimeError("secondary publication failure")

    class IncompleteCommand:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompleteBitbucketPullRequestFilesError(secret)

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", outcome != "disabled")
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setitem(pr_agent_module.command2class, "describe", IncompleteCommand)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/describe")

    assert handled is False
    if outcome == "disabled":
        provider_factory.assert_not_called()
    else:
        provider_factory.assert_called_once_with("https://example/pr/1")
    if outcome in {"disabled", "provider_failure"}:
        provider.get_issue_comments_newest_first.assert_not_called()
        provider.is_comment_authored_by_pr_agent.assert_not_called()
        provider.publish_comment.assert_not_called()
    else:
        provider.get_issue_comments_newest_first.assert_called_once()
        provider.is_comment_authored_by_pr_agent.assert_not_called()
        provider.publish_comment.assert_called_once()
        published = provider.publish_comment.call_args.args[0]
        assert pr_agent_module.IncompleteBitbucketPullRequestFilesError.notice_marker not in published
        assert (
            "[pr-agent:bitbucket-incomplete-files]: https://github.com/The-PR-Agent/pr-agent"
            in published
        )
        assert "Bitbucket returned an incomplete or inconsistent pull-request diff" in published
        assert "command was not run" in published
        assert "GitHub" not in published
        assert "3,000" not in published
        assert secret not in published


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["add_docs", "generate_labels", "describe", "review", "improve"]
)
async def test_incomplete_bitbucket_command_error_returns_failure_and_publishes_notice(monkeypatch, action):
    provider = _incomplete_files_provider()
    provider.supports_html_comment_markers.return_value = False

    class IncompleteCommand:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            raise pr_agent_module.IncompleteBitbucketPullRequestFilesError("internal diff details")

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, action, IncompleteCommand)

    handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", f"/{action}")

    assert handled is False
    provider.publish_comment.assert_called_once()
    published = provider.publish_comment.call_args.args[0]
    assert "Bitbucket returned an incomplete or inconsistent pull-request diff" in published
    assert "Failed to review PR" not in published
    assert "Failed to generate code suggestions for PR" not in published


def test_incomplete_bitbucket_files_notice_deduplicates_trusted_agent_comment(monkeypatch):
    marker = pr_agent_module.IncompleteBitbucketPullRequestFilesError.notice_marker
    existing = {"body": f"## Existing notice\n\n{marker}\n\nDetails"}
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.return_value = True
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompleteBitbucketPullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_not_called()


def test_foreign_incomplete_bitbucket_files_marker_does_not_suppress_notice(monkeypatch):
    marker = pr_agent_module.IncompleteBitbucketPullRequestFilesError.notice_marker
    existing = {"body": f"## Spoofed notice\n\n{marker}\n\nDetails"}
    provider = _incomplete_files_provider([existing])
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompleteBitbucketPullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


def test_unverifiable_incomplete_bitbucket_files_marker_does_not_suppress_notice(monkeypatch):
    marker = pr_agent_module.IncompleteBitbucketPullRequestFilesError.notice_marker
    existing = {"body": f"## Existing notice\n\n{marker}\n\nDetails"}
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.side_effect = RuntimeError("identity unavailable")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompleteBitbucketPullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


@pytest.mark.asyncio
async def test_unexpected_constructor_error_does_not_publish_incomplete_files_notice(monkeypatch):
    provider_factory = Mock()

    class BrokenCommand:
        def __init__(self, pr_url, ai_handler, args):
            raise RuntimeError("unrelated")

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", BrokenCommand)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider_factory.assert_not_called()


def test_incomplete_files_notice_deduplicates_trusted_agent_comment(monkeypatch):
    existing = {
        "body": (
            "## Existing notice\n\n"
            f"{pr_agent_module.IncompletePullRequestFilesError.notice_marker}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.return_value = True
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_not_called()


def test_foreign_incomplete_files_marker_does_not_suppress_notice(monkeypatch):
    existing = {
        "body": (
            "## Spoofed notice\n\n"
            f"{pr_agent_module.IncompletePullRequestFilesError.notice_marker}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_author_cannot_be_verified(monkeypatch):
    existing = {
        "body": (
            "## Existing notice\n\n"
            f"{pr_agent_module.IncompletePullRequestFilesError.notice_marker}\n\nDetails"
        )
    }
    provider = _incomplete_files_provider([existing])
    provider.is_comment_authored_by_pr_agent.side_effect = RuntimeError("identity unavailable")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_comment_lookup_fails(monkeypatch):
    provider = _incomplete_files_provider()
    provider.get_issue_comments_newest_first.side_effect = RuntimeError("lookup unavailable")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_publishes_when_lazy_comment_page_fails(monkeypatch):
    provider = _incomplete_files_provider()

    def incomplete_comments():
        yield {"body": "Unrelated comment"}
        raise RuntimeError("comment page unavailable")

    provider.get_issue_comments_newest_first.return_value = incomplete_comments()
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_fails_open_when_comment_body_cannot_be_read(monkeypatch):
    provider = _incomplete_files_provider([object()])
    provider._get_comment_body.side_effect = RuntimeError("comment decoding failed")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider.publish_comment.assert_called_once()


@pytest.mark.asyncio
async def test_incomplete_files_notice_publication_failure_keeps_command_failed(monkeypatch):
    provider = _incomplete_files_provider()
    provider.publish_comment.side_effect = RuntimeError("publication unavailable")

    class IncompleteCommand:
        def __init__(self, pr_url, ai_handler, args):
            raise pr_agent_module.IncompletePullRequestFilesError("internal details")

    _patch_request_dependencies(monkeypatch)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _pr_url: provider)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", IncompleteCommand)

    handled = await pr_agent_module.PRAgent()._handle_request(
        "https://example/pr/1", "/custom"
    )

    assert handled is False
    provider.publish_comment.assert_called_once()


def test_incomplete_files_notice_respects_disabled_output(monkeypatch):
    provider_factory = Mock()
    monkeypatch.setitem(get_settings().config, "publish_output", False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)

    pr_agent_module.publish_incomplete_files_comment(
        "https://example/pr/1", pr_agent_module.IncompletePullRequestFilesError("incomplete")
    )

    provider_factory.assert_not_called()


@pytest.mark.asyncio
async def test_handle_request_wrapper_returns_false_on_exception(monkeypatch):
    async def raise_error(self, pr_url, request, notify=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(pr_agent_module.PRAgent, "_handle_request", raise_error)

    handled = await pr_agent_module.PRAgent().handle_request("https://example/pr/1", "/review")

    assert handled is False


@pytest.mark.asyncio
async def test_handle_request_propagates_cancellation(monkeypatch):
    started = asyncio.Event()

    class BlockingCommand:
        def __init__(self, pr_url, ai_handler, args):
            pass

        async def run(self):
            started.set()
            await asyncio.Event().wait()

    _patch_request_dependencies(monkeypatch, update_settings_fn=pr_agent_module.update_settings_from_args)
    monkeypatch.setitem(
        pr_agent_module.command2class, "blocking", BlockingCommand
    )

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.RESPONSE_LANGUAGE", "de-DE")
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Keep original instructions")
        task = asyncio.create_task(
            pr_agent_module.PRAgent(ai_handler="fake-ai").handle_request(
                "https://example/pr/1", "/blocking --config.response_language=fr-FR"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        assert "'fr-FR'" in settings.pr_reviewer.extra_instructions
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert settings.pr_reviewer.extra_instructions == "Keep original instructions"
        assert settings.config.response_language == "de-DE"


@pytest.mark.asyncio
async def test_unscoped_dispatcher_does_not_load_artifact_or_change_instructions(monkeypatch):
    settings = get_settings()
    original_instructions = settings.pr_reviewer.extra_instructions
    observed = []

    class FakeCommand:
        def __init__(self, _pr_url, ai_handler=None, args=None):
            observed.append(str(settings.pr_reviewer.extra_instructions))

        async def run(self):
            return None

    def fail_if_loaded():
        pytest.fail("dispatcher without an ingress must not load an artifact")

    token = artifacts._artifact_context.set(None)
    try:
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Unscoped instructions")
        _patch_request_dependencies(monkeypatch)
        monkeypatch.setattr(artifacts, "load_artifact_context", fail_if_loaded)
        monkeypatch.setattr(artifacts, "_read_and_truncate", fail_if_loaded)
        monkeypatch.setitem(pr_agent_module.command2class, "review", FakeCommand)

        handled = await pr_agent_module.PRAgent()._handle_request("https://example/pr/1", "/review")

        assert handled is True
        assert observed == ["Unscoped instructions"]
        assert artifacts.get_artifact_context("pr_reviewer") is None
    finally:
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", original_instructions)
        artifacts._artifact_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/unknown", "/answer", "/auto_review"])
async def test_handle_request_returns_false_for_unregistered_command(monkeypatch, command):
    _patch_request_dependencies(monkeypatch, update_settings_fn=pr_agent_module.update_settings_from_args)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.RESPONSE_LANGUAGE", "en-us")
        handled = await pr_agent_module.PRAgent()._handle_request(
            "https://example/pr/1", f"{command} --config.response_language=fr-FR"
        )
        assert settings.config.response_language == "en-us"

    assert handled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error_stage", ["arguments", "setup"])
async def test_handle_request_restores_language_after_invalid_arguments(monkeypatch, error_stage):
    _patch_request_dependencies(monkeypatch, update_settings_fn=pr_agent_module.update_settings_from_args)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.RESPONSE_LANGUAGE", "de-DE")
        settings.set("CONFIG.PROPAGATE_COMMAND_ERRORS", False)
        command = "/review --config.response_language=fr-FR"
        if error_stage == "arguments":
            command += " --config=123"
        else:
            monkeypatch.setattr(pr_agent_module, "init_run_details", Mock(side_effect=RuntimeError("setup failed")))
        handled = await pr_agent_module.PRAgent()._handle_request(
            "https://example/pr/1", command, propagate_command_errors=True
        )
        assert handled is False
        assert settings.config.response_language == "de-DE"
        assert settings.get("CONFIG.PROPAGATE_COMMAND_ERRORS", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("original_instructions", ["Keep user instructions", None])
@pytest.mark.parametrize("base_language", ["en-us", "de-DE"])
@pytest.mark.parametrize("prepare_automatic", [False, True])
async def test_handle_request_scopes_language_instructions_to_each_command(
    monkeypatch, original_instructions, base_language, prepare_automatic
):
    observed = []

    class FakeReviewer:
        def __init__(self, *_args, **_kwargs):
            self.instructions = get_settings().pr_reviewer.extra_instructions

        async def run(self):
            await asyncio.sleep(0)
            assert get_settings().pr_reviewer.extra_instructions == self.instructions
            observed.append(self.instructions)

    _patch_request_dependencies(monkeypatch, update_settings_fn=pr_agent_module.update_settings_from_args)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FakeReviewer)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", original_instructions)
        settings.set("CONFIG.RESPONSE_LANGUAGE", base_language)
        worker = pr_agent_module.PRAgent()
        for language in ("de-DE", "de-DE", "fr-FR", "EN-US"):
            command = f"/review --config.response_language={language}"
            if prepare_automatic:
                command = pr_agent_module.prepare_command(command)
                assert settings.config.response_language == base_language
            assert await worker._handle_request("https://example/pr/1", command) is True
            assert settings.pr_reviewer.extra_instructions == original_instructions
            assert settings.config.response_language == base_language
        plain_command = pr_agent_module.prepare_command("/review") if prepare_automatic else "/review"
        assert await worker._handle_request("https://example/pr/1", plain_command) is True
        assert settings.config.response_language == base_language

    assert observed[0] == observed[1]
    for instructions, language in zip(observed[:3], ("de-DE", "de-DE", "fr-FR"), strict=True):
        assert instructions.count("locale code:") == 1
        assert f"'{language}'" in instructions
        assert "Keep schema control values" in instructions
        assert "'No'" in instructions
        assert "do not translate them" in instructions
        if original_instructions:
            assert instructions.startswith(original_instructions)
    assert "de-DE" not in observed[2]
    assert observed[3] == original_instructions
    if base_language == "en-us":
        assert observed[4] == original_instructions
    else:
        assert observed[4].count("locale code:") == 1
        assert "'de-DE'" in observed[4]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["constructor", "run"])
async def test_handle_request_restores_language_instructions_after_failure(monkeypatch, failure_stage):
    observed = []

    class BrokenReviewer:
        def __init__(self, *_args, **_kwargs):
            observed.append(get_settings().pr_reviewer.extra_instructions)
            if failure_stage == "constructor":
                raise RuntimeError("Command setup failed")

        async def run(self):
            observed.append(get_settings().pr_reviewer.extra_instructions)
            raise RuntimeError("Command execution failed")

    _patch_request_dependencies(monkeypatch, update_settings_fn=pr_agent_module.update_settings_from_args)
    monkeypatch.setitem(pr_agent_module.command2class, "review", BrokenReviewer)
    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.RESPONSE_LANGUAGE", "de-DE")
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Keep user instructions")
        assert await pr_agent_module.PRAgent()._handle_request(
            "https://example/pr/1", "/review --config.response_language=fr-FR"
        ) is False
        assert settings.pr_reviewer.extra_instructions == "Keep user instructions"
        assert settings.config.response_language == "de-DE"

    assert len(observed) == (1 if failure_stage == "constructor" else 2)
    assert all("'fr-FR'" in instructions for instructions in observed)
