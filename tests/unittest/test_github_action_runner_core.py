import copy
import json
from unittest.mock import Mock

import pytest

import pr_agent.agent.pr_agent as pr_agent_module
import pr_agent.servers.github_action_runner as github_action_runner
from pr_agent.algo import artifacts
from pr_agent.algo.run_details import command_failed, get_run_details, record_command_failure
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.git_providers.github_provider import IncompletePullRequestFilesError


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
async def test_direct_auto_tool_notifies_and_reraises_incomplete_constructor_error(monkeypatch):
    error = IncompletePullRequestFilesError("private repository details")
    notify = Mock()

    class IncompleteTool:
        def __init__(self, _pr_url):
            raise error

    monkeypatch.setattr(github_action_runner, "publish_incomplete_files_comment", notify)

    with pytest.raises(IncompletePullRequestFilesError) as raised:
        await github_action_runner._run_auto_tool(IncompleteTool, "https://example/pr/1")

    assert raised.value is error
    notify.assert_called_once_with("https://example/pr/1", error)


@pytest.mark.asyncio
async def test_direct_auto_tool_leaves_unexpected_constructor_error_unchanged(monkeypatch):
    error = RuntimeError("unrelated")
    notify = Mock()

    class BrokenTool:
        def __init__(self, _pr_url):
            raise error

    monkeypatch.setattr(github_action_runner, "publish_incomplete_files_comment", notify)

    with pytest.raises(RuntimeError) as raised:
        await github_action_runner._run_auto_tool(BrokenTool, "https://example/pr/1")

    assert raised.value is error
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_direct_auto_tool_publishes_and_preserves_error_when_comment_decode_fails(
    monkeypatch
):
    error = IncompletePullRequestFilesError("private repository details")
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = [object()]
    provider._get_comment_body.side_effect = RuntimeError("comment decoding failed")
    monkeypatch.setattr(get_settings().config, "publish_output", True, raising=False)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _url: provider)

    class IncompleteTool:
        def __init__(self, _pr_url):
            raise error

    with pytest.raises(IncompletePullRequestFilesError) as raised:
        await github_action_runner._run_auto_tool(IncompleteTool, "https://example/pr/1")

    assert raised.value is error
    provider.publish_comment.assert_called_once()


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
    """Snapshot and restore global settings that run_action mutates.

    Covers GITHUB/GITHUB_ACTION_CONFIG/GITHUB_APP plus task-local artifact context
    and extra_instructions so these tests don't leak state into others.
    """
    settings = get_settings()
    had_github = "GITHUB" in settings
    original_github = copy.deepcopy(settings.get("GITHUB", None))
    had_cfg = "GITHUB_ACTION_CONFIG" in settings
    original_cfg = copy.deepcopy(settings.get("GITHUB_ACTION_CONFIG", None))
    had_app = "GITHUB_APP" in settings
    original_app = copy.deepcopy(settings.get("GITHUB_APP", None))
    original_is_auto = getattr(settings.config, "is_auto_command", None)
    original_final_update = getattr(settings.pr_description, "final_update_message", None)
    original_extra_instructions = {
        section: getattr(getattr(settings, section, None), "extra_instructions", None)
        for section in ("pr_reviewer", "pr_description", "pr_code_suggestions")
    }
    artifact_token = artifacts._artifact_context.set(None)
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
    if original_is_auto is not None:
        settings.config.is_auto_command = original_is_auto
    if original_final_update is not None:
        settings.pr_description.final_update_message = original_final_update
    for section, extra_instructions in original_extra_instructions.items():
        if extra_instructions is not None:
            getattr(settings, section).extra_instructions = extra_instructions
    artifacts._artifact_context.reset(artifact_token)


@pytest.fixture
def restore_artifact_action_settings():
    """Keep B1 ingress settings local to the Action tests that exercise them."""
    settings = get_settings()
    originals = {
        key: copy.deepcopy(settings.get(key, None))
        for key in ("ARTIFACTS", "PR_REVIEWER", "PR_DESCRIPTION", "PR_CODE_SUGGESTIONS")
    }
    original_use_repo_settings = settings.get("CONFIG.USE_REPO_SETTINGS_FILE", None)
    yield settings
    for key, value in originals.items():
        settings.set(key, value, merge=False)
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", original_use_repo_settings)


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
    return _write_issue_comment_event_with_body(tmp_path, "/review", sender_type)


def _write_review_event(
    tmp_path, state="changes_requested", sender_type="User", review_author_type="User", action="submitted"
):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": action,
        "review": {"state": state, "user": {"type": review_author_type}},
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
            "draft": False,
        },
        "sender": {"type": sender_type},
    }))
    return event_path


def _write_review_comment_event_with_body(tmp_path, body):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {
            "body": body,
            "id": 123,
            "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
            "subject_type": "line",
            "start_line": 10,
            "line": 12,
            "diff_hunk": "@@ -1,3 +1,4 @@\n+new line",
            "path": "src/app.py",
            "side": "RIGHT",
        },
        "sender": {"type": "User"},
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


async def _run_review_action_with_settings(
    monkeypatch,
    tmp_path,
    app_review_states,
    app_review_commands,
    action_config=None,
    action="submitted",
    state="changes_requested",
):
    settings = get_settings()
    settings.set("GITHUB_APP.REVIEW_STATES", app_review_states)
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])
    settings.set("GITHUB_APP.REVIEW_COMMANDS", app_review_commands)
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", False)
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", False)
    settings.set("GITHUB_ACTION_CONFIG", dict(action_config or {}), merge=False)

    handled = []
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _pr_url: None)
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: None)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_write_review_event(tmp_path, action=action, state=state)),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()
    return handled


@pytest.mark.asyncio
async def test_review_submission_runs_configured_commands(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_review_event(tmp_path, "CHANGES_REQUESTED")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
            "GITHUB_ACTION_CONFIG.REVIEW_STATES": '["changes_requested"]',
            "GITHUB_ACTION_CONFIG.REVIEW_AUTHOR_TYPES": '["User"]',
            "GITHUB_ACTION_CONFIG.REVIEW_COMMANDS": json.dumps([
                "/review --pr_reviewer.extra_instructions='be concise please'"
            ]),
            "GITHUB_ACTION_CONFIG.FEEDBACK_ON_DRAFT_PR": False,
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)

    await github_action_runner.run_action()

    assert handled == [(
        "https://api.github.com/repos/org/repo/pulls/1",
        ["/review", '--pr_reviewer.extra_instructions="be concise please"'],
    )]


@pytest.mark.asyncio
async def test_review_submission_ignores_unconfigured_state(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_review_event(tmp_path, "approved")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
            "GITHUB_ACTION_CONFIG.REVIEW_STATES": '["changes_requested"]',
            "GITHUB_ACTION_CONFIG.REVIEW_AUTHOR_TYPES": '["User"]',
            "GITHUB_ACTION_CONFIG.REVIEW_COMMANDS": '["/review"]',
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_review_submission_ignores_unconfigured_author_type(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_write_review_event(tmp_path, review_author_type="Bot"))
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
            "GITHUB_ACTION_CONFIG.REVIEW_STATES": '["changes_requested"]',
            "GITHUB_ACTION_CONFIG.REVIEW_AUTHOR_TYPES": '["User"]',
            "GITHUB_ACTION_CONFIG.REVIEW_COMMANDS": '["/review"]',
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
async def test_review_submission_is_disabled_without_review_commands(monkeypatch, tmp_path, restore_github_settings):
    handled = await _run_review_action_with_settings(
        monkeypatch,
        tmp_path,
        app_review_states=["changes_requested"],
        app_review_commands=[],
    )

    assert handled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["edited", "dismissed"])
async def test_review_submission_ignores_non_submitted_actions(
    monkeypatch, tmp_path, restore_github_settings, action
):
    handled = await _run_review_action_with_settings(
        monkeypatch,
        tmp_path,
        app_review_states=["changes_requested"],
        app_review_commands=["/review"],
        action=action,
    )

    assert handled == []


@pytest.mark.asyncio
async def test_action_review_config_falls_back_to_app_and_overrides_it(
    monkeypatch, tmp_path, restore_github_settings
):
    handled = await _run_review_action_with_settings(
        monkeypatch,
        tmp_path,
        app_review_states=["changes_requested"],
        app_review_commands=["/app-review"],
    )
    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", ["/app-review"])]

    handled = await _run_review_action_with_settings(
        monkeypatch,
        tmp_path,
        app_review_states=["approved"],
        app_review_commands=["/app-review"],
        action_config={
            "review_states": ["changes_requested"],
            "review_commands": ["/action-review"],
        },
    )
    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", ["/action-review"])]


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


@pytest.mark.asyncio
async def test_issue_comment_calls_inject_artifact_context(monkeypatch, tmp_path, restore_github_settings):
    """_inject_artifact_context must be called for comment-triggered runs."""
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_issue_comment_event(tmp_path, "User")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    inject_calls = []
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: inject_calls.append(1))

    await github_action_runner.run_action()

    assert inject_calls, "_inject_artifact_context was not called for issue_comment event"


@pytest.mark.asyncio
async def test_action_keeps_the_whole_question_in_a_quote_reply_with_two_asks(
        monkeypatch, tmp_path, restore_github_settings):
    """Apply the #3670 quote-reply fix in the Action too, keeping the text after a second /ask."""
    body = "> ![image][image-1]\n\n/review please\n\n/ask why does /ask appear twice here?"
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_issue_comment_event_with_body(tmp_path, body)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled[0][1].startswith("/ask")
    assert "why does /ask appear twice here?" in handled[0][1]


@pytest.mark.asyncio
async def test_review_comment_with_review_command_mentioning_ask_stays_review(
    monkeypatch, tmp_path, restore_github_settings
):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review_comment")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_write_review_comment_event_with_body(tmp_path, "/review please, I will /ask later")),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [(
        "https://api.github.com/repos/org/repo/pulls/1",
        "/review please, I will /ask later",
    )]


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
    _patch_synchronize_deps(monkeypatch, handled, [
        '/describe --pr_description.extra_instructions="true"', ["/improve", "literal 'text'"]
    ])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", ["/describe", '--pr_description.extra_instructions="true"']),
        ("https://api.github.com/repos/org/repo/pulls/1", ["/improve", "literal 'text'"]),
    ]


@pytest.mark.parametrize("command", ['/review --pr_reviewer.extra_instructions="unfinished', "  "])
def test_invalid_configured_command_fails_action_but_continues(
    monkeypatch, tmp_path, restore_github_settings, command
):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, [command, "/review"])
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    with pytest.raises(SystemExit) as error:
        github_action_runner.main()

    assert error.value.code == 1
    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", ["/review"])]

def test_action_exits_nonzero_when_command_fails_and_continues(
    monkeypatch, tmp_path, restore_github_settings
):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/improve"])

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))
            return body != ["/review"]

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", ["/review"]),
        ("https://api.github.com/repos/org/repo/pulls/1", ["/improve"]),
    ]


# --- Recorded tool failures must fail the Action (issue #3705) ---


def _write_pull_request_opened_event(tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "opened",
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
        },
    }))
    return event_path


def _install_action_env(monkeypatch, tmp_path, event_path, event_name="pull_request"):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")


def _patch_env_settings(monkeypatch, overrides=None):
    real_get_setting_or_env = github_action_runner.get_setting_or_env

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
            "GITHUB_ACTION_CONFIG.PR_ACTIONS": ["opened"],
            "GITHUB_ACTION.AUTO_REVIEW": True,
            "GITHUB_ACTION.AUTO_DESCRIBE": False,
            "GITHUB_ACTION.AUTO_IMPROVE": False,
            "GITHUB_ACTION_CONFIG.FAIL_ON_TOOL_ERRORS": True,
        }
        if overrides:
            values.update(overrides)
        return values.get(key, real_get_setting_or_env(key, default))

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)


def _capture_action_status(monkeypatch):
    """Observe the sticky _ActionStatus that _run_action_and_drain installed."""
    captured = {}
    original_run_action = github_action_runner.run_action

    async def wrapper():
        try:
            await original_run_action()
        finally:
            captured["status"] = github_action_runner._action_status.get()

    monkeypatch.setattr(github_action_runner, "run_action", wrapper)
    return captured


class _FakeAutoTool:
    name = "base"
    runs = []

    def __init__(self, pr_url):
        self.pr_url = pr_url

    async def run(self):
        type(self).runs.append(self.pr_url)
        return None


class _FakeDescription(_FakeAutoTool):
    name = "describe"


class _FakeReviewer(_FakeAutoTool):
    name = "review"


class _FakeSuggestions(_FakeAutoTool):
    name = "improve"


def _install_fake_auto_tools(monkeypatch):
    for tool in (_FakeDescription, _FakeReviewer, _FakeSuggestions):
        tool.runs = []
    monkeypatch.setattr(github_action_runner, "PRDescription", _FakeDescription)
    monkeypatch.setattr(github_action_runner, "PRReviewer", _FakeReviewer)
    monkeypatch.setattr(github_action_runner, "PRCodeSuggestions", _FakeSuggestions)


def test_recorded_auto_tool_failure_fails_action_with_default_gate(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_tools(monkeypatch)

    class FailingTool(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            record_command_failure()
            return None

    monkeypatch.setattr(github_action_runner, "PRReviewer", FailingTool)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert FailingTool.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_recorded_tool_failure_stays_green_when_gate_disabled(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_tools(monkeypatch)

    class FailingTool(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            record_command_failure()
            return None

    monkeypatch.setattr(github_action_runner, "PRReviewer", FailingTool)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch, overrides={"GITHUB_ACTION_CONFIG.FAIL_ON_TOOL_ERRORS": "false"})

    github_action_runner.main()

    assert FailingTool.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_fresh_collector_per_command_and_sticky_status_when_gate_on(monkeypatch, tmp_path, restore_github_settings):
    collectors = []
    handled = []

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            collectors.append(get_run_details())
            handled.append(body)
            if body == ["/review"]:
                record_command_failure()

    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/improve"])
    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_action_env(monkeypatch, tmp_path, _write_synchronize_event(tmp_path))
    captured = _capture_action_status(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert len(collectors) == 2
    assert collectors[0] is not collectors[1]
    assert collectors[0].command_failed is True
    # Judge the clean second command on its own fresh collector...
    assert collectors[1].command_failed is False
    assert command_failed() is False
    # ...and keep the overall Action failure status sticky.
    assert captured["status"].failed is True


def test_fresh_collector_per_command_when_gate_off(monkeypatch, tmp_path, restore_github_settings):
    collectors = []
    handled = []

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            collectors.append(get_run_details())
            handled.append(body)
            if body == ["/review"]:
                record_command_failure()

    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/improve"])
    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    settings = get_settings()
    settings.set("GITHUB_ACTION_CONFIG.FAIL_ON_TOOL_ERRORS", "false")
    _install_action_env(monkeypatch, tmp_path, _write_synchronize_event(tmp_path))

    github_action_runner.main()

    assert len(collectors) == 2
    assert collectors[0] is not collectors[1]
    assert collectors[0].command_failed is True
    # Keep the first command's collector from failing the second command.
    assert collectors[1].command_failed is False
    assert command_failed() is False


def test_recorded_command_failure_fails_action_with_default_gate(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)

    class FailingAgent:
        async def handle_request(self, url, body, notify=None):
            record_command_failure()
            return None

    monkeypatch.setattr(github_action_runner, "PRAgent", FailingAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_action_env(
        monkeypatch, tmp_path, _write_issue_comment_event(tmp_path, "User"), event_name="issue_comment"
    )

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1


def test_comment_argument_cannot_disable_recorded_failure_check(monkeypatch, tmp_path, restore_github_settings):
    ran = []

    class FailingReviewer:
        def __init__(self, _pr_url, ai_handler=None, args=None):
            pass

        async def run(self):
            ran.append(True)
            record_command_failure()

    class FakeProvider:
        def __init__(self, pr_url=None):
            self.pr_url = pr_url

        def add_eyes_reaction(self, comment_id, disable_eyes=False):
            return None

    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "get_git_provider", lambda: FakeProvider)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FailingReviewer)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    event_path = _write_issue_comment_event_with_body(
        tmp_path, "/review --github_action_config.fail_on_tool_errors=false"
    )
    _install_action_env(monkeypatch, tmp_path, event_path, event_name="issue_comment")

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    # Reject the override before the tool runs.
    assert ran == []


def test_command_returning_false_fails_action_even_with_gate_off(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/improve"])

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))
            return body != ["/review"]

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    settings = get_settings()
    settings.set("GITHUB_ACTION_CONFIG.FAIL_ON_TOOL_ERRORS", "false")
    _install_action_env(monkeypatch, tmp_path, _write_synchronize_event(tmp_path))

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", ["/review"]),
        ("https://api.github.com/repos/org/repo/pulls/1", ["/improve"]),
    ]


def test_auto_tool_returning_false_fails_action_even_with_gate_off(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_tools(monkeypatch)

    class FalseTool(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            return False

    monkeypatch.setattr(github_action_runner, "PRReviewer", FalseTool)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch, overrides={"GITHUB_ACTION_CONFIG.FAIL_ON_TOOL_ERRORS": False})

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert FalseTool.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_successful_auto_tool_run_stays_green(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_tools(monkeypatch)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch)

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert _FakeReviewer.runs == ["https://api.github.com/repos/org/repo/pulls/1"]
    assert captured["status"].failed is False


def test_successful_dispatched_command_stays_green(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_action_env(
        monkeypatch, tmp_path, _write_issue_comment_event(tmp_path, "User"), event_name="issue_comment"
    )

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", "/review")]
    assert captured["status"].failed is False


def test_plain_non_command_comment_stays_green(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": "just a remark", "id": 123},
        "issue": {
            "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/1"},
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": "User"},
    }))
    _install_action_env(monkeypatch, tmp_path, event_path, event_name="issue_comment")

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == []
    assert captured["status"].failed is False


def test_bot_comment_event_stays_green(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    _install_action_env(
        monkeypatch, tmp_path, _write_issue_comment_event(tmp_path, "Bot"), event_name="issue_comment"
    )

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == []
    assert captured["status"].failed is False


def test_skipped_unconfigured_pull_request_action_stays_green(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    _install_fake_auto_tools(monkeypatch)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "closed",
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
        },
    }))
    _install_action_env(monkeypatch, tmp_path, event_path)

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert _FakeReviewer.runs == []
    assert captured["status"].failed is False


def test_skipped_unconfigured_review_state_stays_green(monkeypatch, tmp_path, restore_github_settings):
    settings = get_settings()
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", False)
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", False)
    settings.set("GITHUB_ACTION_CONFIG", {"ENABLE_OUTPUT": True}, merge=False)
    handled = []
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _pr_url: None)
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: None)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_action_env(
        monkeypatch, tmp_path, _write_review_event(tmp_path, "approved"), event_name="pull_request_review"
    )

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == []
    assert captured["status"].failed is False


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
async def test_synchronize_event_triggers_push_commands_on_pull_request_target(
    monkeypatch, tmp_path, restore_github_settings
):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe", "/improve"])
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", ["/describe"]),
        ("https://api.github.com/repos/org/repo/pulls/1", ["/improve"]),
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
        ("https://api.github.com/repos/org/repo/pulls/1", ["/describe"]),
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


@pytest.mark.asyncio
async def test_issue_comment_not_starting_with_slash_is_skipped(
    monkeypatch, tmp_path, restore_github_settings
):
    """Regression for #3603: a plain comment must not fail the action run."""
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_write_issue_comment_event_with_body(tmp_path, "thanks!")),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []  # non-command comment skipped; action run stays green

@pytest.mark.asyncio
async def test_issue_comment_on_plain_issue_is_dispatched(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_plain_issue_comment_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [
        (
            "https://api.github.com/repos/org/repo/issues/1",
            "/ask what is this issue about?",
        )
    ]

def _write_workflow_run_event(tmp_path, originating_event="pull_request", pull_requests=None, conclusion="success"):
    if pull_requests is None:
        pull_requests = [{"url": "https://api.github.com/repos/org/repo/pulls/42", "number": 42}]
    workflow_run = {
        "id": 9999,
        "event": originating_event,
        "pull_requests": pull_requests,
    }
    if conclusion is not None:
        workflow_run["conclusion"] = conclusion
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "completed",
        "workflow_run": workflow_run,
    }))
    return event_path


def _patch_workflow_run_deps(monkeypatch, runs):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)

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


@pytest.mark.asyncio
async def test_workflow_run_runs_auto_tools(monkeypatch, tmp_path, restore_github_settings):
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION.AUTO_DESCRIBE": True,
            "GITHUB_ACTION.AUTO_REVIEW": True,
            "GITHUB_ACTION.AUTO_IMPROVE": False,
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)

    await github_action_runner.run_action()

    assert runs == [
        ("describe", "https://api.github.com/repos/org/repo/pulls/42"),
        ("review", "https://api.github.com/repos/org/repo/pulls/42"),
    ]


@pytest.mark.asyncio
async def test_workflow_run_skips_non_pull_request_origin(monkeypatch, tmp_path, restore_github_settings):
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, originating_event="push")))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert runs == []


@pytest.mark.asyncio
async def test_workflow_run_skips_when_pull_requests_empty(monkeypatch, tmp_path, restore_github_settings):
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, pull_requests=[])))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert runs == []


@pytest.mark.asyncio
async def test_workflow_run_injects_ci_conclusion_when_not_success(monkeypatch, tmp_path, restore_github_settings):
    """Verify a failing/cancelled triggering workflow is surfaced to the model.

    Not silently reviewed as if CI were green (see issue #2841).
    """
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, conclusion="failure"))
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert "concluded: failure" in str(get_settings().pr_reviewer.extra_instructions)


@pytest.mark.asyncio
async def test_workflow_run_does_not_inject_ci_conclusion_when_absent(monkeypatch, tmp_path, restore_github_settings):
    """Verify an absent conclusion does not append a 'concluded: None' instruction.

    Covers an older event shape that has no conclusion field at all.
    """
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, conclusion=None))
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert "CI status" not in str(get_settings().pr_reviewer.extra_instructions)


def _write_issue_comment_event_with_body(tmp_path, body, sender_type="User"):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": body, "id": 123},
        "issue": {
            "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/1"},
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": sender_type},
    }))
    return event_path

def _write_plain_issue_comment_event(tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": "/ask what is this issue about?", "id": 123},
        "issue": {
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": "User"},
    }))
    return event_path

@pytest.mark.asyncio
async def test_issue_comment_body_reaches_the_agent_with_its_case_preserved(
        monkeypatch, tmp_path, restore_github_settings):
    """Pass the comment body to the agent unchanged, as the webhook server does, so
    identifiers in an /ask question survive to the model."""
    body = "/ask Why does the JWTValidator reject RS256 tokens from Auth0?"
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_issue_comment_event_with_body(tmp_path, body)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled, "comment was not handled"
    assert handled[0][1] == body


@pytest.mark.asyncio
async def test_action_configured_commands_share_one_artifact_context_after_real_repo_merges(
    monkeypatch, tmp_path, restore_github_settings, restore_artifact_action_settings,
):
    """Verify repeated repository merges share the Action's single artifact read."""
    settings = restore_artifact_action_settings
    artifact = tmp_path / "report.txt"
    artifact.write_text("ACTION_CONFIGURED_ARTIFACT", encoding="utf-8")
    observed = []

    class Provider:
        def get_repo_settings(self):
            return '[pr_reviewer]\nextra_instructions = "Repository instruction"\n'

        def get_files(self):
            return []

    class RecordingReviewer:
        def __init__(self, _pr_url, ai_handler=None, args=None):
            observed.append((
                str(get_settings().pr_reviewer.extra_instructions),
                artifacts.get_artifact_context("pr_reviewer"),
            ))

        async def run(self):
            return None

    settings.set("ARTIFACTS", {
        "enable": True,
        "artifact_path": str(artifact),
        "target_tools": ["pr_reviewer"],
    }, merge=False)
    settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Host instruction")
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)
    settings.set("GITHUB_ACTION_CONFIG", {
        "handle_push_trigger": True,
        "push_commands": ["/review", "/review"],
        "push_trigger_ignore_merge_commits": False,
        "push_trigger_ignore_bot_commits": False,
    }, merge=False)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda _url: Provider())
    monkeypatch.setitem(pr_agent_module.command2class, "review", RecordingReviewer)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    read = Mock(wraps=artifacts._read_and_truncate)
    monkeypatch.setattr(artifacts, "_read_and_truncate", read)

    await github_action_runner.run_action()

    assert len(observed) == 2
    assert all(text.startswith("Repository instruction") for text, _context in observed)
    assert all("ACTION_CONFIGURED_ARTIFACT" not in text for text, _context in observed)
    assert all(
        context["content"].count("ACTION_CONFIGURED_ARTIFACT") == 1
        for _text, context in observed
    )
    read.assert_called_once_with(artifact.resolve(), 50000)


@pytest.mark.asyncio
async def test_direct_action_and_workflow_run_keep_artifact_separate_from_ci_conclusion(
    monkeypatch, tmp_path, restore_github_settings, restore_artifact_action_settings,
):
    settings = restore_artifact_action_settings
    artifact = tmp_path / "report.txt"
    artifact.write_text("ACTION_DIRECT_ARTIFACT", encoding="utf-8")
    observations = []

    class RecordingTool:
        section = "pr_reviewer"

        def __init__(self, _pr_url):
            tool_settings = getattr(settings, self.section)
            observations.append((
                self.section,
                str(tool_settings.extra_instructions),
                artifacts.get_artifact_context(self.section),
            ))

        async def run(self):
            return None

    class Description(RecordingTool):
        section = "pr_description"

    class Reviewer(RecordingTool):
        section = "pr_reviewer"

    class Suggestions(RecordingTool):
        section = "pr_code_suggestions"

    settings.set("ARTIFACTS", {
        "enable": True,
        "artifact_path": str(artifact),
        "target_tools": ["pr_reviewer", "pr_description", "pr_code_suggestions"],
    }, merge=False)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_action_runner, "PRDescription", Description)
    monkeypatch.setattr(github_action_runner, "PRReviewer", Reviewer)
    monkeypatch.setattr(github_action_runner, "PRCodeSuggestions", Suggestions)

    def action_settings(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.PR_ACTIONS": ["opened"],
            "GITHUB_ACTION.AUTO_DESCRIBE": True,
            "GITHUB_ACTION.AUTO_REVIEW": True,
            "GITHUB_ACTION.AUTO_IMPROVE": True,
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", action_settings)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    event = tmp_path / "opened.json"
    event.write_text(json.dumps({"action": "opened", "pull_request": {
        "url": "https://api.github.com/repos/org/repo/pulls/1",
        "html_url": "https://github.com/org/repo/pull/1",
    }}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert {section for section, _text, _context in observations} == {
        "pr_description", "pr_reviewer", "pr_code_suggestions",
    }
    assert all("ACTION_DIRECT_ARTIFACT" not in text for _section, text, _context in observations)
    assert all(
        context["content"].count("ACTION_DIRECT_ARTIFACT") == 1
        for _section, _text, context in observations
    )

    observations.clear()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, conclusion="failure"))
    )

    await github_action_runner.run_action()

    reviewer_text, reviewer_context = next(
        (text, context) for section, text, context in observations if section == "pr_reviewer"
    )
    assert "concluded: failure" in reviewer_text
    assert reviewer_context["content"].count("ACTION_DIRECT_ARTIFACT") == 1


@pytest.mark.asyncio
async def test_non_pr_action_routes_do_not_read_artifacts(
    monkeypatch, tmp_path, restore_github_settings, restore_artifact_action_settings,
):
    artifact = tmp_path / "report.txt"
    artifact.write_text("NON_PR_ARTIFACT", encoding="utf-8")
    settings = restore_artifact_action_settings
    settings.set("ARTIFACTS", {
        "enable": True,
        "artifact_path": str(artifact),
        "target_tools": ["pr_reviewer"],
    }, merge=False)

    def fail_if_read(*_args, **_kwargs):
        pytest.fail("a non-PR Action route must not read an artifact")

    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    monkeypatch.setattr(artifacts, "_read_and_truncate", fail_if_read)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_plain_issue_comment_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [("https://api.github.com/repos/org/repo/issues/1", "/ask what is this issue about?")]

    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, originating_event="push"))
    )
    await github_action_runner.run_action()
