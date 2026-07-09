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


@pytest.fixture
def restore_github_settings():
    """run_action mutates global GITHUB/GITHUB_ACTION_CONFIG/GITHUB_APP settings; snapshot
    and restore them so these tests don't leak state into others."""
    settings = get_settings()
    had_github = "GITHUB" in settings
    original_github = copy.deepcopy(settings.get("GITHUB", None))
    had_cfg = "GITHUB_ACTION_CONFIG" in settings
    original_cfg = copy.deepcopy(settings.get("GITHUB_ACTION_CONFIG", None))
    had_app = "GITHUB_APP" in settings
    original_app = copy.deepcopy(settings.get("GITHUB_APP", None))
    yield
    if had_github:
        settings.set("GITHUB", original_github)
    else:
        settings.unset("GITHUB", force=True)
    if had_cfg:
        settings.set("GITHUB_ACTION_CONFIG", original_cfg)
    else:
        settings.unset("GITHUB_ACTION_CONFIG", force=True)
    if had_app:
        settings.set("GITHUB_APP", original_app)
    else:
        settings.unset("GITHUB_APP", force=True)


def _write_synchronize_event(tmp_path, before_sha="abc", after_sha="def", merge_commit_sha=None, sender_type="User"):
    payload = {
        "action": "synchronize",
        "before": before_sha,
        "after": after_sha,
        "sender": {"type": sender_type},
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
        },
    }
    if merge_commit_sha is not None:
        payload["pull_request"]["merge_commit_sha"] = merge_commit_sha
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    return event_path


def _write_issue_comment_event(tmp_path, sender_type):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": "/review", "id": 123},
        "issue": {
            "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/1"},
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": sender_type},
    }))
    return event_path


def _patch_issue_comment_deps(monkeypatch, handled):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

    class FakeProvider:
        def __init__(self, pr_url=None):
            self.pr_url = pr_url

        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            return None

    monkeypatch.setattr(github_action_runner, "get_git_provider", lambda: FakeProvider)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)


@pytest.mark.asyncio
async def test_issue_comment_from_bot_sender_is_skipped(monkeypatch, tmp_path, restore_github_settings):
    """Regression for #2398: a comment authored by a bot (e.g. pr-agent's own
    'Preparing review...' message) must not be parsed as a command, which would
    re-trigger the action in a feedback loop."""
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_issue_comment_event(tmp_path, "Bot")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []  # bot comment skipped; no command handled


def _patch_synchronize_deps(monkeypatch, handled, push_commands, handle_push_trigger=True):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_app"], "push_commands", list(push_commands))
    settings.set("github_action_config", {
        "handle_push_trigger": handle_push_trigger,
        "push_trigger_ignore_merge_commits": False,
        "push_trigger_ignore_bot_commits": False,
    }, merge=False)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)


@pytest.mark.asyncio
async def test_synchronize_event_triggers_push_commands(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe", "/improve"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", "/describe"),
        ("https://api.github.com/repos/org/repo/pulls/1", "/improve"),
    ]


@pytest.mark.asyncio
async def test_synchronize_skips_when_push_trigger_disabled(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"], handle_push_trigger=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_synchronize_skips_equal_before_after_sha(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    event_path = _write_synchronize_event(tmp_path, before_sha="same", after_sha="same")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_synchronize_event_triggers_push_commands_on_pull_request_target(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe", "/improve"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", "/describe"),
        ("https://api.github.com/repos/org/repo/pulls/1", "/improve"),
    ]


@pytest.mark.asyncio
async def test_synchronize_skips_merge_commit(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"])
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], "push_trigger_ignore_merge_commits", True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(
        tmp_path, before_sha="abc", after_sha="merge123", merge_commit_sha="merge123"
    )))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_synchronize_skips_bot_commit(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"])
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], "push_trigger_ignore_bot_commits", True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(
        tmp_path, sender_type="Bot"
    )))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_synchronize_uses_github_action_config_push_commands(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/review"], handle_push_trigger=True)
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], "push_commands", ["/describe"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", "/describe"),
    ]


@pytest.mark.asyncio
async def test_issue_comment_from_user_is_processed(monkeypatch, tmp_path, restore_github_settings):
    """The bot guard must not over-skip: a human comment is still handled."""
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_issue_comment_event(tmp_path, "User")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", "/review")]
