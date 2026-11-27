"""Pin one filtering policy across the five webhook servers.

Read each test as the same question asked of every server: should this pull request be
processed at all. #3516 moved the rules into `pr_agent/servers/utils.py`, so drive the
servers through their own entry points rather than the helper, and keep them agreeing
rule by rule — that is what stops the five copies coming back.

Cover the string-valued `config.ignore_pr_title` case too: iterating "^WIP" yields "^",
which matches every title, so a setting written as a plain string must be treated as one
pattern on every provider.
"""

import copy
import importlib
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import bitbucket_app, bitbucket_server_webhook, gitea_app, github_app
from pr_agent.servers.utils import should_process_pr_logic as shared_should_process_pr_logic
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

IGNORE_KEYS = (
    "CONFIG.IGNORE_REPOSITORIES",
    "CONFIG.IGNORE_PR_AUTHORS",
    "CONFIG.IGNORE_PR_TITLE",
    "CONFIG.IGNORE_PR_LABELS",
    "CONFIG.IGNORE_PR_SOURCE_BRANCHES",
    "CONFIG.IGNORE_PR_TARGET_BRANCHES",
    "CONFIG.INCLUDE_PR_LABELS",
)

DEFAULTS = {
    "title": "Regular PR",
    "sender": "alice",
    "source_branch": "feature/cache",
    "target_branch": "main",
    "repo_full_name": "org/repo",
    "labels": (),
}


def _fields(**overrides):
    fields = dict(DEFAULTS)
    fields.update(overrides)
    return fields


def _github_body(fields):
    return {
        "pull_request": {
            "title": fields["title"],
            "labels": [{"name": label} for label in fields["labels"]],
            "head": {"ref": fields["source_branch"]},
            "base": {"ref": fields["target_branch"]},
            "user": {"login": fields["sender"]},
        },
        "sender": {"login": fields["sender"]},
        "repository": {"full_name": fields["repo_full_name"]},
    }


def _bitbucket_payload(fields):
    return {
        "data": {
            "pullrequest": {
                "title": fields["title"],
                "author": {"nickname": fields["sender"]},
                "source": {"branch": {"name": fields["source_branch"]}},
                "destination": {
                    "branch": {"name": fields["target_branch"]},
                    "repository": {"full_name": fields["repo_full_name"]},
                },
            },
            "actor": {"display_name": fields["sender"]},
        }
    }


def _bitbucket_server_payload(fields):
    project_key, _, repo_slug = fields["repo_full_name"].partition("/")
    return {
        "pullRequest": {
            "id": 7,
            "title": fields["title"],
            "fromRef": {"displayId": fields["source_branch"]},
            "toRef": {
                "displayId": fields["target_branch"],
                "repository": {"slug": repo_slug, "project": {"key": project_key}},
            },
            "author": {"user": {"name": fields["sender"]}},
        }
    }


def _gitlab_payload(fields):
    return {
        "object_attributes": {
            "title": fields["title"],
            "source_branch": fields["source_branch"],
            "target_branch": fields["target_branch"],
        },
        "labels": [{"title": label} for label in fields["labels"]],
        "project": {"path_with_namespace": fields["repo_full_name"]},
        "user": {"username": fields["sender"]},
    }


@pytest.fixture(scope="module")
def gitlab_webhook_module():
    settings = get_settings()
    original_git_provider = settings.config.get("git_provider", None)
    had_gitlab_settings = "GITLAB" in settings
    original_gitlab_settings = copy.deepcopy(settings.get("GITLAB", None))
    settings.set("GITLAB.URL", "https://gitlab.com")
    try:
        yield importlib.import_module("pr_agent.servers.gitlab_webhook")
    finally:
        settings.config.git_provider = original_git_provider
        if had_gitlab_settings:
            settings.set("GITLAB", original_gitlab_settings)
        else:
            settings.unset("GITLAB", force=True)


@pytest.fixture
def providers(gitlab_webhook_module):
    """Yield one (name, entry point, payload builder, carries labels) row per webhook server."""
    return (
        ("github", github_app.should_process_pr_logic, _github_body, True),
        ("gitea", gitea_app.should_process_pr_logic, _github_body, True),
        ("gitlab", gitlab_webhook_module.should_process_pr_logic, _gitlab_payload, True),
        ("bitbucket", bitbucket_app.should_process_pr_logic, _bitbucket_payload, False),
        ("bitbucket_server", bitbucket_server_webhook.should_process_pr_logic, _bitbucket_server_payload, False),
    )


@pytest.fixture
def ignore_settings():
    snapshot = snapshot_settings((*IGNORE_KEYS, "CONFIG.INCLUDE_PR_LABELS_MATCH_ALL"))
    settings = get_settings()
    for key in IGNORE_KEYS:
        settings.set(key, [])
    settings.set("CONFIG.INCLUDE_PR_LABELS_MATCH_ALL", False)
    yield settings
    restore_settings(snapshot)


def _assert_all(providers, fields, expected, reason, only_with_labels=False):
    for name, should_process, build_payload, carries_labels in providers:
        if only_with_labels and not carries_labels:
            continue
        result = should_process(build_payload(fields))
        assert result is expected, f"{name}: expected {expected} for {reason}, got {result}"


def test_github_and_gitea_ignore_pr_author_not_event_sender(ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["blocked"])
    data = _github_body(_fields(sender="allowed"))
    data["sender"]["login"] = "blocked"
    assert github_app.should_process_pr_logic(data) is True
    assert gitea_app.should_process_pr_logic(data) is True

    data["pull_request"]["user"]["login"] = "blocked"
    data["sender"]["login"] = "allowed"
    assert github_app.should_process_pr_logic(data) is False
    assert gitea_app.should_process_pr_logic(data) is False

    # A malformed or incomplete webhook cannot turn the missing author into
    # the update actor, or pass None to regex matching.
    data["pull_request"]["user"]["login"] = None
    data["sender"]["login"] = "blocked"
    assert github_app.should_process_pr_logic(data) is True
    assert gitea_app.should_process_pr_logic(data) is True


def test_bitbucket_cloud_ignores_pr_author_not_event_actor(ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["blocked"])
    data = _bitbucket_payload(_fields(sender="allowed"))
    data["data"]["actor"] = {"nickname": "blocked"}
    assert bitbucket_app.should_process_pr_logic(data) is True

    data["data"]["pullrequest"]["author"]["nickname"] = "blocked"
    data["data"]["actor"] = {"nickname": "allowed"}
    assert bitbucket_app.should_process_pr_logic(data) is False


def test_bitbucket_cloud_prefers_author_display_name_over_nickname(ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["Blocked Author"])
    data = _bitbucket_payload(_fields(sender="allowed"))
    data["data"]["pullrequest"]["author"] = {
        "display_name": "Blocked Author", "nickname": "different-nickname"
    }
    data["data"]["actor"] = {"display_name": "allowed"}
    assert bitbucket_app.should_process_pr_logic(data) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea"])
async def test_webhook_external_host_rules_deny_before_repository_settings(
    monkeypatch, gitlab_webhook_module, tmp_path, provider,
):
    from starlette_context import request_cycle_context

    external = tmp_path / "operator.toml"
    external.write_text('[config]\nignore_pr_title = ["^Blocked"]\n')
    settings = copy.deepcopy(get_settings())
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(external))
    settings.set("CONFIG.IGNORE_PR_TITLE", [])
    settings.set("CONFIG.INCLUDE_PR_LABELS", [])
    dispatched = AsyncMock()

    if provider == "github":
        body = _github_body(_fields(title="Blocked change"))
        body["action"] = "opened"
        monkeypatch.setattr(github_app, "get_log_context", lambda *_: ({}, "alice", 1, "User"))
        monkeypatch.setattr(github_app, "is_bot_user", lambda *_: False)
        monkeypatch.setattr(github_app, "revoke_auto_approval_on_push", lambda _: None)
        monkeypatch.setattr(github_app, "handle_new_pr_opened", dispatched)
        execute = github_app._dispatch_request(body, "pull_request", "opened")
    elif provider == "gitlab":
        body = _gitlab_payload(_fields(title="Blocked change"))
        body["object_kind"] = "merge_request"
        body["object_attributes"].update(action="open", url="https://gitlab.com/org/repo/-/merge_requests/1")
        monkeypatch.setattr(gitlab_webhook_module, "is_bot_user", lambda _: False)
        monkeypatch.setattr(gitlab_webhook_module, "apply_new_mr_repo_settings", dispatched)
        execute = gitlab_webhook_module.process_gitlab_event(body, {})
    else:
        body = _github_body(_fields(title="Blocked change"))
        body["action"] = "opened"
        monkeypatch.setattr(gitea_app, "handle_pr_event", dispatched)
        execute = gitea_app.handle_request(body, "pull_request")

    with request_cycle_context({"settings": settings, "git_provider": {}}):
        await execute
        assert get_settings().get("CONFIG.IGNORE_PR_TITLE") == ["^Blocked"]
    dispatched.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("author", "actor", "dispatch"), [
    ("blocked", "allowed", False),
    ("allowed", "blocked", True),
])
async def test_gitlab_host_author_check_uses_mr_author_on_open(
    gitlab_webhook_module, ignore_settings, monkeypatch, author, actor, dispatch,
):
    from starlette_context import request_cycle_context

    ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["blocked"])
    body = _gitlab_payload(_fields(sender=actor))
    body["object_kind"] = "merge_request"
    body["user"]["id"] = 22
    body["object_attributes"].update(
        author_id=11, action="open", url="https://gitlab.com/org/repo/-/merge_requests/7"
    )
    provider = SimpleNamespace(get_request_policy_metadata=lambda fields: {"sender": author})
    monkeypatch.setattr(gitlab_webhook_module, "is_bot_user", lambda _data: False)
    monkeypatch.setattr(gitlab_webhook_module, "get_git_provider", lambda: lambda _url: provider)
    settings_read = AsyncMock()
    commands = AsyncMock()
    monkeypatch.setattr(gitlab_webhook_module, "apply_new_mr_repo_settings", settings_read)
    monkeypatch.setattr(gitlab_webhook_module, "_perform_commands_gitlab", commands)

    with request_cycle_context({"settings": ignore_settings, "git_provider": {}}):
        await gitlab_webhook_module.process_gitlab_event(body, {})

    assert settings_read.await_count == int(dispatch)
    assert commands.await_count == int(dispatch)


@pytest.mark.parametrize(("author", "actor", "allowed"), [
    ("blocked", "collaborator", False),
    ("collaborator", "blocked", True),
])
def test_gitlab_post_repo_author_filter_uses_mr_author(
    gitlab_webhook_module, ignore_settings, monkeypatch, author, actor, allowed,
):
    from starlette_context import request_cycle_context

    # Simulate a repository-only exclusion: the earlier host prefilter did not fetch the author.
    payload = _gitlab_payload(_fields(sender=actor))
    payload["user"]["id"] = 22
    payload["object_attributes"].update(
        author_id=11, action="update", url="https://gitlab.com/org/repo/-/merge_requests/7"
    )
    provider = SimpleNamespace(get_request_policy_metadata=lambda _fields: {"sender": author})
    get_provider = Mock(return_value=provider)
    monkeypatch.setattr(gitlab_webhook_module, "get_git_provider_with_context", get_provider)

    with request_cycle_context({"settings": ignore_settings, "git_provider": {}}):
        ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["blocked"])
        assert gitlab_webhook_module.should_process_pr_logic(payload) is allowed
    get_provider.assert_called_once_with(pr_url=payload["object_attributes"]["url"])


async def test_gitlab_author_prefilter_uses_uncached_provider(
    gitlab_webhook_module, ignore_settings, monkeypatch,
):
    from starlette_context import context, request_cycle_context

    ignore_settings.set("CONFIG.IGNORE_PR_AUTHORS", ["blocked"])
    payload = _gitlab_payload(_fields(sender="collaborator"))
    payload["object_kind"] = "merge_request"
    payload["user"]["id"] = 22
    payload["object_attributes"].update(
        author_id=11, action="open", url="https://gitlab.com/org/repo/-/merge_requests/7"
    )
    provider = SimpleNamespace(get_request_policy_metadata=lambda _fields: {"sender": "blocked"})
    cached_provider = Mock()
    monkeypatch.setattr(gitlab_webhook_module, "is_bot_user", lambda _data: False)
    monkeypatch.setattr(gitlab_webhook_module, "get_git_provider", lambda: lambda _url: provider)
    monkeypatch.setattr(gitlab_webhook_module, "get_git_provider_with_context", cached_provider)
    with request_cycle_context({"settings": ignore_settings, "git_provider": {}}):
        await gitlab_webhook_module.process_gitlab_event(payload, {})
        assert context.get("git_provider") == {}
    cached_provider.assert_not_called()


def test_unfiltered_pr_is_processed_by_every_server(providers, ignore_settings):
    _assert_all(providers, _fields(), True, "a PR that matches no ignore rule")


@pytest.mark.parametrize(
    "setting, patterns, matching, non_matching",
    [
        ("CONFIG.IGNORE_REPOSITORIES", ["^org/repo$"], {"repo_full_name": "org/repo"},
         {"repo_full_name": "org/other"}),
        ("CONFIG.IGNORE_PR_AUTHORS", ["^dependabot"], {"sender": "dependabot[bot]"}, {"sender": "alice"}),
        ("CONFIG.IGNORE_PR_TITLE", ["^WIP"], {"title": "WIP: cache"}, {"title": "Add cache"}),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", ["^generated/"], {"source_branch": "generated/api"},
         {"source_branch": "feature/api"}),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", ["^legacy$"], {"target_branch": "legacy"}, {"target_branch": "main"}),
    ],
)
def test_every_server_applies_the_same_ignore_rule(providers, ignore_settings, setting, patterns, matching,
                                                   non_matching):
    ignore_settings.set(setting, patterns)
    _assert_all(providers, _fields(**matching), False, f"{setting}={patterns} matching {matching}")
    _assert_all(providers, _fields(**non_matching), True, f"{setting}={patterns} not matching {non_matching}")


def test_label_rule_is_shared_by_the_servers_that_carry_labels(providers, ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", ["skip-pr-agent"])
    _assert_all(providers, _fields(labels=("skip-pr-agent",)), False, "an ignored label", only_with_labels=True)
    _assert_all(providers, _fields(labels=("ready",)), True, "a label that is not ignored", only_with_labels=True)


def test_include_labels_requires_at_least_one_matching_label(providers, ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review", "on-demand"])
    _assert_all(providers, _fields(labels=("on-demand", "other")), True,
                "one selected label", only_with_labels=True)
    _assert_all(providers, _fields(labels=("other",)), False, "no selected label")
    _assert_all(providers, _fields(), False, "no labels")


@pytest.mark.parametrize(("match_all", "labels", "allowed"), [
    (False, ("ai-review",), True),
    ("false", ("ai-review",), True),
    (True, ("ai-review",), False),
    ("true", ("ready-for-review",), False),
    (True, ("ai-review", "ready-for-review"), True),
    (True, (), False),
])
def test_include_labels_can_require_all(providers, ignore_settings, match_all, labels, allowed):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review", "ready-for-review"])
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS_MATCH_ALL", match_all)
    _assert_all(providers, _fields(labels=labels), allowed, "all selected labels", only_with_labels=True)


def test_all_label_mode_without_allowlist_does_not_restrict_prs(ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS_MATCH_ALL", True)
    assert shared_should_process_pr_logic(labels=[]) is True


def test_all_label_mode_still_respects_exclusions(providers, ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review", "ready-for-review"])
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS_MATCH_ALL", True)
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    _assert_all(providers, _fields(labels=("ai-review", "ready-for-review", "skip")), False,
                "ignored label overrides all-match", only_with_labels=True)


def test_ignore_labels_still_override_include_labels(providers, ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review"])
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    _assert_all(providers, _fields(labels=("ai-review", "skip")), False,
                "an explicitly ignored selected PR", only_with_labels=True)
    _assert_all(providers, _fields(labels=("ai-review",)), True,
                "an eligible selected PR", only_with_labels=True)


def test_include_labels_scalar_is_one_exact_label(providers, ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", "ready")
    _assert_all(providers, _fields(labels=("ready",)), True, "the exact label", only_with_labels=True)
    _assert_all(providers, _fields(labels=("r",)), False, "not a substring")


@pytest.mark.parametrize(("setting", "matching_label"), [(0, "0"), (123, "123"), ([0], "0")])
def test_numeric_include_label_settings_do_not_disable_the_gate(ignore_settings, setting, matching_label):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", setting)
    assert shared_should_process_pr_logic(labels=[matching_label]) is True
    assert shared_should_process_pr_logic(labels=["other"]) is False
    assert shared_should_process_pr_logic(labels=[]) is False


def test_invalid_label_payload_cannot_bypass_an_enabled_include_gate(providers, ignore_settings):
    gitlab = next(should_process for name, should_process, *_ in providers if name == "gitlab")
    payload = _gitlab_payload(_fields())
    payload["labels"] = True  # GitLab normally supplies a list; this value cannot be read.

    assert gitlab(payload) is True  # Existing fail-open behavior without an opt-in gate.
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review"])
    assert gitlab(payload) is False


def test_gitlab_explicit_empty_root_labels_override_nested_labels(ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review"])
    payload = _gitlab_payload(_fields())
    payload["object_attributes"]["labels"] = [{"title": "ai-review"}]
    assert shared_should_process_pr_logic(payload, provider="gitlab") is False


def test_malformed_label_containers_cannot_satisfy_allowlist(ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review", "a"])
    assert shared_should_process_pr_logic(labels={"ai-review": True}) is False
    assert shared_should_process_pr_logic(labels="a") is False
    assert shared_should_process_pr_logic(data={"pull_request": {"labels": {"name": "ai-review"}}}) is False


def test_prefilter_defers_include_labels_but_still_applies_exclusions(ignore_settings):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["host-label"])
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    assert shared_should_process_pr_logic(labels=["team-label"]) is False
    assert shared_should_process_pr_logic(labels=["team-label"], check_include_labels=False) is True
    assert shared_should_process_pr_logic(labels=["skip"], check_include_labels=False) is False


@pytest.mark.asyncio
async def test_github_and_gitea_prefilters_allow_repository_label_overrides(ignore_settings, monkeypatch):
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["host-label"])
    body = _github_body(_fields(labels=("team-label",)))
    body["action"] = "opened"
    body["pull_request"]["url"] = "https://github.com/org/repo/pull/7"

    monkeypatch.setattr(github_app, "get_log_context", lambda *_args: ({}, "human", 1, "User"))
    monkeypatch.setattr(github_app, "is_bot_user", lambda *_args: False)
    github_dispatch = AsyncMock()
    monkeypatch.setattr(github_app, "handle_new_pr_opened", github_dispatch)
    await github_app._dispatch_request(body, "pull_request", "opened")
    github_dispatch.assert_awaited_once()

    gitea_dispatch = AsyncMock()
    monkeypatch.setattr(gitea_app, "handle_pr_event", gitea_dispatch)
    await gitea_app.handle_request(body, "pull_request")
    gitea_dispatch.assert_awaited_once()

    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["team-label"])
    assert github_app.should_process_pr_logic(body) is True
    assert gitea_app.should_process_pr_logic(body) is True


@pytest.mark.asyncio
async def test_gitlab_prefilter_allows_repository_label_override(gitlab_webhook_module, ignore_settings, monkeypatch):
    from starlette.background import BackgroundTasks
    from starlette_context import request_cycle_context

    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["host-label"])
    payload = _gitlab_payload(_fields(labels=("team-label",)))
    payload["object_kind"] = "merge_request"
    payload["object_attributes"].update(action="open", url="https://gitlab.com/org/repo/-/merge_requests/7")
    monkeypatch.setattr(gitlab_webhook_module, "authenticate_gitlab_webhook", AsyncMock(return_value=None))
    monkeypatch.setattr(gitlab_webhook_module, "is_bot_user", lambda *_args: False)
    monkeypatch.setattr(gitlab_webhook_module, "global_settings", ignore_settings)

    async def apply_repo(_url):
        get_settings().set("CONFIG.INCLUDE_PR_LABELS", ["team-label"])

    run_commands = AsyncMock()
    monkeypatch.setattr(gitlab_webhook_module, "apply_new_mr_repo_settings", apply_repo)
    monkeypatch.setattr(gitlab_webhook_module, "_perform_commands_gitlab", run_commands)
    background = BackgroundTasks()
    with request_cycle_context({"settings": ignore_settings, "git_provider": {}}):
        await gitlab_webhook_module.gitlab_webhook(background, SimpleNamespace(json=AsyncMock(return_value=payload)))
        await background()
        assert gitlab_webhook_module.should_process_pr_logic(payload) is True
    run_commands.assert_awaited_once()


def test_bitbucket_server_filter_error_respects_opt_in_gate(ignore_settings, monkeypatch):
    monkeypatch.setattr(bitbucket_server_webhook, "shared_should_process_pr_logic",
                        Mock(side_effect=ValueError("malformed payload")))
    assert bitbucket_server_webhook.should_process_pr_logic({}) is True
    ignore_settings.set("CONFIG.INCLUDE_PR_LABELS", ["ai-review"])
    assert bitbucket_server_webhook.should_process_pr_logic({}) is False


def test_ignore_pr_title_given_as_a_plain_string_is_not_iterated_per_character(providers, ignore_settings):
    """Read a string setting as one pattern, never as a sequence of characters.

    Iterating "^WIP" yields "^", which matches every title, so an unrelated PR would be
    dropped. Dynaconf hands the value through unchanged when it is written as a string.
    """
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", "^WIP")
    _assert_all(providers, _fields(title="WIP: cache"), False, "a string ignore_pr_title that matches")
    _assert_all(providers, _fields(title="Add cache"), True, "a string ignore_pr_title that does not match")


def test_default_auto_title_ignore_only_matches_bracketed_titles(ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", [r"^\[Auto\]"])

    assert shared_should_process_pr_logic(title="Autoscaling fix") is True
    assert shared_should_process_pr_logic(title="Automate release notes") is True
    assert shared_should_process_pr_logic(title="AutoMapper upgrade") is True
    assert shared_should_process_pr_logic(title="[Auto] generated update") is False


@pytest.mark.parametrize(
    "setting, matching, non_matching, labels_only",
    [
        ("CONFIG.IGNORE_REPOSITORIES", {"repo_full_name": "org/blocked"}, {"repo_full_name": "org/repo"}, False),
        ("CONFIG.IGNORE_PR_AUTHORS", {"sender": "blocked"}, {"sender": "alice"}, False),
        ("CONFIG.IGNORE_PR_LABELS", {"labels": ("blocked",)}, {"labels": ("block",)}, True),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", {"source_branch": "blocked"},
         {"source_branch": "feature/cache"}, False),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", {"target_branch": "blocked"}, {"target_branch": "release"}, False),
    ],
)
def test_scalar_ignore_setting_is_one_pattern(providers, ignore_settings, setting, matching, non_matching, labels_only):
    ignore_settings.set(setting, "blocked")
    _assert_all(providers, _fields(**matching), False, f"{setting} matches", only_with_labels=labels_only)
    _assert_all(providers, _fields(**non_matching), True, f"{setting} does not match", only_with_labels=labels_only)


@pytest.mark.parametrize(
    "setting, matching, non_matching",
    [
        ("CONFIG.IGNORE_REPOSITORIES", {"repo_full_name": "org/2024"}, {"repo_full_name": "org/repo"}),
        ("CONFIG.IGNORE_PR_AUTHORS", {"sender": "user2024"}, {"sender": "alice"}),
        ("CONFIG.IGNORE_PR_TITLE", {"title": "Release 2024"}, {"title": "Add cache"}),
        ("CONFIG.IGNORE_PR_LABELS", {"labels": ("2024",)}, {"labels": ("ready",)}),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", {"source_branch": "release/2024"}, {"source_branch": "feature/cache"}),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", {"target_branch": "2024"}, {"target_branch": "main"}),
    ],
)
@pytest.mark.parametrize("value", [2024, [2024]])
def test_numeric_ignore_setting_is_matched_as_text(ignore_settings, setting, matching, non_matching, value):
    """An environment value such as CONFIG__IGNORE_PR_LABELS=2024 arrives as an int, not a string."""
    ignore_settings.set(setting, value)
    assert shared_should_process_pr_logic(**_fields(**matching)) is False
    assert shared_should_process_pr_logic(**_fields(**non_matching)) is True


def test_zero_ignore_label_is_not_dropped(ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", 0)
    assert shared_should_process_pr_logic(labels=["0"]) is False
    assert shared_should_process_pr_logic(labels=["ready"]) is True


@pytest.mark.parametrize("setting", IGNORE_KEYS)
def test_empty_scalar_ignore_setting_does_not_filter_any_pr(providers, ignore_settings, setting):
    ignore_settings.set(setting, "")
    _assert_all(providers, _fields(labels=("ready",)), True, f"{setting} is disabled when empty")


def test_an_invalid_pattern_does_not_filter_out_every_pull_request(providers, ignore_settings):
    """Process the PR when a rule cannot be evaluated; the opposite silently disables the agent."""
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", ["("])
    _assert_all(providers, _fields(), True, "an unparsable ignore_pr_title regex")


@pytest.mark.parametrize(
    "setting, field",
    [
        ("CONFIG.IGNORE_REPOSITORIES", "repo_full_name"),
        ("CONFIG.IGNORE_PR_AUTHORS", "sender"),
        ("CONFIG.IGNORE_PR_TITLE", "title"),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", "source_branch"),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", "target_branch"),
    ],
)
def test_catastrophic_ignore_pattern_times_out_as_no_match(ignore_settings, setting, field):
    """Bound a backtracking pattern from repository settings; a later pattern in the list still applies."""
    slow_value = "a" * 40 + "!"
    ignore_settings.set(setting, [r"(a|aa)+$"])
    start = time.monotonic()
    assert shared_should_process_pr_logic(**_fields(**{field: slow_value}), raise_on_error=True) is True
    assert time.monotonic() - start < 3

    ignore_settings.set(setting, [r"(a|aa)+$", "!$"])
    assert shared_should_process_pr_logic(**_fields(**{field: slow_value}), raise_on_error=True) is False


@pytest.mark.parametrize(
    "setting, field",
    [
        ("CONFIG.IGNORE_REPOSITORIES", "repo_full_name"),
        ("CONFIG.IGNORE_PR_AUTHORS", "sender"),
        ("CONFIG.IGNORE_PR_TITLE", "title"),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", "source_branch"),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", "target_branch"),
    ],
)
def test_ignore_pattern_over_the_length_cap_is_an_invalid_pattern(ignore_settings, setting, field):
    """Reject an oversized pattern before compiling it, the way an unparsable one is handled."""
    ignore_settings.set(setting, ["x" * 1025])
    assert shared_should_process_pr_logic(**_fields(**{field: "x" * 1025})) is True
    with pytest.raises(re.error):
        shared_should_process_pr_logic(**_fields(**{field: "x" * 1025}), raise_on_error=True)


def test_shared_helper_defaults_to_processing(ignore_settings):
    assert shared_should_process_pr_logic() is True
