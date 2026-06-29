import copy
import importlib

import pytest

import pr_agent.servers.bitbucket_server_webhook as bitbucket_server_webhook
from pr_agent.config_loader import get_settings


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


def test_bitbucket_server_process_command_applies_repo_settings_and_filters_args(monkeypatch):
    calls = []

    monkeypatch.setattr(bitbucket_server_webhook, "apply_repo_settings", lambda url: calls.append(("repo", url)))
    monkeypatch.setattr(
        bitbucket_server_webhook,
        "update_settings_from_args",
        lambda args: [arg for arg in args if not arg.startswith("--config.")],
    )

    command = bitbucket_server_webhook._process_command(
        "/review --config.temperature=0 --pr_reviewer.extra_instructions=test",
        "https://example/pr/1",
    )

    assert calls == [("repo", "https://example/pr/1")]
    assert command == "/review --pr_reviewer.extra_instructions=test"


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


def test_gitlab_handle_ask_line_converts_new_line_diff_note_to_right_side_command(gitlab_webhook_module):
    data = {
        "object_attributes": {
            "discussion_id": "disc-1",
            "position": {
                "new_path": "src/app.py",
                "line_range": {
                    "start": {"new_line": 10},
                    "end": {"new_line": 12},
                },
            },
        }
    }

    body = gitlab_webhook_module.handle_ask_line("/ask why this change?", data)

    assert body == (
        "/ask_line --line_start=10 --line_end=12 --side=RIGHT "
        "--file_name=src/app.py --comment_id=disc-1 why this change?"
    )
