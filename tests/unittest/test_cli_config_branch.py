from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent import cli


def test_set_parser_supports_config_branch_flag():
    args = cli.set_parser().parse_args(
        ["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "feature", "review"]
    )
    assert args.config_branch == "feature"


def test_run_sets_config_branch_from_cli_flag():
    observed_calls = []
    fake_settings = SimpleNamespace(
        config={},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        observed_calls.extend(cli.get_settings().set.call_args_list)
        return True

    with patch("pr_agent.cli.get_settings", return_value=fake_settings), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ), patch(
        "pr_agent.cli.litellm_callbacks_registered", return_value=False,
    ), patch(
        "pr_agent.cli.inject_artifact_context", return_value=None,
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "feature", "review"])

    assert ("CONFIG.CONFIG_BRANCH", "feature") in [call.args for call in observed_calls]


def test_run_sets_config_branch_from_env_var():
    observed_calls = []
    fake_settings = SimpleNamespace(
        config={},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        observed_calls.extend(cli.get_settings().set.call_args_list)
        return True

    with patch.dict("os.environ", {"PR_AGENT_CONFIG_BRANCH": "env-branch"}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ), patch(
        "pr_agent.cli.litellm_callbacks_registered", return_value=False,
    ), patch(
        "pr_agent.cli.inject_artifact_context", return_value=None,
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    assert ("CONFIG.CONFIG_BRANCH", "env-branch") in [call.args for call in observed_calls]


def test_run_whitespace_cli_branch_falls_back_to_env_var():
    """A whitespace-only --config-branch must not short-circuit the env fallback."""
    observed_calls = []
    fake_settings = SimpleNamespace(
        config={},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        observed_calls.extend(cli.get_settings().set.call_args_list)
        return True

    with patch.dict("os.environ", {"PR_AGENT_CONFIG_BRANCH": "env-branch"}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ), patch(
        "pr_agent.cli.litellm_callbacks_registered", return_value=False,
    ), patch(
        "pr_agent.cli.inject_artifact_context", return_value=None,
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "   ", "review"])

    assert ("CONFIG.CONFIG_BRANCH", "env-branch") in [call.args for call in observed_calls]


def test_run_reconciles_config_branch_when_absent():
    """Without a flag or env var, the invocation receives a reset branch setting."""
    observed_calls = []
    fake_settings = SimpleNamespace(
        config={},
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        observed_calls.extend(cli.get_settings().set.call_args_list)
        return True

    with patch.dict("os.environ", {}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ), patch(
        "pr_agent.cli.litellm_callbacks_registered", return_value=False,
    ), patch(
        "pr_agent.cli.inject_artifact_context", return_value=None,
    ):
        import os as _os

        _os.environ.pop("PR_AGENT_CONFIG_BRANCH", None)
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    assert ("CONFIG.CONFIG_BRANCH", None) in [call.args for call in observed_calls]
