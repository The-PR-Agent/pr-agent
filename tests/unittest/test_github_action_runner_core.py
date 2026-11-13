import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from github import GithubException
from starlette_context import context, request_cycle_context

import pr_agent.agent.pr_agent as pr_agent_module
import pr_agent.servers.github_action_runner as github_action_runner
from pr_agent import config_loader
from pr_agent.agent.request_policy import enforce_request_policy
from pr_agent.algo import artifacts
from pr_agent.algo.run_details import command_failed, get_run_details, record_command_failure
from pr_agent.algo.utils import decode_user_text_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import _GIT_PROVIDERS, get_git_provider_with_context
from pr_agent.git_providers import utils as git_utils
from pr_agent.git_providers.github_provider import GithubProvider, IncompletePullRequestFilesError


@pytest.fixture(autouse=True)
def _provider_key_gate_passes(monkeypatch):
    """Isolate unrelated routing tests from credential/policy I/O.
    Tests of either gate override the corresponding attribute again."""
    monkeypatch.setattr(github_action_runner, "missing_provider_key_env_vars", lambda: None)
    monkeypatch.setattr(github_action_runner, "enforce_request_policy", lambda *_args, **_kwargs: True)


def test_is_true_accepts_bool_and_case_insensitive_true_string():
    assert github_action_runner.is_true(True) is True
    assert github_action_runner.is_true(False) is False
    assert github_action_runner.is_true("TRUE") is True
    assert github_action_runner.is_true("false") is False
    assert github_action_runner.is_true(" on ") is True
    assert github_action_runner.is_true(1) is True
    assert github_action_runner.is_true(0) is False
    assert github_action_runner.is_true(None) is False


@pytest.mark.asyncio
async def test_run_action_returns_when_required_env_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    await github_action_runner.run_action()

    assert "GITHUB_EVENT_NAME not set" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_direct_auto_command_notifies_and_reraises_incomplete_constructor_error(monkeypatch):
    error = IncompletePullRequestFilesError("private repository details")
    notify = Mock()

    class IncompleteCommand:
        def __init__(self, _pr_url):
            raise error

    monkeypatch.setattr(github_action_runner, "publish_incomplete_files_comment", notify)

    with pytest.raises(IncompletePullRequestFilesError) as raised:
        await github_action_runner._run_auto_command(IncompleteCommand, "https://example/pr/1")

    assert raised.value is error
    notify.assert_called_once_with("https://example/pr/1", error)


@pytest.mark.asyncio
async def test_direct_auto_command_leaves_unexpected_constructor_error_unchanged(monkeypatch):
    error = RuntimeError("unrelated")
    notify = Mock()

    class BrokenCommand:
        def __init__(self, _pr_url):
            raise error

    monkeypatch.setattr(github_action_runner, "publish_incomplete_files_comment", notify)

    get_settings().get("config")
    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.RESPONSE_LANGUAGE", "de-DE")
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Keep user instructions")
        with pytest.raises(RuntimeError) as raised:
            await github_action_runner._run_auto_command(BrokenCommand, "https://example/pr/1")
        assert settings.pr_reviewer.extra_instructions == "Keep user instructions"

    assert raised.value is error
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [
    ("false", False),
    ("off", False),
    ("", False),
    ("true", True),
])
async def test_direct_auto_command_normalizes_publish_output(value, expected):
    observed = []

    class AutoCommand:
        def __init__(self, _pr_url):
            pass

        async def run(self):
            observed.append(get_settings().config.publish_output)

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        get_settings().set("CONFIG.PUBLISH_OUTPUT", value)
        await github_action_runner._run_auto_command(AutoCommand, "https://example/pr/1")

    assert observed == [expected]


@pytest.mark.asyncio
async def test_direct_auto_command_publishes_and_preserves_error_when_comment_decode_fails(monkeypatch):
    error = IncompletePullRequestFilesError("private repository details")
    provider = Mock()
    provider.get_issue_comments_newest_first.return_value = [object()]
    provider._get_comment_body.side_effect = RuntimeError("comment decoding failed")
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _url: provider)

    class IncompleteCommand:
        def __init__(self, _pr_url):
            raise error

    with pytest.raises(IncompletePullRequestFilesError) as raised:
        await github_action_runner._run_auto_command(IncompleteCommand, "https://example/pr/1")

    assert raised.value is error
    provider.publish_comment.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_output,expected_output", [(True, True), ("", False), ("  ", False)])
async def test_run_action_invokes_enabled_auto_commands_for_pull_request_event(
    monkeypatch, tmp_path, enable_output, expected_output
):
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
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": enable_output,
        }
        return values.get(key, default)

    monkeypatch.setattr(github_action_runner, "get_setting_or_env", fake_get_setting_or_env)
    runs = []

    class FakeCommand:
        name = "base"

        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            runs.append((self.name, self.pr_url))

    class FakeDescription(FakeCommand):
        name = "describe"

    class FakeReviewer(FakeCommand):
        name = "review"

    monkeypatch.setattr(github_action_runner, "PRDescription", FakeDescription)
    monkeypatch.setattr(github_action_runner, "PRReviewer", FakeReviewer)

    try:
        settings.config.response_language = "en-us"

        await github_action_runner.run_action()

        assert settings.get("GITHUB_ACTION_CONFIG.ENABLE_OUTPUT") is expected_output
        assert runs == [
            ("describe", "https://api.github.com/repos/org/repo/pulls/1"),
            ("review", "https://api.github.com/repos/org/repo/pulls/1"),
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


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_target"])
@pytest.mark.parametrize("include_opened", [True, False])
@pytest.mark.parametrize(
    "created_at,updated_at,expected_batches",
    [
        ("2026-11-04T12:00:00Z", "2026-11-04T12:00:00Z", 1),
        ("2026-11-04T12:00:00Z", "2026-11-04T12:01:00Z", 2),
        (None, None, 2),
        ("2026-11-04T12:00:00Z", None, 2),
        (None, "2026-11-04T12:00:00Z", 2),
        ("", "", 2),
    ],
)
async def test_first_open_review_request_stream(
    monkeypatch, tmp_path, restore_github_settings, event_name, include_opened, created_at, updated_at, expected_batches
):
    pr_url = "https://api.github.com/repos/org/repo/pulls/1"
    pull_request = {"url": pr_url}
    if created_at is not None:
        pull_request["created_at"] = created_at
    if updated_at is not None:
        pull_request["updated_at"] = updated_at
    event_path = tmp_path / "event.json"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "unit-test-token")
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: None)
    monkeypatch.setattr(
        github_action_runner, "get_setting_or_env",
        lambda key, default=None: True if key.startswith("GITHUB_ACTION.AUTO_") else default,
    )
    actions = ["opened", "review_requested"] if include_opened else ["review_requested"]
    get_settings().set("GITHUB_ACTION_CONFIG.PR_ACTIONS", actions)
    run_command = AsyncMock()
    monkeypatch.setattr(github_action_runner, "_run_auto_command", run_command)

    for action in ("opened", "review_requested"):
        event_path.write_text(json.dumps({"action": action, "pull_request": pull_request}))
        await github_action_runner.run_action()

    expected_commands = [
        github_action_runner.PRDescription,
        github_action_runner.PRReviewer,
    ] * (expected_batches if include_opened else 1)
    assert [invocation.args[0] for invocation in run_command.await_args_list] == expected_commands
    assert all(invocation.args[1] == pr_url for invocation in run_command.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", ["pull_request", "workflow_run"])
@pytest.mark.parametrize(
    ("auto_review", "auto_improve", "expected_reviews"),
    [
        (True, True, 1),
        (False, True, 1),
        (True, False, 1),
        (None, None, 1),
        (None, False, 1),
        (True, None, 1),
        (None, True, 1),
        (False, None, 0),
        (False, False, 0),
    ],
)
async def test_action_auto_review_and_auto_improve_run_one_review(
    monkeypatch, tmp_path, restore_github_settings, event_name, auto_review, auto_improve, expected_reviews
):
    pr_url = "https://api.github.com/repos/org/repo/pulls/1"
    event_path = tmp_path / "event.json"
    if event_name == "workflow_run":
        payload = {
            "workflow_run": {"event": "pull_request", "conclusion": "success", "pull_requests": [{"url": pr_url}]}
        }
    else:
        payload = {"action": "opened", "pull_request": {"url": pr_url}}
    event_path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "unit-test-token")
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_action_runner, "_inject_artifact_context", lambda: None)
    flags = {"GITHUB_ACTION.AUTO_REVIEW": auto_review, "GITHUB_ACTION.AUTO_IMPROVE": auto_improve,
             "GITHUB_ACTION.AUTO_DESCRIBE": False}
    monkeypatch.setattr(
        github_action_runner, "get_setting_or_env", lambda key, default=None: flags.get(key, default)
    )
    get_settings().set("GITHUB_ACTION_CONFIG.PR_ACTIONS", ["opened"])
    run_command = AsyncMock()
    monkeypatch.setattr(github_action_runner, "_run_auto_command", run_command)

    await github_action_runner.run_action()

    assert [call.args[0] for call in run_command.await_args_list] == (
        [github_action_runner.PRReviewer] * expected_reviews
    )


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
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "html_url": "https://github.com/org/repo/pull/1",
        },
        "comment": {
            "body": body,
            "id": 123,
            "html_url": "https://github.com/org/repo/pull/1#discussion_r123",
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

    monkeypatch.setattr(github_action_runner, "get_git_provider_with_context", FakeProvider)

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
    disable_auto_feedback=False,
):
    settings = get_settings()
    settings.set("GITHUB_APP.REVIEW_STATES", app_review_states)
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])
    settings.set("GITHUB_APP.REVIEW_COMMANDS", app_review_commands)
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", False)
    settings.set("CONFIG.DISABLE_AUTO_FEEDBACK", disable_auto_feedback)
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
async def test_review_submission_treats_invalid_disable_auto_feedback_as_disabled(
    monkeypatch, tmp_path, restore_github_settings
):
    handled = await _run_review_action_with_settings(
        monkeypatch,
        tmp_path,
        app_review_states=["changes_requested"],
        app_review_commands=["/review"],
        disable_auto_feedback="y",
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
    monkeypatch.setattr(github_action_runner, "get_git_provider_with_context",
                        lambda _url: pytest.fail("line comment should not construct a provider"))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["created", "edited"])
@pytest.mark.parametrize("subject_type,command", [
    ("file", "/ask"), ("line", "/ask"), (None, "/ask"), ("file", "/ask_line"),
    ("file", "/ask_question"), ("file", "/ASK"),
    ("line", "/ask_question"), ("line", "/ASK"), ("line", "/ask_line"), ("line", "/ASK_LINE"),
])
async def test_review_comment_ask_routes_by_subject_type(
    monkeypatch, tmp_path, restore_github_settings, action, subject_type, command
):
    question = "Why does JWTValidator reject RS256 tokens?"
    body = f"{command} {question}"
    event_path = _write_review_comment_event_with_body(tmp_path, body)
    payload = json.loads(event_path.read_text())
    payload["action"] = action
    if subject_type is None:
        payload["comment"].pop("subject_type")
    else:
        payload["comment"]["subject_type"] = subject_type
    if subject_type == "file":
        # GitHub file comments can carry a line=1 placeholder even with subject_type=file.
        line = 1 if command in ("/ask", "/ask_line") else None
        payload["comment"].update(start_line=None, line=line, original_line=line, diff_hunk="")
    event_path.write_text(json.dumps(payload))

    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    _install_action_env(monkeypatch, tmp_path, event_path, "pull_request_review_comment")
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)

    await github_action_runner._run_action_and_drain()

    assert len(handled) == 1
    url, request = handled[0]
    assert url == "https://api.github.com/repos/org/repo/pulls/1"
    if subject_type == "file" and command.lower() in ("/ask", "/ask_question"):
        assert request[0] == "/ask"
        assert decode_user_text_args(request[1:]) == question
    else:
        start, end = (1, 1) if subject_type == "file" else (10, 12)
        assert request[:6] == [
            "/ask_line", f"--line_start={start}", f"--line_end={end}", "--side=RIGHT",
            "--file_name=src/app.py", "--comment_id=123",
        ]
        assert request[6] == question


@pytest.mark.asyncio
@pytest.mark.parametrize("command,file_line", [("/ask", 1), ("/ask_question", None)])
@pytest.mark.parametrize("question,file_path,in_reply_to_id", [
    ('Why is `"` escaped here?', "src/Other File.py", None),
    ("Explain --config.publish_output=false in this code.", "src/Other File.py", None),
    ("--config.publish_output=false", "src/Other File.py", None),
    ("Why this change?", "src/`<!--draft-->: notes.py", None),
    ("Why this change?", "src/line\nbreak.py", None),
    ("Why this change?", r"src/line\nbreak.py", None),
    ("Why this change?", "src/app.py", 456),
])
async def test_file_question_keeps_literal_text_and_path_through_dispatch(
    monkeypatch, tmp_path, restore_github_settings, question, file_path, in_reply_to_id, command, file_line
):
    observed = []

    def question_command(_url, ai_handler=None, args=None):
        observed.append((
            decode_user_text_args(args), get_settings().config.publish_output, context["file_question_path"],
            context["file_question_comment_id"], context["file_question_origin_comment_id"],
            context["file_question_comment_url"],
        ))
        return SimpleNamespace(run=AsyncMock())

    event_path = _write_review_comment_event_with_body(tmp_path, f"{command} {question}")
    payload = json.loads(event_path.read_text())
    payload["comment"].update(
        subject_type="file", path=file_path, start_line=None, line=file_line,
        original_line=file_line, diff_hunk="",
    )
    payload["comment"]["in_reply_to_id"] = in_reply_to_id
    event_path.write_text(json.dumps(payload))
    _patch_issue_comment_deps(monkeypatch, [])
    monkeypatch.setattr(github_action_runner, "PRAgent", pr_agent_module.PRAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: True)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "ask", question_command)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    _install_action_env(monkeypatch, tmp_path, event_path, "pull_request_review_comment")

    await github_action_runner._run_action_and_drain()

    assert observed == [(
        question, True, file_path, in_reply_to_id or 123, 123,
        "https://github.com/org/repo/pull/1#discussion_r123",
    )]


def _patch_synchronize_deps(monkeypatch, handled, push_commands, handle_push_trigger=True, base_branch_merges=None):
    # apply_repo_settings returns the provider it loaded, which knows each PR commit's parents; it tells
    # which (head, previous head) pairs only merge the base branch.
    provider = None if base_branch_merges is None else SimpleNamespace(
        is_base_branch_merge=lambda sha, previous_head: (sha, previous_head) in base_branch_merges
    )
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: provider)
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
    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/describe"])

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
        ("https://api.github.com/repos/org/repo/pulls/1", ["/describe"]),
    ]


# --- Recorded command failures must fail the Action (issue #3705) ---


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


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_target", "workflow_run"])
@pytest.mark.parametrize("missing_model_key", [False, True])
@pytest.mark.parametrize("title,repo_settings,allowed", [
    ("[Auto] Refresh docs", b"", False),
    ("Regular change", b"", True),
    ("[Auto] Refresh docs", b"[config]\nignore_pr_title = []\n", True),
    ("Regular change", b'[config]\nignore_pr_labels = ["skip-review"]\n', False),
])
async def test_auto_actions_apply_policy_after_repo_settings_without_rebuilding_provider(
    monkeypatch, tmp_path, event_name, missing_model_key, title, repo_settings, allowed,
):
    # Keep the Action's real settings/provider path; mock only SDK I/O and commands.
    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    settings = copy.deepcopy(get_settings())
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "false")
    monkeypatch.setattr(config_loader, "global_settings", settings)
    monkeypatch.setattr(github_action_runner, "enforce_request_policy", enforce_request_policy)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setattr(
        github_action_runner, "missing_provider_key_env_vars",
        lambda: ("openai", ("OPENAI_KEY",)) if missing_model_key else None,
    )
    settings.set("config.git_provider", "github")
    settings.set("config.ignore_pr_title", ["^\\[Auto\\]", "^Auto"])
    settings.set("github_action_config.pr_actions", ["opened"])
    pr = SimpleNamespace(
        title=title, html_url="https://github.com/org/repo/pull/1",
        user=SimpleNamespace(login="author"), head=SimpleNamespace(ref="feature", sha="head"),
        base=SimpleNamespace(ref="main"), labels=[SimpleNamespace(name="skip-review")],
        get_commits=Mock(return_value=[SimpleNamespace(sha="head")]),
    )
    repo = Mock(full_name="org/repo", default_branch="main")
    repo.get_pull.return_value = pr
    repo.get_contents.return_value = SimpleNamespace(decoded_content=repo_settings)
    client = Mock()
    client.get_repo.return_value = repo
    monkeypatch.setattr(GithubProvider, "_get_github_client", lambda self: client)
    commands = [Mock(return_value=SimpleNamespace(run=AsyncMock())) for _ in range(2)]
    for name, command in zip(("PRDescription", "PRReviewer"), commands, strict=True):
        monkeypatch.setattr(github_action_runner, name, command)
    event = (_write_workflow_run_event(tmp_path) if event_name == "workflow_run"
             else _write_pull_request_opened_event(tmp_path))
    _install_action_env(monkeypatch, tmp_path, event, event_name)
    _patch_env_settings(monkeypatch, {
        "GITHUB_ACTION.AUTO_DESCRIBE": True, "GITHUB_ACTION.AUTO_REVIEW": True, "GITHUB_ACTION.AUTO_IMPROVE": True,
    })

    # Verify that ignored batches succeed without a model key, while allowed ones still require it.
    if missing_model_key and allowed:
        with pytest.raises(SystemExit, match="no usable API key"):
            await github_action_runner._run_action_and_drain()
    else:
        await github_action_runner._run_action_and_drain()

    for command in commands:
        expected_calls = int(allowed and not missing_model_key)
        assert command.call_count == expected_calls
        assert command.return_value.run.await_count == expected_calls
    # The policy reuses the instance that already loaded .pr_agent.toml; each command that runs re-reads the PR once.
    client.get_repo.assert_called_once_with("org/repo")
    assert repo.get_pull.call_count == 1 + sum(command.call_count for command in commands)
    pr.get_commits.assert_called_once()
    repo.get_contents.assert_called_once()


def _patch_env_settings(monkeypatch, overrides=None):
    real_get_setting_or_env = github_action_runner.get_setting_or_env

    def fake_get_setting_or_env(key, default=None):
        values = {
            "GITHUB_ACTION_CONFIG.ENABLE_OUTPUT": True,
            "GITHUB_ACTION_CONFIG.PR_ACTIONS": ["opened"],
            "GITHUB_ACTION.AUTO_REVIEW": True,
            "GITHUB_ACTION.AUTO_DESCRIBE": False,
            "GITHUB_ACTION.AUTO_IMPROVE": False,
            "GITHUB_ACTION_CONFIG.FAIL_ON_COMMAND_ERRORS": True,
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


class _FakeAutoCommand:
    name = "base"
    runs = []

    def __init__(self, pr_url):
        self.pr_url = pr_url

    async def run(self):
        type(self).runs.append(self.pr_url)
        return None


class _FakeDescription(_FakeAutoCommand):
    name = "describe"


class _FakeReviewer(_FakeAutoCommand):
    name = "review"


def _install_fake_auto_commands(monkeypatch):
    for command in (_FakeDescription, _FakeReviewer):
        command.runs = []
    monkeypatch.setattr(github_action_runner, "PRDescription", _FakeDescription)
    monkeypatch.setattr(github_action_runner, "PRReviewer", _FakeReviewer)


def test_recorded_auto_command_failure_fails_action_with_default_gate(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_commands(monkeypatch)

    class FailingCommand(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            record_command_failure()
            return None

    monkeypatch.setattr(github_action_runner, "PRReviewer", FailingCommand)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert FailingCommand.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_recorded_command_failure_stays_green_when_gate_disabled(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_commands(monkeypatch)

    class FailingCommand(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            record_command_failure()
            return None

    monkeypatch.setattr(github_action_runner, "PRReviewer", FailingCommand)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch, overrides={"GITHUB_ACTION_CONFIG.FAIL_ON_COMMAND_ERRORS": "false"})

    github_action_runner.main()

    assert FailingCommand.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_fresh_collector_per_command_and_sticky_status_when_gate_on(monkeypatch, tmp_path, restore_github_settings):
    collectors = []
    handled = []

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            collectors.append(get_run_details())
            handled.append(body)
            if body == ["/review"]:
                record_command_failure()

    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/describe"])
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

    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/describe"])
    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    settings = get_settings()
    settings.set("GITHUB_ACTION_CONFIG.FAIL_ON_COMMAND_ERRORS", "false")
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
    monkeypatch.setattr(github_action_runner, "get_git_provider_with_context", FakeProvider)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", FailingReviewer)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    event_path = _write_issue_comment_event_with_body(
        tmp_path, "/review --github_action_config.fail_on_command_errors=false"
    )
    _install_action_env(monkeypatch, tmp_path, event_path, event_name="issue_comment")

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    # Reject the override before the command runs.
    assert ran == []


def test_command_returning_false_fails_action_even_with_gate_off(monkeypatch, tmp_path, restore_github_settings):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/review", "/describe"])

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            handled.append((url, body))
            return body != ["/review"]

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    settings = get_settings()
    settings.set("GITHUB_ACTION_CONFIG.FAIL_ON_COMMAND_ERRORS", "false")
    _install_action_env(monkeypatch, tmp_path, _write_synchronize_event(tmp_path))

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert handled == [
        ("https://api.github.com/repos/org/repo/pulls/1", ["/review"]),
        ("https://api.github.com/repos/org/repo/pulls/1", ["/describe"]),
    ]


def test_auto_command_returning_false_fails_action_even_with_gate_off(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_commands(monkeypatch)

    class FalseCommand(_FakeReviewer):
        async def run(self):
            type(self).runs.append(self.pr_url)
            return False

    monkeypatch.setattr(github_action_runner, "PRReviewer", FalseCommand)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch, overrides={"GITHUB_ACTION_CONFIG.FAIL_ON_COMMAND_ERRORS": False})

    with pytest.raises(SystemExit) as exc_info:
        github_action_runner.main()

    assert exc_info.value.code == 1
    assert FalseCommand.runs == ["https://api.github.com/repos/org/repo/pulls/1"]


def test_successful_auto_command_run_stays_green(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_fake_auto_commands(monkeypatch)
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


@pytest.mark.parametrize("github_token", ["token", ""])
def test_plain_non_command_comment_stays_green(monkeypatch, tmp_path, restore_github_settings, github_token):
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
    monkeypatch.setenv("GITHUB_TOKEN", github_token)

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == []
    assert captured["status"].failed is False


@pytest.mark.parametrize("github_token", ["token", ""])
def test_bot_comment_event_stays_green(monkeypatch, tmp_path, restore_github_settings, github_token):
    handled = []
    _patch_issue_comment_deps(monkeypatch, handled)
    _install_action_env(
        monkeypatch, tmp_path, _write_issue_comment_event(tmp_path, "Bot"), event_name="issue_comment"
    )
    monkeypatch.setenv("GITHUB_TOKEN", github_token)

    captured = _capture_action_status(monkeypatch)

    github_action_runner.main()

    assert handled == []
    assert captured["status"].failed is False


def test_skipped_unconfigured_pull_request_action_stays_green(monkeypatch, tmp_path, restore_github_settings):
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda pr_url: None)
    _install_fake_auto_commands(monkeypatch)
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
        ("https://api.github.com/repos/org/repo/pulls/1", ["/review"]),
    ]


@pytest.mark.asyncio
async def test_synchronize_skips_merge_commit(monkeypatch, tmp_path, restore_github_settings):
    # "Update branch" pushes a merge of the base branch onto the previous head; merge_commit_sha is
    # GitHub's test merge of that head into the base, not the pushed commit.
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"], base_branch_merges={("merge123", "abc")})
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], "push_trigger_ignore_merge_commits", True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(
        tmp_path, before_sha="abc", after_sha="merge123", merge_commit_sha="testmerge"
    )))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("before_sha", ["abc", None], ids=["not-a-base-branch-merge", "no-before"])
async def test_synchronize_runs_unless_push_only_merges_base_branch(
    monkeypatch, tmp_path, restore_github_settings, before_sha
):
    # merge123 merges the base branch onto "older", not onto the head this push replaced.
    handled = []
    _patch_synchronize_deps(
        monkeypatch, handled, ["/describe"], base_branch_merges={("merge123", "older"), ("merge123", None)}
    )
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], "push_trigger_ignore_merge_commits", True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(
        tmp_path, before_sha=before_sha, after_sha="merge123", merge_commit_sha="testmerge"
    )))
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    await github_action_runner.run_action()

    assert handled == [("https://api.github.com/repos/org/repo/pulls/1", ["/describe"])]


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
@pytest.mark.parametrize(
    ("setting_name", "event_kwargs"),
    [
        (
            "push_trigger_ignore_merge_commits",
            {"before_sha": "abc", "after_sha": "merge123", "merge_commit_sha": "testmerge"},
        ),
        ("push_trigger_ignore_bot_commits", {"sender_type": "Bot"}),
    ],
    ids=["merge-commit", "bot-commit"],
)
async def test_synchronize_fails_closed_for_invalid_ignore_setting(
    monkeypatch, tmp_path, restore_github_settings, setting_name, event_kwargs
):
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe"], base_branch_merges={("merge123", "abc")})
    settings = get_settings()
    monkeypatch.setitem(settings.store["github_action_config"], setting_name, "y")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_write_synchronize_event(tmp_path, **event_kwargs)),
    )
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
    monkeypatch.setattr(github_action_runner, "get_git_provider_with_context",
                        lambda _url: pytest.fail("plain issue should not construct a provider"))
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

    class FakeCommand:
        name = "base"

        def __init__(self, pr_url):
            self.pr_url = pr_url

        async def run(self):
            runs.append((self.name, self.pr_url))

    class FakeDescription(FakeCommand):
        name = "describe"

    class FakeReviewer(FakeCommand):
        name = "review"

    monkeypatch.setattr(github_action_runner, "PRDescription", FakeDescription)
    monkeypatch.setattr(github_action_runner, "PRReviewer", FakeReviewer)


@pytest.mark.asyncio
async def test_workflow_run_runs_auto_commands(monkeypatch, tmp_path, restore_github_settings):
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
@pytest.mark.parametrize("github_token", ["token", ""])
async def test_workflow_run_skips_non_pull_request_origin(monkeypatch, tmp_path, restore_github_settings, github_token):
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, originating_event="push")))
    monkeypatch.setenv("GITHUB_TOKEN", github_token)

    await github_action_runner.run_action()

    assert runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("github_token", ["token", ""])
async def test_workflow_run_skips_when_pull_requests_empty(
    monkeypatch, tmp_path, restore_github_settings, github_token
):
    runs = []
    _patch_workflow_run_deps(monkeypatch, runs)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_workflow_run_event(tmp_path, pull_requests=[])))
    monkeypatch.setenv("GITHUB_TOKEN", github_token)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name,language", [
    ("issue_comment", "fr-FR"),
    ("issue_comment", "en-us"),
    ("pull_request", "de-DE"),
    ("workflow_run", "de-DE"),
    ("pull_request", 123),
    ("pull_request", ""),
    ("pull_request", "  "),
])
async def test_run_action_scopes_language_after_repo_and_command_settings(monkeypatch, tmp_path, event_name, language):
    observed = []

    class Provider:
        def __init__(self, pr_url=None):
            pass

        def get_repo_settings(self):
            repo_language = "de-DE" if event_name == "issue_comment" else language
            return f"[config]\nresponse_language = {json.dumps(repo_language)}\n"

        def get_files(self):
            return []

        def add_eyes_reaction(self, *_args, **_kwargs):
            pass

    class RecordingReviewer:
        def __init__(self, *_args, **_kwargs):
            observed.append(get_settings().pr_reviewer.extra_instructions)

        async def run(self):
            observed.append(get_settings().pr_reviewer.extra_instructions)

    if event_name == "issue_comment":
        event_path = _write_issue_comment_event_with_body(
            tmp_path, f"/review --config.response_language={language}"
        )
        payload = json.loads(event_path.read_text())
        payload["issue"]["pull_request"]["html_url"] = "https://github.com/org/repo/pull/1"
        event_path.write_text(json.dumps(payload))
    elif event_name == "workflow_run":
        event_path = _write_workflow_run_event(tmp_path, conclusion=None)
    else:
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps({
            "action": "opened",
            "pull_request": {
                "url": "https://api.github.com/repos/org/repo/pulls/1",
                "html_url": "https://github.com/org/repo/pull/1",
            },
        }))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda _url: Provider())
    monkeypatch.setattr(github_action_runner, "get_git_provider_with_context", lambda _url: Provider())
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "flush_telemetry", lambda: None)
    monkeypatch.setitem(pr_agent_module.command2class, "review", RecordingReviewer)
    monkeypatch.setattr(github_action_runner, "PRReviewer", RecordingReviewer)
    _patch_env_settings(monkeypatch)

    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)
        settings.set("CONFIG.RESPONSE_LANGUAGE", "en-us")
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Keep user instructions")
        await github_action_runner.run_action()
        assert settings.pr_reviewer.extra_instructions == "Keep user instructions"

    assert len(observed) == 2
    assert observed[0] == observed[1]
    if language == "en-us" or not isinstance(language, str) or not language.strip():
        assert observed[0] == "Keep user instructions"
    else:
        assert observed[0].startswith("Keep user instructions")
        assert observed[0].count("locale code:") == 1
        assert f"'{language}'" in observed[0]


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

@pytest.mark.parametrize("github_token", [None, ""], ids=["missing", "empty"])
@pytest.mark.parametrize("event_name, write_event", [
    ("pull_request", _write_pull_request_opened_event),
    ("pull_request_target", _write_pull_request_opened_event),
    ("workflow_run", _write_workflow_run_event),
    ("pull_request_review", _write_review_event),
    ("issue_comment", lambda path: _write_issue_comment_event(path, "User")),
    ("issue_comment", _write_plain_issue_comment_event),
    ("pull_request_review_comment", lambda path: _write_review_comment_event_with_body(path, "/review")),
])
def test_missing_github_token_fails_before_repository_access(
    monkeypatch, tmp_path, restore_github_settings, github_token, event_name, write_event,
):
    _install_action_env(monkeypatch, tmp_path, write_event(tmp_path), event_name)
    if github_token is None:
        monkeypatch.delenv("GITHUB_TOKEN")
    else:
        monkeypatch.setenv("GITHUB_TOKEN", github_token)
    apply_settings = Mock()
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", apply_settings)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)

    with pytest.raises(SystemExit, match="GITHUB_TOKEN not set.*workflow's env"):
        github_action_runner.main()

    apply_settings.assert_not_called()


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
        "target_commands": ["pr_reviewer"],
    }, merge=False)
    settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "Host instruction")
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)
    settings.set("GITHUB_ACTION_CONFIG", {
        "handle_push_trigger": True,
        "push_commands": ["/review", "/describe"],
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
    monkeypatch.setitem(pr_agent_module.command2class, "describe", RecordingReviewer)
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
    read.assert_called_once_with(artifact.resolve(), 50000, "start")


@pytest.mark.asyncio
async def test_direct_action_and_workflow_run_keep_artifact_separate_from_ci_conclusion(
    monkeypatch, tmp_path, restore_github_settings, restore_artifact_action_settings,
):
    settings = restore_artifact_action_settings
    artifact = tmp_path / "report.txt"
    artifact.write_text("ACTION_DIRECT_ARTIFACT", encoding="utf-8")
    observations = []

    class RecordingCommand:
        section = "pr_reviewer"

        def __init__(self, _pr_url):
            command_settings = getattr(settings, self.section)
            observations.append((
                self.section,
                str(command_settings.extra_instructions),
                artifacts.get_artifact_context(self.section),
            ))

        async def run(self):
            return None

    class Description(RecordingCommand):
        section = "pr_description"

    class Reviewer(RecordingCommand):
        section = "pr_reviewer"

    settings.set("ARTIFACTS", {
        "enable": True,
        "artifact_path": str(artifact),
        "target_commands": ["pr_reviewer", "pr_description", "pr_code_suggestions"],
    }, merge=False)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_action_runner, "PRDescription", Description)
    monkeypatch.setattr(github_action_runner, "PRReviewer", Reviewer)

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

    assert {section for section, _text, _context in observations} == {"pr_description", "pr_reviewer"}
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
        "target_commands": ["pr_reviewer"],
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


class _ScopeProbeProvider:
    """Stand in for the GitHub provider and record how one Action run reuses it."""

    created = []
    failing_refresh = None

    def __init__(self, pr_url):
        self.diff_resets = 0
        self.pr_refreshes = 0
        self.repo_settings_reads = 0
        self.reactions = []
        type(self).created.append(self)

    def get_repo_settings(self):
        self.repo_settings_reads += 1
        return ""

    def reset_diff_cache_for_command(self):
        self.diff_resets += 1

    def refresh_pr_for_command(self):
        self.pr_refreshes += 1
        if self.pr_refreshes == type(self).failing_refresh:
            raise GithubException(502, {"message": "Bad Gateway"}, {})

    def add_eyes_reaction(self, comment_id, disable_eyes=False):
        self.reactions.append((comment_id, disable_eyes))


def _use_scope_probe_provider(monkeypatch):
    # Other tests leave a different config.git_provider on the shared settings; pin this test's own copy.
    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    settings = copy.deepcopy(get_settings())
    monkeypatch.setattr(config_loader, "global_settings", settings)
    settings.set("config.git_provider", "github")
    _ScopeProbeProvider.created = []
    monkeypatch.setitem(_GIT_PROVIDERS, "github", _ScopeProbeProvider)
    return _ScopeProbeProvider.created


def _stub_auto_commands(monkeypatch, tmp_path):
    """Set up an opened-PR run whose three commands record what the shared probe provider showed them."""
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    _install_action_env(monkeypatch, tmp_path, _write_pull_request_opened_event(tmp_path))
    _patch_env_settings(monkeypatch, {
        "GITHUB_ACTION.AUTO_DESCRIBE": True, "GITHUB_ACTION.AUTO_REVIEW": True, "GITHUB_ACTION.AUTO_IMPROVE": True,
    })
    created = _use_scope_probe_provider(monkeypatch)
    runs = []

    def make_command(name):
        class ProviderCommand:
            def __init__(self, pr_url):
                self.provider = get_git_provider_with_context(pr_url)

            async def run(self):
                runs.append((name, self.provider.diff_resets, self.provider.pr_refreshes))

        return ProviderCommand

    monkeypatch.setattr(github_action_runner, "PRDescription", make_command("describe"))
    monkeypatch.setattr(github_action_runner, "PRReviewer", make_command("review"))
    return created, runs


def _stub_push_commands(monkeypatch, tmp_path):
    """Set up a synchronize run of three push commands that record what the shared probe provider showed them."""
    handled = []
    _patch_synchronize_deps(monkeypatch, handled, ["/describe", "/review", "/describe"])
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(_write_synchronize_event(tmp_path)))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    # The real settings step builds the provider the commands then reuse, so even the first refresh is checked.
    monkeypatch.setattr(github_action_runner, "apply_repo_settings", git_utils.apply_repo_settings)
    created = _use_scope_probe_provider(monkeypatch)

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            provider = get_git_provider_with_context(url)
            handled.append((body[0], provider.diff_resets, provider.pr_refreshes))

    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)
    return created, handled


def test_auto_commands_share_one_provider_and_refresh_it_before_each_command(
    monkeypatch, tmp_path, restore_github_settings
):
    created, runs = _stub_auto_commands(monkeypatch, tmp_path)

    github_action_runner.main()

    # The repo-settings step builds the provider every command then reuses; each command resets its diff and re-reads
    # the PR first.
    assert len(created) == 1
    assert runs == [("describe", 1, 1), ("review", 2, 2)]


def test_auto_commands_end_the_run_when_the_pull_request_cannot_be_read_before_a_command(
    monkeypatch, tmp_path, restore_github_settings
):
    _, runs = _stub_auto_commands(monkeypatch, tmp_path)
    monkeypatch.setattr(_ScopeProbeProvider, "failing_refresh", 2)

    with pytest.raises(GithubException):
        github_action_runner.main()

    # As on main, where a command whose provider cannot read the PR does not run, the commands after it do not either.
    assert runs == [("describe", 1, 1)]


def test_push_commands_share_one_provider_and_refresh_it_before_each_command(
    monkeypatch, tmp_path, restore_github_settings
):
    created, handled = _stub_push_commands(monkeypatch, tmp_path)

    github_action_runner.main()

    assert len(created) == 1
    assert handled == [("/describe", 1, 1), ("/review", 2, 2), ("/describe", 3, 3)]


def test_push_commands_skip_a_command_whose_pull_request_cannot_be_read(
    monkeypatch, tmp_path, restore_github_settings
):
    _, handled = _stub_push_commands(monkeypatch, tmp_path)
    monkeypatch.setattr(_ScopeProbeProvider, "failing_refresh", 2)

    with pytest.raises(SystemExit) as exit_info:
        github_action_runner.main()

    # The command fails the run without being run, and the next one reads the PR for itself.
    assert exit_info.value.code == 1
    assert handled == [("/describe", 1, 1), ("/describe", 3, 3)]


@pytest.mark.asyncio
async def test_comment_command_builds_one_provider_for_settings_reaction_and_command(
    monkeypatch, tmp_path, restore_github_settings
):
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({
        "action": "created",
        "comment": {"body": "/review", "id": 123},
        "issue": {
            "pull_request": {
                "url": "https://api.github.com/repos/org/repo/pulls/1",
                "html_url": "https://github.com/org/repo/pull/1",
            },
            "url": "https://api.github.com/repos/org/repo/issues/1",
        },
        "sender": {"type": "User"},
    }))
    _install_action_env(monkeypatch, tmp_path, event_path, "issue_comment")
    created = _use_scope_probe_provider(monkeypatch)
    real_apply_repo_settings = github_action_runner.apply_repo_settings
    applied = []

    def apply_repo_settings(pr_url):
        applied.append(real_apply_repo_settings(pr_url))
        return applied[-1]

    class FakeAgent:
        async def handle_request(self, url, body, notify=None):
            apply_repo_settings(url)  # PRAgent applies the repo settings again before it runs a command
            notify()  # and acknowledges the comment once it accepts the command

    monkeypatch.setattr(github_action_runner, "apply_repo_settings", apply_repo_settings)
    monkeypatch.setattr(github_action_runner, "PRAgent", FakeAgent)

    await github_action_runner._run_action_and_drain()

    assert len(created) == 1
    assert len(applied) == 2
    assert applied[0] is applied[1] is created[0]
    assert created[0].repo_settings_reads == 1
    assert created[0].reactions == [(123, False)]


@pytest.mark.parametrize("event_name,payload,expected", [
    ("pull_request", {"pull_request": {"url": "api-url", "html_url": "html-url"}}, "api-url"),
    ("pull_request", {"pull_request": {"html_url": "html-url"}}, "html-url"),
    ("issue_comment", {"issue": {"pull_request": {"url": "api-url", "html_url": "html-url"}}}, "api-url"),
    ("workflow_run", {"workflow_run": {"event": "pull_request", "pull_requests": [{"url": "api-url"}]}}, "api-url"),
])
def test_repo_settings_load_from_the_url_the_commands_use(event_name, payload, expected):
    assert github_action_runner._settings_pr_url(payload, event_name) == expected


@pytest.mark.asyncio
async def test_callback_drain_reads_the_settings_the_run_applied(monkeypatch):
    # Repository settings are applied to the run's settings; the teardown must read the same ones.
    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    settings = copy.deepcopy(get_settings())
    monkeypatch.setattr(config_loader, "global_settings", settings)
    drain = AsyncMock()
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: True)
    monkeypatch.setattr(github_action_runner, "drain_litellm_callbacks", drain)

    async def run_action():
        get_settings().set("litellm.callback_timeout_seconds", 7)

    monkeypatch.setattr(github_action_runner, "run_action", run_action)

    await github_action_runner._run_action_and_drain()

    drain.assert_awaited_once_with(7)
    assert settings.litellm.get("callback_timeout_seconds") != 7


@pytest.mark.asyncio
async def test_trusted_host_settings_reach_the_settings_the_run_is_copied_from(monkeypatch):
    # The options LiteLLM reads from the process-wide settings must still see the trusted host layer,
    # and the run's own apply_repo_settings() must reuse the payload instead of fetching it again.
    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    settings = copy.deepcopy(get_settings())
    monkeypatch.setattr(config_loader, "global_settings", settings)
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "false")  # apply_host_settings sets it; restore it afterwards
    host_url = "https://config.example.test/host.toml"
    settings.set("config.extra_config_url", host_url)
    fetches = []

    def urlopen(request, timeout):
        fetches.append(request.full_url)
        return io.BytesIO(b'[litellm]\nsuccess_callback = ["langsmith"]\n')

    monkeypatch.setattr(git_utils, "urlopen", urlopen)
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    seen = []

    async def run_action():
        git_utils.apply_host_settings()  # apply_repo_settings() applies the host layer first
        seen.append(get_settings().litellm.get("success_callback"))

    monkeypatch.setattr(github_action_runner, "run_action", run_action)

    await github_action_runner._run_action_and_drain()

    assert seen == [["langsmith"]]
    assert settings.litellm.get("success_callback") == ["langsmith"]
    assert fetches == [host_url]


@pytest.mark.asyncio
async def test_unusable_host_settings_do_not_abort_the_run(monkeypatch):
    get_settings().get("config")  # Initialize Dynaconf before copying its loaded sections.
    settings = copy.deepcopy(get_settings())
    monkeypatch.setattr(config_loader, "global_settings", settings)
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "false")  # apply_host_settings sets it; restore it afterwards
    settings.set("config.extra_config_url", "http://[invalid")  # urlparse() raises a ValueError for it
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)
    ran = []

    async def run_action():
        ran.append(True)

    monkeypatch.setattr(github_action_runner, "run_action", run_action)

    await github_action_runner._run_action_and_drain()

    assert ran == [True]


@pytest.mark.asyncio
async def test_failed_run_resets_the_request_scope(monkeypatch):
    monkeypatch.setattr(github_action_runner, "litellm_callbacks_registered", lambda: False)

    async def run_action():
        raise RuntimeError("boom")

    monkeypatch.setattr(github_action_runner, "run_action", run_action)
    assert not context.exists()

    with pytest.raises(RuntimeError, match="boom"):
        await github_action_runner._run_action_and_drain()

    assert not context.exists()
