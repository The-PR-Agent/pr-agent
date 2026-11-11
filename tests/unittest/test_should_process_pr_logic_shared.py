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
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent import git_providers
from pr_agent.agent import pr_agent as agent
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.servers import bitbucket_app, bitbucket_server_webhook, gitea_app, github_app
from pr_agent.servers import utils as server_utils
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
        },
        "sender": {"login": fields["sender"]},
        "repository": {"full_name": fields["repo_full_name"]},
    }


def _bitbucket_payload(fields):
    return {
        "data": {
            "pullrequest": {
                "title": fields["title"],
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
    snapshot = snapshot_settings(IGNORE_KEYS)
    settings = get_settings()
    for key in IGNORE_KEYS:
        settings.set(key, [])
    yield settings
    restore_settings(snapshot)


@pytest.fixture
def github_repo_policy(ignore_settings, monkeypatch):
    settings = copy.deepcopy(ignore_settings)
    for key, value in {
        "CONFIG.USE_REPO_SETTINGS_FILE": True,
        "CONFIG.EXTRA_CONFIG_URL": None,
        "CONFIG.ENABLE_PER_DIRECTORY_SETTINGS": False,
        "CONFIG.DISABLE_AUTO_FEEDBACK": False,
        "CONFIG.GIT_PROVIDER": "github",
        "GITHUB_APP.HANDLE_PR_ACTIONS": ["opened"],
        "GITHUB_APP.PR_COMMANDS": ["/review"],
        "GITHUB_APP.IGNORE_BOT_PR": False,
        "GITHUB.PUBLISH_AS_CHECK_RUN": False,
    }.items():
        settings.set(key, value)
    provider = Mock()
    for module in (git_providers, git_utils, agent, github_app):
        monkeypatch.setattr(module, "get_git_provider_with_context", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(github_app, "get_identity_provider", lambda: SimpleNamespace(
        verify_eligibility=lambda *_args: Eligibility.ELIGIBLE))
    monkeypatch.setattr(agent, "flush_telemetry", Mock())
    # The real loader changes this environment variable; restore it after each test.
    monkeypatch.setenv("AUTO_CAST_FOR_DYNACONF", "false")
    command = SimpleNamespace(run=AsyncMock())
    monkeypatch.setitem(agent.command2class, "review", Mock(return_value=command))
    with request_cycle_context({"settings": settings, "git_provider": {}}):
        yield settings, provider, command


def _assert_all(providers, fields, expected, reason, only_with_labels=False):
    for name, should_process, build_payload, carries_labels in providers:
        if only_with_labels and not carries_labels:
            continue
        result = should_process(build_payload(fields))
        assert result is expected, f"{name}: expected {expected} for {reason}, got {result}"


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
@pytest.mark.parametrize("setting, host_value, repo_value", [
    ("CONFIG.INCLUDE_PR_LABELS", ["host-label"], ["team-label"]),
    ("CONFIG.IGNORE_PR_LABELS", ["team-label"], []),
])
async def test_github_and_gitea_prefilters_allow_repository_label_overrides(
    ignore_settings, monkeypatch, setting, host_value, repo_value
):
    ignore_settings.set(setting, host_value)
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

    ignore_settings.set(setting, repo_value)
    assert github_app.should_process_pr_logic(body) is True
    assert gitea_app.should_process_pr_logic(body) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("setting, host_value, repo_value", [
    ("CONFIG.INCLUDE_PR_LABELS", ["host-label"], ["team-label"]),
    ("CONFIG.IGNORE_PR_LABELS", ["team-label"], []),
])
async def test_gitlab_prefilter_allows_repository_label_override(
    gitlab_webhook_module, ignore_settings, monkeypatch, setting, host_value, repo_value
):
    ignore_settings.set(setting, host_value)
    payload = _gitlab_payload(_fields(labels=("team-label",)))
    payload["object_kind"] = "merge_request"
    payload["object_attributes"].update(action="open", url="https://gitlab.com/org/repo/-/merge_requests/7")
    monkeypatch.setattr(gitlab_webhook_module, "authenticate_gitlab_webhook", AsyncMock(return_value=None))
    monkeypatch.setattr(gitlab_webhook_module, "is_bot_user", lambda *_args: False)
    monkeypatch.setattr(gitlab_webhook_module, "global_settings", ignore_settings)

    async def apply_repo(_url):
        get_settings().set(setting, repo_value)

    run_commands = AsyncMock()
    monkeypatch.setattr(gitlab_webhook_module, "apply_new_mr_repo_settings", apply_repo)
    monkeypatch.setattr(gitlab_webhook_module, "_perform_commands_gitlab", run_commands)
    background = BackgroundTasks()
    with request_cycle_context({"settings": ignore_settings, "git_provider": {}}):
        await gitlab_webhook_module.gitlab_webhook(background, SimpleNamespace(json=AsyncMock(return_value=payload)))
        await background()
        assert gitlab_webhook_module.should_process_pr_logic(payload) is True
    run_commands.assert_awaited_once()


@pytest.mark.parametrize("use_repo_settings, extra_source, expected", [
    (True, None, True),
    (False, None, False),
    (False, "  ", False),
    (False, "host.toml", True),
    ("false", None, True),  # Match the existing loader's truthiness check.
])
def test_prefilter_only_applies_host_exclusions_when_no_settings_source_can_override_them(
    ignore_settings, use_repo_settings, extra_source, expected
):
    settings = copy.deepcopy(ignore_settings)
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", use_repo_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", extra_source)
    settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    settings.set("CONFIG.INCLUDE_PR_LABELS", ["selected"])
    with request_cycle_context({"settings": settings}):
        body = _github_body(_fields(labels=("skip",)))
        assert server_utils.should_process_pr_before_repo_settings(body, provider="github") is expected
        # The existing opt-in gate is still deferred, even on the fast path.
        body = _github_body(_fields(labels=("other",)))
        assert server_utils.should_process_pr_before_repo_settings(body, provider="github") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("setting, host_value, repo_toml, fields, allowed", [
    ("ignore_pr_labels", ["skip"], b"", _fields(labels=("skip",)), False),
    ("ignore_pr_labels", ["skip"], b"[config]\nignore_pr_labels=[]", _fields(labels=("skip",)), True),
    ("ignore_pr_labels", ["host"], b'[config]\nignore_pr_labels=["repo"]', _fields(labels=("host",)), True),
    ("ignore_pr_labels", ["host"], b'[config]\nignore_pr_labels=["repo"]', _fields(labels=("repo",)), False),
    ("ignore_pr_title", [r"^\[Auto\]"], b"[config]\nignore_pr_title=[]", _fields(title="[Auto] Update"), True),
    ("include_pr_labels", ["host"], b'[config]\ninclude_pr_labels=["repo"]', _fields(labels=("repo",)), True),
])
async def test_github_auto_commands_use_repository_policy(
    github_repo_policy, setting, host_value, repo_toml, fields, allowed
):
    settings, provider, command = github_repo_policy
    settings.set(f"CONFIG.{setting}", host_value)
    provider.get_repo_settings.return_value = repo_toml
    provider.get_request_policy_metadata.return_value = fields
    body = _github_body(fields)
    body["action"] = "opened"
    body["pull_request"].update(url="https://api.github.com/repos/org/repo/pulls/7", state="open", draft=False)

    await github_app._dispatch_request(body, "pull_request", "opened")

    assert command.run.await_count == int(allowed)
    # The webhook and Agent both apply settings, but share the request's cached file.
    provider.get_repo_settings.assert_called_once()
    if allowed and settings.get(f"CONFIG.{setting}"):
        provider.get_request_policy_metadata.assert_called_once_with({"labels"})


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_toml, expected_repo_reads", [
    ("[config]\nignore_pr_labels=[]", 0),
    ("[config]\nuse_repo_settings_file=true", 1),
])
async def test_github_extra_settings_can_override_exclusions_or_enable_repo_settings(
    github_repo_policy, tmp_path, extra_toml, expected_repo_reads
):
    settings, provider, command = github_repo_policy
    extra_file = tmp_path / "host.toml"
    extra_file.write_text(extra_toml)
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", False)
    settings.set("CONFIG.EXTRA_CONFIG_URL", str(extra_file))
    settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    provider.get_repo_settings.return_value = b"[config]\nignore_pr_labels=[]"
    fields = _fields(labels=("skip",))
    provider.get_request_policy_metadata.return_value = fields
    body = _github_body(fields)
    body["action"] = "opened"
    body["pull_request"].update(url="https://api.github.com/repos/org/repo/pulls/7", state="open", draft=False)

    await github_app._dispatch_request(body, "pull_request", "opened")

    command.run.assert_awaited_once()
    assert provider.get_repo_settings.call_count == expected_repo_reads


@pytest.mark.asyncio
async def test_github_repository_override_keeps_environment_exclusion(github_repo_policy, monkeypatch):
    settings, provider, command = github_repo_policy
    settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    monkeypatch.setenv("CONFIG__IGNORE_PR_LABELS", '["skip"]')
    provider.get_repo_settings.return_value = b"[config]\nignore_pr_labels=[]"
    body = _github_body(_fields(labels=("skip",)))
    body["action"] = "opened"
    body["pull_request"].update(url="https://api.github.com/repos/org/repo/pulls/7", state="open", draft=False)

    await github_app._dispatch_request(body, "pull_request", "opened")

    provider.get_repo_settings.assert_called_once()
    command.run.assert_not_awaited()
    assert settings.config.ignore_pr_labels == ["skip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_repo_settings", [False, True])
async def test_bitbucket_created_prefilter_preserves_host_only_fast_path(
    ignore_settings, monkeypatch, use_repo_settings
):
    settings = copy.deepcopy(ignore_settings)
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", use_repo_settings)
    settings.set("CONFIG.EXTRA_CONFIG_URL", None)
    settings.set("CONFIG.IGNORE_PR_TITLE", [r"^\[Auto\]"])
    payload = _bitbucket_payload(_fields(title="[Auto] Update"))
    payload["event"] = "pullrequest:created"
    payload["data"]["pullrequest"]["links"] = {"html": {"href": "https://bitbucket.org/org/repo/pull-requests/7"}}
    payload["data"]["actor"].update(type="user", account_id="alice")
    monkeypatch.setattr(bitbucket_app, "global_settings", settings)
    monkeypatch.setattr(bitbucket_app, "_verify_webhook_jwt", lambda *_args: ("client", "secret"))
    bearer = AsyncMock(return_value="token")
    monkeypatch.setattr(bitbucket_app, "get_bearer_token", bearer)
    monkeypatch.setattr(bitbucket_app, "get_identity_provider", lambda: SimpleNamespace(
        verify_eligibility=lambda *_args: Eligibility.ELIGIBLE))
    dispatch = AsyncMock()
    monkeypatch.setattr(bitbucket_app, "_perform_commands_bitbucket", dispatch)
    request = SimpleNamespace(headers={"authorization": "JWT token"}, json=AsyncMock(return_value=payload))
    background = BackgroundTasks()
    with request_cycle_context({"settings": settings, "git_provider": {}}):
        await bitbucket_app.handle_github_webhooks(background, request)
        await background()
    assert bearer.await_count == int(use_repo_settings)
    assert dispatch.await_count == int(use_repo_settings)


@pytest.mark.asyncio
async def test_github_closed_analytics_keeps_host_exclusions(ignore_settings, monkeypatch):
    settings = copy.deepcopy(ignore_settings)
    settings.set("CONFIG.USE_REPO_SETTINGS_FILE", True)
    settings.set("CONFIG.ANALYTICS_FOLDER", "analytics")
    settings.set("CONFIG.IGNORE_PR_LABELS", ["skip"])
    body = _github_body(_fields(labels=("skip",)))
    body["action"] = "closed"
    body["pull_request"].update(state="closed", merged=True)
    analytics = Mock()
    monkeypatch.setattr(github_app, "handle_closed_pr", analytics)
    with request_cycle_context({"settings": settings}):
        await github_app._dispatch_request(body, "pull_request", "closed")
    analytics.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [None, []])
async def test_gitlab_prefilter_ignores_malformed_merge_request_attributes(
    gitlab_webhook_module, ignore_settings, monkeypatch, attributes
):
    payload = _gitlab_payload(_fields())
    payload.update(object_kind="merge_request", object_attributes=attributes)
    dispatch = AsyncMock()
    monkeypatch.setattr(gitlab_webhook_module, "_perform_commands_gitlab", dispatch)
    await gitlab_webhook_module.process_gitlab_event(payload, {})
    dispatch.assert_not_awaited()


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


@pytest.mark.parametrize("setting", IGNORE_KEYS)
def test_empty_scalar_ignore_setting_does_not_filter_any_pr(providers, ignore_settings, setting):
    ignore_settings.set(setting, "")
    _assert_all(providers, _fields(labels=("ready",)), True, f"{setting} is disabled when empty")


def test_an_invalid_pattern_does_not_filter_out_every_pull_request(providers, ignore_settings):
    """Process the PR when a rule cannot be evaluated; the opposite silently disables the agent."""
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", ["("])
    _assert_all(providers, _fields(), True, "an unparsable ignore_pr_title regex")


def test_shared_helper_defaults_to_processing(ignore_settings):
    assert shared_should_process_pr_logic() is True
