import copy
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import bitbucket_server_webhook, gitea_app, github_app


@pytest.fixture
def gitlab_webhook_module():
    settings = get_settings()
    original_git_provider = settings.config.get("git_provider", None)
    had_gitlab_settings = "GITLAB" in settings
    original_gitlab_settings = copy.deepcopy(settings.get("GITLAB", None))
    settings.set("GITLAB.URL", "https://gitlab.com")
    try:
        module = importlib.import_module("pr_agent.servers.gitlab_webhook")
        yield module
    finally:
        settings.config.git_provider = original_git_provider
        if had_gitlab_settings:
            settings.set("GITLAB", original_gitlab_settings)
        else:
            settings.unset("GITLAB", force=True)


def _bitbucket_server_payload(**overrides):
    payload = {
        "pullRequest": {
            "id": 7,
            "title": "Regular PR",
            "fromRef": {"displayId": "feature/cache"},
            "toRef": {
                "displayId": "main",
                "repository": {
                    "slug": "repo",
                    "project": {"key": "PROJ"},
                },
            },
            "author": {"user": {"name": "alice"}},
        }
    }
    payload["pullRequest"].update(overrides)
    return payload


class _StubRequest:
    """Minimal stand-in for a starlette Request, exposing only what handle_webhook reads."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.headers = {}

    async def json(self):
        return self._payload

    async def body(self):
        return json.dumps(self._payload).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_key", ["pr:from_ref_updated", "repo:refs_changed"])
@pytest.mark.parametrize("proceed", [True, False])
async def test_bitbucket_server_handle_webhook_accepts_push_trigger_event_keys(event_key, proceed, monkeypatch):
    # Regression test: "pr:from_ref_updated" used to be excluded from this branch and
    # fell through to the "Unsupported event" 400 response instead of running push commands.
    settings = get_settings()
    original_webhook_secret = settings.get("BITBUCKET_SERVER.WEBHOOK_SECRET", None)
    original_handle_push_trigger = settings.get("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER", None)
    original_url = settings.get("BITBUCKET_SERVER.URL", None)
    settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", None)
    settings.set("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER", True)
    settings.set("BITBUCKET_SERVER.URL", "https://bitbucket.example.com")

    monkeypatch.setattr(bitbucket_server_webhook, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(bitbucket_server_webhook, "should_process_pr_logic", lambda data: True)
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "_get_commands_list_from_settings",
        lambda key: ["/review"] if key == "BITBUCKET_SERVER.PUSH_COMMANDS" else [],
    )
    slots = []
    commands = []

    @asynccontextmanager
    async def record_slot(key, **kwargs):
        slots.append((key, kwargs))
        yield proceed

    async def record_commands(commands_to_run, url, _log_context):
        commands.append((commands_to_run, url))

    monkeypatch.setattr(bitbucket_server_webhook, "push_trigger_slot", record_slot)
    monkeypatch.setattr(bitbucket_server_webhook, "_run_commands_sequentially", record_commands)

    payload = _bitbucket_server_payload()
    payload["eventKey"] = event_key
    request = _StubRequest(payload)
    background_tasks = BackgroundTasks()

    try:
        with request_cycle_context({}):
            response = await bitbucket_server_webhook.handle_webhook(background_tasks, request)
            await background_tasks()
    finally:
        settings.set("BITBUCKET_SERVER.WEBHOOK_SECRET", original_webhook_secret)
        settings.set("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER", original_handle_push_trigger)
        settings.set("BITBUCKET_SERVER.URL", original_url)

    assert response.status_code == 200
    assert json.loads(response.body)["message"] == "success"
    assert len(background_tasks.tasks) == 1
    expected_url = "https://bitbucket.example.com/projects/PROJ/repos/repo/pull-requests/7"
    assert slots == [(expected_url, {"allow_backlog": True, "ttl": 300})]
    assert commands == ([(["/review"], expected_url)] if proceed else [])


def _gitlab_payload(**object_attributes):
    return {
        "object_attributes": {
            "title": "Regular MR",
            "source_branch": "feature/cache",
            "target_branch": "main",
            "labels": [],
            **object_attributes,
        },
        "project": {"path_with_namespace": "org/repo"},
        "user": {"username": "alice", "name": "Alice"},
    }


async def _post_gitlab_webhook(app, data):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/webhook", headers={"X-Gitlab-Token": "secret-id"}, json=data
        )


def test_bitbucket_server_should_process_pr_logic_ignores_author_title_and_branch():
    settings = get_settings()
    original = {
        "ignore_repositories": settings.get("CONFIG.IGNORE_REPOSITORIES", []),
        "ignore_pr_authors": settings.get("CONFIG.IGNORE_PR_AUTHORS", []),
        "ignore_pr_title": settings.get("CONFIG.IGNORE_PR_TITLE", []),
        "ignore_pr_source_branches": settings.get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", []),
        "ignore_pr_target_branches": settings.get("CONFIG.IGNORE_PR_TARGET_BRANCHES", []),
    }
    settings.set("CONFIG.IGNORE_REPOSITORIES", [])
    settings.set("CONFIG.IGNORE_PR_AUTHORS", ["dependabot"])
    settings.set("CONFIG.IGNORE_PR_TITLE", ["^WIP"])
    settings.set("CONFIG.IGNORE_PR_SOURCE_BRANCHES", ["^generated/"])
    settings.set("CONFIG.IGNORE_PR_TARGET_BRANCHES", ["^legacy$"])

    try:
        assert bitbucket_server_webhook.should_process_pr_logic(
            _bitbucket_server_payload(author={"user": {"name": "dependabot"}})
        ) is False
        assert bitbucket_server_webhook.should_process_pr_logic(
            _bitbucket_server_payload(title="WIP: generated docs")
        ) is False
        assert bitbucket_server_webhook.should_process_pr_logic(
            _bitbucket_server_payload(fromRef={"displayId": "generated/api"})
        ) is False
        assert bitbucket_server_webhook.should_process_pr_logic(
            _bitbucket_server_payload(toRef={
                "displayId": "legacy",
                "repository": {"slug": "repo", "project": {"key": "PROJ"}},
            })
        ) is False
    finally:
        settings.set("CONFIG.IGNORE_REPOSITORIES", original["ignore_repositories"])
        settings.set("CONFIG.IGNORE_PR_AUTHORS", original["ignore_pr_authors"])
        settings.set("CONFIG.IGNORE_PR_TITLE", original["ignore_pr_title"])
        settings.set("CONFIG.IGNORE_PR_SOURCE_BRANCHES", original["ignore_pr_source_branches"])
        settings.set("CONFIG.IGNORE_PR_TARGET_BRANCHES", original["ignore_pr_target_branches"])


def test_bitbucket_server_process_command_applies_repo_settings_before_preparing_command(monkeypatch):
    calls = []

    monkeypatch.setattr(bitbucket_server_webhook, "apply_repo_settings", lambda url: calls.append(("repo", url)))
    prepared = []
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "prepare_command",
        lambda command: prepared.append(command) or ["/review"],
    )

    command = bitbucket_server_webhook._process_command(
        "/review --config.temperature=0 --pr_reviewer.extra_instructions=test",
        "https://example/pr/1",
    )

    assert calls == [("repo", "https://example/pr/1")]
    assert prepared == ["/review --config.temperature=0 --pr_reviewer.extra_instructions=test"]
    assert command == ["/review"]


def test_bitbucket_server_to_list_rejects_non_list_strings():
    with pytest.raises(ValueError, match="Invalid command string"):
        bitbucket_server_webhook._to_list("{'/review': true}")


def test_gitlab_should_process_pr_logic_ignores_labels_and_branches(gitlab_webhook_module):
    settings = get_settings()
    original = {
        "ignore_repositories": settings.get("CONFIG.IGNORE_REPOSITORIES", []),
        "ignore_pr_authors": settings.get("CONFIG.IGNORE_PR_AUTHORS", []),
        "ignore_pr_title": settings.get("CONFIG.IGNORE_PR_TITLE", []),
        "ignore_pr_labels": settings.get("CONFIG.IGNORE_PR_LABELS", []),
        "ignore_pr_source_branches": settings.get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", []),
        "ignore_pr_target_branches": settings.get("CONFIG.IGNORE_PR_TARGET_BRANCHES", []),
    }
    settings.set("CONFIG.IGNORE_REPOSITORIES", [])
    settings.set("CONFIG.IGNORE_PR_AUTHORS", [])
    settings.set("CONFIG.IGNORE_PR_TITLE", [])
    settings.set("CONFIG.IGNORE_PR_LABELS", ["skip-pr-agent"])
    settings.set("CONFIG.IGNORE_PR_SOURCE_BRANCHES", ["^generated/"])
    settings.set("CONFIG.IGNORE_PR_TARGET_BRANCHES", ["^legacy$"])

    try:
        assert gitlab_webhook_module.should_process_pr_logic(
            _gitlab_payload(labels=[{"title": "skip-pr-agent"}])
        ) is False
        assert gitlab_webhook_module.should_process_pr_logic(
            _gitlab_payload(source_branch="generated/api")
        ) is False
        assert gitlab_webhook_module.should_process_pr_logic(
            _gitlab_payload(target_branch="legacy")
        ) is False

        settings.set("CONFIG.IGNORE_PR_TITLE", ["^Auto:"])
        for nullable_field in ("source_branch", "target_branch", "labels"):
            assert gitlab_webhook_module.should_process_pr_logic(
                _gitlab_payload(title="Auto: bump deps", **{nullable_field: None})
            ) is False
    finally:
        settings.set("CONFIG.IGNORE_REPOSITORIES", original["ignore_repositories"])
        settings.set("CONFIG.IGNORE_PR_AUTHORS", original["ignore_pr_authors"])
        settings.set("CONFIG.IGNORE_PR_TITLE", original["ignore_pr_title"])
        settings.set("CONFIG.IGNORE_PR_LABELS", original["ignore_pr_labels"])
        settings.set("CONFIG.IGNORE_PR_SOURCE_BRANCHES", original["ignore_pr_source_branches"])
        settings.set("CONFIG.IGNORE_PR_TARGET_BRANCHES", original["ignore_pr_target_branches"])


def test_gitlab_is_draft_ready_accepts_string_booleans(gitlab_webhook_module):
    data = {
        "changes": {
            "draft": {
                "previous": "true",
                "current": "false",
            }
        }
    }

    assert gitlab_webhook_module.is_draft_ready(data) is True


class RecordingAgent:
    def __init__(self):
        self.commands = []

    async def handle_request(self, _url, command, notify=None):
        self.commands.append(command)


async def _run_github_pr_commands(
    monkeypatch, repo_setting, action="opened", draft=True, configured_commands=None
):
    # draft=None omits the field from the payload.
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.HANDLE_PR_ACTIONS", ["opened", "reopened", "ready_for_review"])
    settings.set(
        "GITHUB_APP.PR_COMMANDS",
        configured_commands if configured_commands is not None else ["/review"],
    )
    # Prove the repo setting, not the global default, decides.
    settings.set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", not repo_setting)

    repo_settings_calls = 0

    def apply_repo_settings(_):
        nonlocal repo_settings_calls
        repo_settings_calls += 1
        get_settings().set("GITHUB_APP.FEEDBACK_ON_DRAFT_PR", repo_setting)

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", apply_repo_settings)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    identity_provider = SimpleNamespace(
        verify_eligibility=lambda *args, **kwargs: Eligibility.ELIGIBLE
    )
    monkeypatch.setattr(
        github_app, "get_identity_provider", lambda: identity_provider
    )
    try:
        await github_app.handle_request(
            {
                "action": action,
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    **({} if draft is None else {"draft": draft}),
                },
                "sender": {"login": "alice", "id": 1, "type": "User"},
                "repository": {"full_name": "org/repo"},
            },
            "pull_request",
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)
    return agent.commands, repo_settings_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "draft", "feedback_on_draft_pr", "expected_commands"),
    [
        ("opened", True, False, []),
        # A missing draft field defaults to draft and is rejected.
        ("opened", None, False, []),
        ("opened", True, True, [["/review"]]),
        ("ready_for_review", False, False, [["/review"]]),
        ("ready_for_review", False, True, []),
    ],
)
async def test_github_automatic_feedback_follows_draft_setting(
    monkeypatch, action, draft, feedback_on_draft_pr, expected_commands
):
    commands, repo_settings_calls = await _run_github_pr_commands(
        monkeypatch, feedback_on_draft_pr, action, draft
    )

    assert commands == expected_commands
    assert repo_settings_calls == 1


@pytest.mark.asyncio
async def test_github_automatic_feedback_preserves_quoted_command_arguments(monkeypatch):
    commands, repo_settings_calls = await _run_github_pr_commands(
        monkeypatch,
        repo_setting=True,
        configured_commands=['/ask "why is this change risky?"'],
    )

    assert commands == [["/ask", "why is this change risky?"]]
    assert repo_settings_calls == 1


def _github_review_event(action="submitted", state="changes_requested", review_author_type="User"):
    return {
        "action": action,
        "review": {"state": state, "user": {"type": review_author_type}},
        "pull_request": {
            "url": "https://api.github.com/repos/org/repo/pulls/1",
            "state": "open",
            "draft": False,
            "title": "Regular PR",
            "labels": [],
            "head": {"ref": "feature/cache"},
            "base": {"ref": "main"},
        },
        "sender": {"login": "alice", "id": 1, "type": "User"},
        "repository": {"full_name": "org/repo"},
    }


def test_matches_review_state_is_case_insensitive_and_handles_empty_values():
    assert github_app.matches_review_state(" CHANGES_REQUESTED ", ["changes_requested"])
    assert github_app.matches_review_state("approved", "approved")
    assert not github_app.matches_review_state("approved", [])
    assert not github_app.matches_review_state("", ["approved"])


@pytest.mark.asyncio
async def test_github_review_submission_is_disabled_without_review_commands(monkeypatch):
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.REVIEW_COMMANDS", [])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        github_app,
        "get_identity_provider",
        lambda: SimpleNamespace(verify_eligibility=lambda *args, **kwargs: Eligibility.ELIGIBLE),
    )

    try:
        await github_app.handle_request(_github_review_event(), "pull_request_review")
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["edited", "dismissed"])
async def test_github_review_submission_ignores_non_submitted_actions(monkeypatch, action):
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _body: True)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)

    try:
        await github_app.handle_request(_github_review_event(action=action), "pull_request_review")
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == []


@pytest.mark.asyncio
async def test_github_review_submission_runs_configured_commands(monkeypatch):
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        github_app,
        "get_identity_provider",
        lambda: SimpleNamespace(verify_eligibility=lambda *args, **kwargs: Eligibility.ELIGIBLE),
    )

    try:
        await github_app.handle_request(
            {
                "action": "submitted",
                "review": {
                    "state": "changes_requested",
                    "body": "Please fix the validation.",
                    "user": {"type": "User"},
                },
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    "draft": False,
                    "title": "Regular PR",
                    "labels": [],
                    "head": {"ref": "feature/cache"},
                    "base": {"ref": "main"},
                },
                "sender": {"login": "alice", "id": 1, "type": "User"},
                "repository": {"full_name": "org/repo"},
            },
            "pull_request_review",
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == [["/review"]]


@pytest.mark.asyncio
async def test_github_review_submission_ignores_unconfigured_state(monkeypatch):
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        github_app,
        "get_identity_provider",
        lambda: SimpleNamespace(verify_eligibility=lambda *args, **kwargs: Eligibility.ELIGIBLE),
    )

    try:
        await github_app.handle_request(
            {
                "action": "submitted",
                "review": {"state": "approved", "user": {"type": "User"}},
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    "draft": False,
                    "title": "Regular PR",
                    "labels": [],
                    "head": {"ref": "feature/cache"},
                    "base": {"ref": "main"},
                },
                "sender": {"login": "alice", "id": 1, "type": "User"},
                "repository": {"full_name": "org/repo"},
            },
            "pull_request_review",
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == []


@pytest.mark.asyncio
async def test_github_review_submission_ignores_unconfigured_author_type(monkeypatch):
    settings = get_settings()
    original_github_app = copy.deepcopy(settings.get("GITHUB_APP"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITHUB_APP.REVIEW_COMMANDS", ["/review"])
    settings.set("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    settings.set("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])

    agent = RecordingAgent()
    monkeypatch.setattr(github_app, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(github_app, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        github_app,
        "get_identity_provider",
        lambda: SimpleNamespace(verify_eligibility=lambda *args, **kwargs: Eligibility.ELIGIBLE),
    )

    try:
        await github_app.handle_request(
            {
                "action": "submitted",
                "review": {"state": "changes_requested", "user": {"type": "Bot"}},
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1",
                    "state": "open",
                    "draft": False,
                    "title": "Regular PR",
                    "labels": [],
                    "head": {"ref": "feature/cache"},
                    "base": {"ref": "main"},
                },
                "sender": {"login": "review-bot", "id": 1, "type": "User"},
                "repository": {"full_name": "org/repo"},
            },
            "pull_request_review",
        )
    finally:
        settings.set("GITHUB_APP", original_github_app)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == []


@pytest.mark.asyncio
async def test_github_automatic_feedback_continues_after_invalid_command(monkeypatch):
    commands, repo_settings_calls = await _run_github_pr_commands(
        monkeypatch,
        repo_setting=True,
        configured_commands=['/ask "unterminated', "/review"],
    )

    assert commands == [["/review"]]
    assert repo_settings_calls == 1


@pytest.mark.asyncio
async def test_gitea_automatic_feedback_continues_after_invalid_command(monkeypatch):
    settings = get_settings()
    original_gitea = copy.deepcopy(settings.get("GITEA"))
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITEA.PR_COMMANDS", ['/ask "unterminated', "/review"])

    agent = RecordingAgent()
    body = {
        "action": "opened",
        "pull_request": {
            "url": "https://gitea.example.com/org/repo/pulls/1",
            "title": "Regular PR",
            "labels": [],
            "head": {"ref": "feature/cache"},
            "base": {"ref": "main"},
        },
        "sender": {"login": "alice"},
        "repository": {"full_name": "org/repo"},
    }

    monkeypatch.setattr(gitea_app, "apply_repo_settings", lambda _url: None)
    try:
        await gitea_app._perform_commands_gitea(
            "pr_commands", agent, body, body["pull_request"]["url"]
        )
    finally:
        settings.set("GITEA", original_gitea)
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert agent.commands == [["/review"]]


async def _run_gitlab_pr_commands(module, monkeypatch, draft, repo_setting, event="open"):
    settings = get_settings()
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITLAB.PR_COMMANDS", ["/review"])
    settings.set("GITLAB.PUSH_COMMANDS", ["/review"])
    settings.set("GITLAB.HANDLE_PUSH_TRIGGER", True)
    # Prove repo settings are applied before draft filtering.
    settings.set("GITLAB.FEEDBACK_ON_DRAFT_PR", not repo_setting)

    repo_settings_calls = 0

    def apply_repo_settings(_):
        nonlocal repo_settings_calls
        repo_settings_calls += 1
        get_settings().set("GITLAB.FEEDBACK_ON_DRAFT_PR", repo_setting)

    agent = RecordingAgent()
    monkeypatch.setattr(module, "apply_repo_settings", apply_repo_settings)
    monkeypatch.setattr(module, "PRAgent", lambda: agent)
    secret_provider = SimpleNamespace(
        get_secret=lambda _: '{"gitlab_token": "token"}'
    )
    monkeypatch.setattr(
        module, "get_fork_safe_secret_provider", lambda: secret_provider
    )
    object_attributes = {
        "action": "update" if event.startswith("draft_ready") else event,
        "draft": draft,
        "url": "https://gitlab.com/org/repo/-/merge_requests/1",
    }
    if event in ("update", "draft_ready_push"):
        object_attributes["oldrev"] = "previous-revision"
    data = _gitlab_payload(**object_attributes)
    data["object_kind"] = "merge_request"
    if event.startswith("draft_ready"):
        data["changes"] = {"draft": {"previous": True, "current": False}}
    try:
        response = await _post_gitlab_webhook(module.app, data)
    finally:
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert response.status_code == 200
    return agent.commands, repo_settings_calls


@pytest.mark.asyncio
async def test_gitlab_push_uses_shared_dedupe_slot(gitlab_webhook_module, monkeypatch):
    slots = []

    @asynccontextmanager
    async def reject_duplicate(key, **kwargs):
        slots.append((key, kwargs))
        yield False

    monkeypatch.setattr(gitlab_webhook_module, "push_trigger_slot", reject_duplicate)
    commands, _ = await _run_gitlab_pr_commands(
        gitlab_webhook_module, monkeypatch, draft=False, repo_setting=False, event="update"
    )

    assert commands == []
    assert slots == [
        ("https://gitlab.com/org/repo/-/merge_requests/1", {"allow_backlog": True, "ttl": 300})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("feedback_on_draft_pr", [True, False])
async def test_gitlab_draft_ready_with_new_commits(gitlab_webhook_module, monkeypatch, feedback_on_draft_pr):
    # One update clears the draft flag and carries commits. With draft feedback already on,
    # the review has been running all along and only the push half is new, so it takes the
    # push path through the dedupe slot instead of returning at the guard.
    slots = []

    @asynccontextmanager
    async def record_slot(key, **kwargs):
        slots.append((key, kwargs))
        yield True

    monkeypatch.setattr(gitlab_webhook_module, "push_trigger_slot", record_slot)
    commands, _ = await _run_gitlab_pr_commands(
        gitlab_webhook_module, monkeypatch, draft=False,
        repo_setting=feedback_on_draft_pr, event="draft_ready_push",
    )

    assert commands == [["/review"]]
    expected_slots = [("https://gitlab.com/org/repo/-/merge_requests/1", {"allow_backlog": True, "ttl": 300})]
    assert slots == (expected_slots if feedback_on_draft_pr else [])


@pytest.mark.asyncio
async def test_gitea_push_uses_shared_dedupe_slot(monkeypatch):
    settings = get_settings()
    original_gitea = copy.deepcopy(settings.get("GITEA"))
    settings.set("GITEA.HANDLE_PUSH_TRIGGER", True)
    settings.set("GITEA.PUSH_COMMANDS", ["/review"])
    slots = []
    performed = []

    @asynccontextmanager
    async def reject_duplicate(key, **kwargs):
        slots.append((key, kwargs))
        yield False

    async def perform_commands(*args):
        performed.append(args)

    monkeypatch.setattr(gitea_app, "push_trigger_slot", reject_duplicate)
    monkeypatch.setattr(gitea_app, "_perform_commands_gitea", perform_commands)
    api_url = "https://gitea.example.com/org/repo/pulls/1"
    try:
        await gitea_app.handle_pr_event(
            {"pull_request": {"url": api_url}}, "pull_request", "synchronized", RecordingAgent()
        )
    finally:
        settings.set("GITEA", original_gitea)

    assert performed == []
    assert slots == [(api_url, {"allow_backlog": True, "ttl": 300})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "draft", "feedback_on_draft_pr", "expected_commands"),
    [
        ("open", True, False, []),
        ("open", True, True, [["/review"]]),
        ("open", False, False, [["/review"]]),
        ("reopen", True, False, []),
        ("reopen", True, True, [["/review"]]),
        ("update", True, False, []),
        ("update", True, True, [["/review"]]),
        ("draft_ready", False, False, [["/review"]]),
        ("draft_ready", False, True, []),
    ],
)
async def test_gitlab_automatic_feedback_follows_draft_setting(
    gitlab_webhook_module,
    monkeypatch,
    event,
    draft,
    feedback_on_draft_pr,
    expected_commands,
):
    commands, repo_settings_calls = await _run_gitlab_pr_commands(
        gitlab_webhook_module, monkeypatch, draft, feedback_on_draft_pr, event
    )

    assert commands == expected_commands
    assert repo_settings_calls == 1


@pytest.mark.asyncio
async def test_gitlab_manual_feedback_on_draft_is_unaffected(gitlab_webhook_module, monkeypatch):
    settings = get_settings()
    settings.set("GITLAB.FEEDBACK_ON_DRAFT_PR", False)

    agent = RecordingAgent()
    monkeypatch.setattr(gitlab_webhook_module, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        gitlab_webhook_module,
        "get_fork_safe_secret_provider",
        lambda: SimpleNamespace(get_secret=lambda _: '{"gitlab_token": "token"}'),
    )
    monkeypatch.setattr(
        gitlab_webhook_module,
        "get_git_provider_with_context",
        lambda **_: SimpleNamespace(add_eyes_reaction=lambda *_: None),
    )
    data = _gitlab_payload(note="/review", id=1)
    data.update(
        {
            "object_kind": "note",
            "event_type": "note",
            "merge_request": {
                "draft": True,
                "url": "https://gitlab.com/org/repo/-/merge_requests/1",
            },
        }
    )

    response = await _post_gitlab_webhook(gitlab_webhook_module.app, data)

    assert response.status_code == 200
    assert agent.commands == ["/review"]


@pytest.mark.parametrize(
    "line_range, expected_start, expected_end, expected_side",
    [
        (
            {
                "start": {"type": "new", "new_line": 10, "old_line": 9},
                "end": {"type": "new", "new_line": 12, "old_line": 11},
            },
            10,
            12,
            "RIGHT",
        ),
        (
            {
                "start": {"type": "old", "new_line": None, "old_line": 9},
                "end": {"type": "old", "new_line": None, "old_line": 11},
            },
            9,
            11,
            "LEFT",
        ),
        (
            {
                "start": {"new_line": 10},
                "end": {"new_line": 12},
            },
            10,
            12,
            "RIGHT",
        ),
    ],
)
def test_gitlab_handle_ask_line_selects_line_numbers_and_side_from_line_range(
    gitlab_webhook_module, line_range, expected_start, expected_end, expected_side
):
    data = {
        "object_attributes": {
            "discussion_id": "disc-1",
            "position": {
                "new_path": "new/src/app.py",
                "old_path": "old/src/app.py",
                "line_range": line_range,
            },
        }
    }

    body = gitlab_webhook_module.handle_ask_line("/ask why this change?", data)

    assert body == (
        [
            "/ask_line",
            f"--line_start={expected_start}",
            f"--line_end={expected_end}",
            f"--side={expected_side}",
            "--file_name=new/src/app.py",
            "--comment_id=disc-1",
            "why this change?",
        ]
    )


def test_gitlab_handle_ask_line_only_strips_leading_ask_command(gitlab_webhook_module):
    data = {
        "object_attributes": {
            "discussion_id": "disc-1",
            "position": {
                "new_path": "src/app.py",
                "line_range": {
                    "start": {"type": "new", "new_line": 10},
                    "end": {"type": "new", "new_line": 10},
                },
            },
        }
    }

    body = gitlab_webhook_module.handle_ask_line(
        "/ask explain why /ask appears in the source",
        data,
    )

    assert body[-1] == "explain why /ask appears in the source"


def test_gitlab_handle_ask_line_keeps_question_as_one_argv_item(gitlab_webhook_module):
    data = {
        "object_attributes": {
            "discussion_id": "disc-1",
            "position": {
                "new_path": "src/app.py",
                "line_range": {
                    "start": {"type": "new", "new_line": 10},
                    "end": {"type": "new", "new_line": 10},
                },
            },
        }
    }

    body = gitlab_webhook_module.handle_ask_line(
        "/ask explain --file_name=not-a-cli-argument and keep spaces",
        data,
    )

    assert body[-1] == "explain --file_name=not-a-cli-argument and keep spaces"


@pytest.mark.parametrize(
    "sender_name, expected",
    [
        ("Codium Bot", True),
        ("release_bot", True),
        ("release-bot", True),
        ("bot-release", True),
        ("bot_release", True),
        ("Jane Developer", False),
        ("renovate[bot]", False),  # 'renovate' is not in the default list
    ],
)
def test_gitlab_is_bot_user_uses_default_indicators(
    gitlab_webhook_module, sender_name, expected
):
    # No override applied: fall back to the authoritative default in configuration.toml.
    data = {"user": {"name": sender_name}}
    assert gitlab_webhook_module.is_bot_user(data) is expected


def test_gitlab_is_bot_user_honors_configured_indicators(gitlab_webhook_module):
    settings = get_settings()
    original_override = settings.get("CONFIG.BOT_USER_INDICATORS")
    settings.set("CONFIG.BOT_USER_INDICATORS", ["renovate", "dependabot"])
    try:
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "renovate[bot]"}}
        ) is True
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "dependabot"}}
        ) is True
        # A name matching the built-in default list must NOT be flagged when the
        # override is set: configured indicators fully replace the defaults.
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "codium-agent"}}
        ) is False
    finally:
        settings.set("CONFIG.BOT_USER_INDICATORS", original_override)


def test_gitlab_is_bot_user_matches_case_insensitively(gitlab_webhook_module):
    # Operator supplies indicators with varied casing; matching must be case-insensitive
    # against the (already lowercased) sender display name.
    settings = get_settings()
    original_override = settings.get("CONFIG.BOT_USER_INDICATORS")
    settings.set("CONFIG.BOT_USER_INDICATORS", ["Renovate", "DEPENDABOT"])
    try:
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "renovate[bot]"}}
        ) is True
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "dependabot"}}
        ) is True
    finally:
        settings.set("CONFIG.BOT_USER_INDICATORS", original_override)


def test_gitlab_is_bot_user_normalizes_string_value(gitlab_webhook_module):
    # A misconfigured .pr_agent.toml that sets a bare string instead of a list must not
    # trigger per-character iteration; the value should be treated as a single indicator.
    settings = get_settings()
    original_override = settings.get("CONFIG.BOT_USER_INDICATORS")
    settings.set("CONFIG.BOT_USER_INDICATORS", "renovate")
    try:
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "renovate[bot]"}}
        ) is True
        # 'r', 'e', 'n', 'o', 'v', 'a', 't', 'e' are individual chars — none of these
        # should have matched 'Jane Developer' if the normalization treated the string
        # as a list of characters. Guard against that regression.
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "Jane Developer"}}
        ) is False
    finally:
        settings.set("CONFIG.BOT_USER_INDICATORS", original_override)


def test_gitlab_is_bot_user_skips_non_string_entries(gitlab_webhook_module):
    # Non-string entries in the list should be silently dropped, not crash detection.
    settings = get_settings()
    original_override = settings.get("CONFIG.BOT_USER_INDICATORS")
    settings.set("CONFIG.BOT_USER_INDICATORS", ["renovate", 42, None, "bot"])
    try:
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "renovate[bot]"}}
        ) is True
        assert gitlab_webhook_module.is_bot_user(
            {"user": {"name": "Jane Developer"}}
        ) is False
    finally:
        settings.set("CONFIG.BOT_USER_INDICATORS", original_override)


async def _run_gitlab_update(module, monkeypatch, *, oldrev, draft_ready, handle_push_trigger):
    """Post one merge-request `update` and report which commands ran.

    Distinct `pr_commands` and `push_commands` so the two paths can be told
    apart: the shared helper above configures `/review` for both.
    """
    settings = get_settings()
    original_is_auto_command = settings.get("CONFIG.IS_AUTO_COMMAND")
    settings.set("GITLAB.PR_COMMANDS", ["/review"])
    settings.set("GITLAB.PUSH_COMMANDS", ["/describe"])
    settings.set("GITLAB.HANDLE_PUSH_TRIGGER", handle_push_trigger)
    settings.set("GITLAB.FEEDBACK_ON_DRAFT_PR", False)

    agent = RecordingAgent()
    monkeypatch.setattr(module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(module, "PRAgent", lambda: agent)
    monkeypatch.setattr(
        module,
        "get_fork_safe_secret_provider",
        lambda: SimpleNamespace(get_secret=lambda _: '{"gitlab_token": "token"}'),
    )

    object_attributes = {
        "action": "update",
        "draft": False,
        "url": "https://gitlab.com/org/repo/-/merge_requests/1",
    }
    if oldrev:
        object_attributes["oldrev"] = "previous-revision"
    data = _gitlab_payload(**object_attributes)
    data["object_kind"] = "merge_request"
    if draft_ready:
        data["changes"] = {"draft": {"previous": True, "current": False}}
    try:
        response = await _post_gitlab_webhook(module.app, data)
    finally:
        settings.set("CONFIG.IS_AUTO_COMMAND", original_is_auto_command)

    assert response.status_code == 200
    return agent.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("handle_push_trigger", [False, True])
async def test_gitlab_update_that_is_both_a_push_and_draft_ready_still_reviews(
    gitlab_webhook_module, monkeypatch, handle_push_trigger
):
    """Marking an MR ready and pushing in one action must not run nothing.

    Both `update` branches match this payload. The push branch was tested
    first, so with `handle_push_trigger` false, the common workflow -- push the
    last commit and clear the draft flag together -- returned having run no
    command at all, silently. Draft-to-ready is the more significant of the two
    transitions, so it wins in either setting rather than only when the push
    branch declines.
    """
    commands = await _run_gitlab_update(
        gitlab_webhook_module,
        monkeypatch,
        oldrev=True,
        draft_ready=True,
        handle_push_trigger=handle_push_trigger,
    )
    assert commands == [["/review"]]


@pytest.mark.asyncio
async def test_gitlab_push_only_update_still_takes_the_push_branch(
    gitlab_webhook_module, monkeypatch
):
    """The reordering must not steal a plain push from `push_commands`."""
    commands = await _run_gitlab_update(
        gitlab_webhook_module,
        monkeypatch,
        oldrev=True,
        draft_ready=False,
        handle_push_trigger=True,
    )
    assert commands == [["/describe"]]
