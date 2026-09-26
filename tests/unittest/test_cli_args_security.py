from unittest.mock import AsyncMock, Mock

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.cli_args import _MAPPING_TOO_COMPLEX_ARG, CliArgs

FORBIDDEN_ARGS = [
    # section-qualified key forms
    "--openai.key=secret",
    "--OPENAI.KEY=secret",
    "--config.openai.key=secret",
    # double-underscore form is normalized to dot before matching
    "--openai__key=secret",
    "--OPENAI__KEY=secret",
    # webhook / app secrets via section-qualified prefix
    "--github.webhook_secret=secret",
    "--github_app.private_key=---BEGIN---",
    "--github_app.app_id=123",
    "--github_app.webhook_secret=secret",
    # base/api URLs (SSRF / redirection style abuses)
    "--github.base_url=https://evil.example",
    "--litellm.api_base=https://evil.example",
    "--litellm.api_type=azure",
    "--litellm.api_version=2024-01-01",
    "--jira.jira_base_url=https://evil.example",
    # gitea.web_url is resolved on first use, so a comment could otherwise redirect published links
    "--gitea.web_url=https://evil.example",
    "--gitea__web_url=https://evil.example",
    "--config.url=https://evil.example",
    "--config.uri=https://evil.example",
    # provider / auth selection and skip lists
    "--config.secret_provider=aws",
    "--config.git_provider=github",
    "--config.skip_keys=foo",
    "--auth.bearer_token=abc",
    "--provider.personal_access_token=ghp_xxx",
    "--provider.PERSONAL_ACCESS_TOKEN=ghp_xxx",
    # approval / deployment toggles
    "--config.enable_auto_approval=true",
    "--config.enable_manual_approval=true",
    "--config.enable_comment_approval=true",
    "--config.approve_pr_on_self_review=true",
    "--config.override_deployment_type=app",
    # local cache
    "--config.enable_local_cache=true",
    "--config.local_cache_path=/etc",
    # misc
    "--config.shared_secret=xxx",
    "--config.app_name=evil",
    "--config.analytics_folder=/tmp",
    # double-underscore variants of the above
    "--github__webhook_secret=secret",
    "--github_app__private_key=xxx",
    "--litellm__api_base=https://evil.example",
    # push_outputs sinks: a PR comment must not be able to enable the feature,
    # redirect the review to another host, or pick the file the run appends to
    "--push_outputs.enable=true",
    '--push_outputs.channels=["webhook"]',
    "--push_outputs.webhook_url=https://evil.example/collect",
    "--push_outputs.slack_webhook_url=https://evil.example/slack",
    "--push_outputs.file_path=/etc/cron.d/pwn",
    "--PUSH_OUTPUTS.WEBHOOK_URL=https://evil.example/collect",
    "--push_outputs__webhook_url=https://evil.example/collect",
    # whole-section form: the dotted entries above do not cover it
    '--push_outputs={"enable": true, "channels": ["webhook"], "webhook_url": "https://evil.example"}',
    # publish_error_details can expose service-side failure state, so it is host-only.
    "--pr_reviewer.publish_error_details=true",
    "--pr_reviewer__publish_error_details=true",
    '--pr_reviewer={"publish_error_details": true}',
    # repo_context_max_sibling_files is host-only: letting a comment raise it would defeat the
    # sibling-fetch safety bound and allow unbounded cross-repository API calls.
    "--config.repo_context_max_sibling_files=1000",
    "--config__repo_context_max_sibling_files=1000",
    '--config={"repo_context_max_sibling_files": 1000}',
    # repo_context_files selects which files become model instructions, so comment arguments
    # must not be able to point the bot at arbitrary sibling repo content.
    '--config.repo_context_files=[{"repo_id": "group/A/idea", "file_path": "AGENTS.md"}]',
    "--config__repo_context_files=[\"AGENTS.md\"]",
    '--config={"repo_context_files": ["AGENTS.md"]}',
    # repo_context_sibling_repos is the host-only allowlist of sibling repositories whose
    # files may be selected; neither repo settings nor comment arguments can change it.
    "--config.repo_context_sibling_repos=[]",
    "--config__repo_context_sibling_repos=[]",
    '--config={"repo_context_sibling_repos": []}',
    # description_issue_regex is compiled and run with finditer over the whole pull-request
    # description. An ambiguous pattern backtracks exponentially, so a commenter who can choose
    # it can burn a worker on a short body; it stays an operator choice.
    "--config.description_issue_regex=(?:[A-Za-z ]+)+X(d+)",
    "--config__description_issue_regex=(?:[A-Za-z ]+)+X(d+)",
    '--config={"description_issue_regex": "(?:[A-Za-z ]+)+X(d+)"}',
    # section-level mapping values on sections that are not host-only themselves:
    # the dotted keys below are all rejected, so their {key: value} forms must be too
    '--qdrant={url: "https://evil.example", api_key: "x"}',
    '--qdrant={replicas: [{base_url: "https://evil.example"}]}',
    '--qdrant={azure: {api_base: "https://evil.example"}}',
    '--github_app={private_key: "---BEGIN---", app_id: 123}',
    '--gitea={web_url: "https://evil.example"}',
    '--openai={key: "sk-leaked"}',
    # an empty container still exposes its key path for validation
    '--qdrant={url: {}}',
    '--qdrant={server: {url: []}}',
]


ALLOWED_ARGS_SINGLE = [
    "--pr_reviewer.num_code_suggestions=3",
    "--pr_reviewer.require_tests_review=true",
    "--skills.enabled=true",
    "--skills.max_skills_tokens=1000",
    "--config.response_language=zh-tw",
    "--pr_description.publish_labels=false",
    # a mapping value whose nested keys are all allowed stays accepted
    "--qdrant={timeout: 5, prefer_grpc: true}",
    "--pr_similar_issue={vectordb: qdrant, max_issues_to_scan: 50}",
    # non-flag arguments are not validated against the forbidden list
    "some-positional-arg",
    "yes",
    "because prod is broken",
    "",
]


HOST_ONLY_ARGS = [
    "--skills.paths=/etc",
    "--skills__paths=/etc",
    "--skills.unknown=value",
    "--skills={paths:[/etc]}",
    "--skills={nested: {paths: [\" /etc\", \"/etc\"]}}",
    "--skills={paths: {}}",
    '--prompt_fragments={diff_hunk_format: []}',
    "--prompt_fragments.diff_hunk_format={{ cycler.__init__.__globals__ }}",
    "--prompt_fragments__diff_hunk_format=unsafe",
    '--prompt_fragments={"diff_hunk_format": "unsafe"}',
]


@pytest.mark.parametrize("forbidden", FORBIDDEN_ARGS)
def test_validate_user_args_rejects_forbidden(forbidden):
    ok, offending = CliArgs.validate_user_args([forbidden])
    assert ok is False, f"Expected {forbidden!r} to be rejected"
    assert isinstance(offending, str) and offending, (
        f"Expected an offending-token string for {forbidden!r}, got {offending!r}"
    )


@pytest.mark.parametrize("host_only", HOST_ONLY_ARGS)
def test_validate_user_args_rejects_keys_not_in_repo_allowlist(host_only):
    ok, offending = CliArgs.validate_user_args([host_only])
    assert ok is False
    assert offending.lstrip('.') in host_only.lower().replace('__', '.')


@pytest.mark.parametrize("allowed", ALLOWED_ARGS_SINGLE)
def test_validate_user_args_accepts_allowed_single(allowed):
    ok, offending = CliArgs.validate_user_args([allowed])
    assert ok is True, (
        f"Expected {allowed!r} to be accepted, but it was rejected as {offending!r}"
    )
    assert offending == ""


def test_validate_user_args_empty_list_is_allowed():
    assert CliArgs.validate_user_args([]) == (True, "")


def test_validate_user_args_none_is_allowed():
    # falsy args short-circuit to allowed
    assert CliArgs.validate_user_args(None) == (True, "")


def test_validate_user_args_mixed_allowed_then_forbidden():
    ok, offending = CliArgs.validate_user_args(
        ["--pr_reviewer.num_code_suggestions=3", "--github.webhook_secret=secret"]
    )
    assert ok is False
    assert "webhook_secret" in offending


def test_validate_user_args_all_allowed_together():
    ok, offending = CliArgs.validate_user_args(ALLOWED_ARGS_SINGLE)
    assert ok is True, f"Allowed batch unexpectedly rejected at {offending!r}"
    assert offending == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forbidden",
    [
        "--github.webhook_secret=secret",
        "--openai__key=secret",
        "--push_outputs=enabled",
    ],
)
async def test_handle_request_uses_real_validator_to_block_forbidden(monkeypatch, forbidden):
    """Integration test: forbidden CLI arg should be rejected by the real
    CliArgs.validate_user_args, before any settings update, tool
    instantiation, tool run, or notify call happens."""

    notify = Mock()
    update_settings = Mock()
    tool_factory = Mock()

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", tool_factory)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1",
        f"/custom {forbidden}",
        notify,
    )

    assert handled is False
    update_settings.assert_not_called()
    tool_factory.assert_not_called()
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_request",
    [
        # listed section: the whole-section form is already host-only
        "/custom --push_outputs={url:https://evil.example}",
        ["/custom", "--push_outputs={url:https://evil.example}"],
        # unlisted section: the nested url key must be caught inside the mapping value
        "/custom --qdrant={url:https://evil.example}",
        ["/custom", "--qdrant={url:https://evil.example}"],
    ],
)
async def test_handle_request_rejects_forbidden_mapping_args_in_comment_and_cli(
    monkeypatch, command_request
):
    """A --section={key: value} arg is rejected for both comment and CLI request forms."""
    notify = Mock()
    update_settings = Mock()
    tool_factory = Mock()

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", tool_factory)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", command_request, notify
    )

    assert handled is False
    update_settings.assert_not_called()
    tool_factory.assert_not_called()
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_request",
    [
        # the settings loader strips the value before parsing it, so the validator must too
        '/custom --qdrant="\t{url: https://evil.example}"',
        ["/custom", "--qdrant=\t{url: https://evil.example}"],
        # an empty mapping has no nested paths, so the section path itself is validated
        "/custom --qdrant.url={}",
        ["/custom", "--qdrant.url={}"],
    ],
)
async def test_handle_request_rejects_mapping_args_as_the_settings_loader_parses_them(
    monkeypatch, command_request
):
    """A mapping value is validated as update_settings_from_args would apply it, and a
    rejected argument leaves the settings untouched."""
    notify = Mock()
    update_settings = Mock(wraps=pr_agent_module.update_settings_from_args)
    tool_factory = Mock()
    qdrant_url_before = pr_agent_module.get_settings().get("qdrant.url")

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", tool_factory)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", command_request, notify
    )

    assert handled is False
    update_settings.assert_not_called()
    assert pr_agent_module.get_settings().get("qdrant.url") == qdrant_url_before
    tool_factory.assert_not_called()
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_request",
    [
        '/custom --pr_reviewer.extra_instructions="Flag any hardcoded openai.key in the diff"',
        ["/custom", "--pr_reviewer.extra_instructions=Flag any hardcoded openai.key in the diff"],
    ],
)
async def test_handle_request_allows_protected_key_names_in_setting_values(monkeypatch, command_request):
    """Validate setting keys while retaining original values for direct string and list requests."""
    expected_args = ["--pr_reviewer.extra_instructions=Flag any hardcoded openai.key in the diff"]
    update_settings = Mock(side_effect=lambda args: args)
    tool = Mock()
    tool.run = AsyncMock()
    tool_factory = Mock(return_value=tool)
    notify = Mock()

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _pr_url: None)
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", update_settings)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", tool_factory)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", command_request, notify
    )

    assert handled is True
    update_settings.assert_called_once_with(expected_args)
    tool_factory.assert_called_once_with("https://example/pr/1", ai_handler="fake-ai", args=expected_args)
    tool.run.assert_awaited_once_with()
    notify.assert_called_once_with()


@pytest.mark.parametrize("prefix", ["  ", "\t", "\n", " \t "])
def test_validate_user_args_rejects_forbidden_arg_with_leading_whitespace(prefix):
    """Reject a forbidden argument that arrives with leading whitespace, since
    update_settings_from_args strips the token before applying it."""
    ok, offending = CliArgs.validate_user_args([f"{prefix}--github.webhook_secret=secret"])
    assert ok is False
    assert "webhook_secret" in offending


@pytest.mark.parametrize(
    "cyclic",
    [
        "--qdrant={\"x\": &a [*a]}",
        "--qdrant={\"a\": &x {\"b\": *x}}",
    ],
)
def test_validate_user_args_rejects_cyclic_mapping_value(cyclic):
    """A mapping value that reuses an ancestor object must be rejected instead of
    recursing forever through YAML aliases."""
    ok, offending = CliArgs.validate_user_args([cyclic])
    assert ok is False
    assert offending == _MAPPING_TOO_COMPLEX_ARG


def test_validate_user_args_rejects_mapping_value_beyond_depth_limit():
    nested = '{"a": ' * 40 + '"leaf"' + '}' * 40
    ok, offending = CliArgs.validate_user_args([f"--qdrant={nested}"])
    assert ok is False
    assert offending == _MAPPING_TOO_COMPLEX_ARG


def test_validate_user_args_rejects_mapping_value_beyond_visit_limit():
    wide = "{" + ", ".join(f'"k{i}": 1' for i in range(200)) + "}"
    ok, offending = CliArgs.validate_user_args([f"--qdrant={wide}"])
    assert ok is False
    assert offending == _MAPPING_TOO_COMPLEX_ARG
