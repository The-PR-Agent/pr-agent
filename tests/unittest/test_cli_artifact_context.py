import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from starlette_context import request_cycle_context

from pr_agent import cli
from pr_agent.agent import pr_agent as agent_module
from pr_agent.algo import artifacts
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils


def test_run_injects_the_artifact_context_before_handling_the_request():
    """A pipeline that runs the CLI gets the same [artifacts] injection as the GitHub Action."""
    order = []
    fake_settings = SimpleNamespace(config={}, litellm={}, set=MagicMock())

    async def fake_handle_request(*_args, **_kwargs):
        order.append("handle_request")
        return True

    with patch("pr_agent.cli.get_settings", return_value=fake_settings), \
         patch("pr_agent.cli.inject_artifact_context", side_effect=lambda: order.append("inject")), \
         patch("pr_agent.cli.litellm_callbacks_registered", return_value=False), \
         patch("pr_agent.cli.PRAgent", return_value=SimpleNamespace(handle_request=fake_handle_request)):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    assert order == ["inject", "handle_request"]


def test_cli_disabled_ingress_does_not_reuse_a_calling_context_payload(monkeypatch):
    observed = []

    class FakeAgent:
        async def handle_request(self, *_args, **_kwargs):
            artifacts.reapply_artifact_context()
            observed.append(str(get_settings().pr_reviewer.extra_instructions))
            return True

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("ARTIFACTS.ENABLE", False)
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "")
        monkeypatch.delenv("ARTIFACT_PATH", raising=False)
        monkeypatch.delenv("PR_AGENT_ARTIFACT_PATH", raising=False)
        payload = ("STALE_ARTIFACT", frozenset({"pr_reviewer"}))
        token = artifacts._artifact_context.set(payload)
        try:
            monkeypatch.setattr(cli, "PRAgent", FakeAgent)
            monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: False)

            cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", "review"])

            assert observed == [""]
            assert artifacts._artifact_context.get() == payload
        finally:
            artifacts._artifact_context.reset(token)


@pytest.mark.parametrize("command_override", [False, True])
def test_cli_artifact_survives_effective_instructions(monkeypatch, tmp_path, command_override):
    report = tmp_path / "report.txt"
    report.write_text("CI_FAILURE_MARKER")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("UNSELECTED_ARTIFACT")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_PATH", str(report))

    class Provider:
        def get_repo_settings(self):
            return '''
[artifacts]
artifact_path = "replacement.txt"
target_tools = ["pr_description"]
[pr_reviewer]
extra_instructions = "Repository instructions"
'''

    observed = []

    class RecordingReviewer:
        def __init__(self, *_args, **_kwargs):
            observed.append((get_settings().pr_reviewer.extra_instructions,
                             get_settings().pr_description.extra_instructions))

        async def run(self):
            pass

    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda _url: Provider())
    monkeypatch.setitem(agent_module.command2class, "review", RecordingReviewer)
    monkeypatch.setattr(agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: False)
    read = MagicMock(wraps=artifacts._read_and_truncate)
    monkeypatch.setattr(artifacts, "_read_and_truncate", read)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("ARTIFACTS.TARGET_TOOLS", ["pr_reviewer"])
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "")
        settings.set("PR_DESCRIPTION.EXTRA_INSTRUCTIONS", "")
        settings.set("CONFIG.RESPONSE_LANGUAGE", "es")
        argv = ["--pr_url=https://example.invalid/org/repo/pull/1", "review"]
        if command_override:
            argv.append('--pr_reviewer.extra_instructions="Command instructions"')
        cli.run(argv)

    assert len(observed) == 1
    reviewer, description = observed[0]
    assert reviewer.startswith("Command instructions" if command_override else "Repository instructions")
    assert reviewer.count("CI_FAILURE_MARKER") == 1
    assert "locale code: 'es'" in reviewer
    assert "UNSELECTED_ARTIFACT" not in reviewer
    assert "CI_FAILURE_MARKER" not in description
    read.assert_called_once_with(report.resolve(), 50000)
