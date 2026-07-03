from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent import cli


def test_set_parser_supports_config_branch_flag():
    args = cli.set_parser().parse_args(["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "feature", "review"])
    assert args.config_branch == "feature"


def test_run_sets_config_branch_from_cli_flag():
    fake_settings = SimpleNamespace(
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        return True

    with patch("pr_agent.cli.get_settings", return_value=fake_settings), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "feature", "review"])

    fake_settings.set.assert_any_call("CONFIG.CONFIG_BRANCH", "feature")


def test_run_sets_config_branch_from_env_var():
    fake_settings = SimpleNamespace(
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        return True

    with patch.dict("os.environ", {"PR_AGENT_CONFIG_BRANCH": "env-branch"}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    fake_settings.set.assert_any_call("CONFIG.CONFIG_BRANCH", "env-branch")


def test_run_whitespace_cli_branch_falls_back_to_env_var():
    """A whitespace-only --config-branch must not short-circuit the env fallback."""
    fake_settings = SimpleNamespace(
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        return True

    with patch.dict("os.environ", {"PR_AGENT_CONFIG_BRANCH": "env-branch"}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "--config-branch", "   ", "review"])

    fake_settings.set.assert_any_call("CONFIG.CONFIG_BRANCH", "env-branch")


def test_run_reconciles_config_branch_when_absent():
    """Without a flag or env var, CONFIG.CONFIG_BRANCH must be reset (not leaked
    from a previous run() call in the same process-wide settings singleton)."""
    fake_settings = SimpleNamespace(
        litellm={},
        set=MagicMock(),
    )

    async def fake_handle_request(*_args, **_kwargs):
        return True

    with patch.dict("os.environ", {}, clear=False), patch(
        "pr_agent.cli.get_settings",
        return_value=fake_settings,
    ), patch(
        "pr_agent.cli.PRAgent",
        return_value=SimpleNamespace(handle_request=fake_handle_request),
    ):
        import os as _os

        _os.environ.pop("PR_AGENT_CONFIG_BRANCH", None)
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    fake_settings.set.assert_any_call("CONFIG.CONFIG_BRANCH", None)
