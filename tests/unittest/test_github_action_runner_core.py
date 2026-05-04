import copy
import json

import pytest

import pr_agent.servers.github_action_runner as github_action_runner
from pr_agent.config_loader import get_settings


def test_is_true_accepts_bool_and_case_insensitive_true_string():
    assert github_action_runner.is_true(True) is True
    assert github_action_runner.is_true(False) is False
    assert github_action_runner.is_true("TRUE") is True
    assert github_action_runner.is_true("false") is False
    assert github_action_runner.is_true(None) is False


@pytest.mark.asyncio
async def test_run_action_returns_when_required_env_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    await github_action_runner.run_action()

    assert "GITHUB_EVENT_NAME not set" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_action_invokes_enabled_auto_tools_for_pull_request_event(monkeypatch, tmp_path):
    settings = get_settings()
    original_is_auto_command = settings.config.get("is_auto_command", False)
    original_final_update_message = settings.pr_description.final_update_message
    original_response_language = settings.config.response_language
    had_github_settings = "GITHUB" in settings
    original_github_settings = copy.deepcopy(settings.get("GITHUB", None))
    had_github_action_config = "GITHUB_ACTION_CONFIG" in settings
    original_github_action_config = copy.deepcopy(settings.get("GITHUB_ACTION_CONFIG", None))
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "opened",
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
        },
    }))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.PR_ACTIONS": ["opened"],
            "GITHUB_ACTION.AUTO_DESCRIBE": True,
            "GITHUB_ACTION.AUTO_REVIEW": False,
            "GITHUB_ACTION.AUTO_IMPROVE": True,
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)
    runs = []

    class FakeTool:
        name = "base"

        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            runs.append((self.name, self.pr_url))

    class FakeDescription(FakeTool):
        name = "describe"

    class FakeReviewer(FakeTool):
        name = "review"

    class FakeSuggestions(FakeTool):
        name = "improve"

    monkeypatch.setattr(github_action_runner, "PRDescription", FakeDescription)
    monkeypatch.setattr(github_action_runner, "PRReviewer", FakeReviewer)
    monkeypatch.setattr(github_action_runner, "PRCodeSuggestions", FakeSuggestions)

    try:
        settings.config.response_language = "en-us"

        await github_action_runner.run_action()

        assert runs == [
            ("describe", "https://api.github.com/repos/org/repo/pulls/1"),
            ("improve", "https://api.github.com/repos/org/repo/pulls/1"),
        ]
    finally:
        settings.config.is_auto_command = original_is_auto_command
        settings.pr_description.final_update_message = original_final_update_message
        settings.config.response_language = original_response_language
        if had_github_settings:
            settings.set("GITHUB", original_github_settings)
        else:
            settings.unset("GITHUB", force=True)
        if had_github_action_config:
            settings.set("GITHUB_ACTION_CONFIG", original_github_action_config)
        else:
            settings.unset("GITHUB_ACTION_CONFIG", force=True)
