from unittest.mock import Mock

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.cli_args import CliArgs

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
    "--config.url=https://evil.example",
    "--config.uri=https://evil.example",
    # provider / auth selection and skip lists
    "--config.secret_provider=aws",
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
]


ALLOWED_ARGS_SINGLE = [
    "--pr_reviewer.num_code_suggestions=3",
    "--pr_reviewer.require_tests_review=true",
    "--config.response_language=zh-tw",
    "--pr_description.publish_labels=false",
    # git_provider is allowed (removed from forbidden list so CLI can select provider)
    "--config.git_provider=gitlab",
    # non-flag arguments are not validated against the forbidden list
    "some-positional-arg",
    "yes",
    "because prod is broken",
    "",
]


@pytest.mark.parametrize("forbidden", FORBIDDEN_ARGS)
def test_validate_user_args_rejects_forbidden(forbidden):
    ok, offending = CliArgs.validate_user_args([forbidden])
    assert ok is False, f"Expected {forbidden!r} to be rejected"
    assert isinstance(offending, str) and offending, (
        f"Expected an offending-token string for {forbidden!r}, got {offending!r}"
    )


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
async def test_handle_request_uses_real_validator_to_block_forbidden(monkeypatch):
    """Integration test: forbidden CLI arg should be rejected by the real
    CliArgs.validate_user_args, before any settings update, tool
    instantiation, tool run, or notify call happens."""

    notify = Mock()

    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)

    def _fail_update_settings(args):
        raise AssertionError(
            "update_settings_from_args must not be called when validation fails"
        )

    monkeypatch.setattr(
        pr_agent_module, "update_settings_from_args", _fail_update_settings
    )

    class FakeTool:
        def __init__(self, *args, **kwargs):
            raise AssertionError("tool must not be instantiated for forbidden args")

        async def run(self):
            raise AssertionError("tool must not run for forbidden args")

    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1",
        "/custom --github.webhook_secret=secret",
        notify,
    )

    assert handled is False
    notify.assert_not_called()
