"""GitLab settings hierarchy and credential-scoped cache tests; no network calls."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from gitlab.exceptions import GitlabGetError
from requests.exceptions import RequestException

from pr_agent.git_providers import git_provider, gitlab_provider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    config = SimpleNamespace(use_global_settings_file=True, global_settings_repo="pr-agent-settings")
    settings = SimpleNamespace(config=config)
    monkeypatch.setattr(git_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gitlab_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gitlab_provider, "get_config_branch", lambda: "")
    git_provider._GLOBAL_SETTINGS_CACHE.clear()
    yield config
    git_provider._GLOBAL_SETTINGS_CACHE.clear()


def project(contents, branch="settings-default"):
    result = Mock(default_branch=branch)
    result.files.get.return_value.decode.return_value = contents
    return result


def provider(path="org/parent/child/app", host="https://gitlab.example", token="test-token"):
    result = GitLabProvider.__new__(GitLabProvider)
    result.id_project = path
    result.gitlab_url = host
    result.gl = SimpleNamespace(oauth_token=token, private_token=None, job_token=None, projects=Mock())
    result.gl.projects.get.side_effect = GitlabGetError("missing", response_code=404)
    return result


def route(instance, projects):
    def lookup(path, **kwargs):
        result = projects.get(str(path))
        if result is None:
            raise GitlabGetError("missing", response_code=404)
        if isinstance(result, Exception):
            raise result
        return result
    instance.gl.projects.get.side_effect = lookup


def test_returns_global_closest_group_local_in_order_without_merging_intermediate():
    instance = provider()
    top = project(b"global")
    closest = project(b"closest")
    intermediate = project(b"must-not-merge")
    local = project(b"local", "app-default")
    route(instance, {
        "org/pr-agent-settings": top,
        "org/parent/pr-agent-settings": intermediate,
        "org/parent/child/pr-agent-settings": closest,
        instance.id_project: local,
    })

    assert instance.get_repo_settings() == [("global", b"global"), ("group", b"closest"), ("local", b"local")]
    intermediate.files.get.assert_not_called()
    assert call("org/parent/pr-agent-settings") not in instance.gl.projects.get.call_args_list
    top.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="settings-default")
    closest.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="settings-default")
    local.files.get.assert_called_once_with(file_path=".pr_agent.toml", ref="app-default")


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("missing", ["project", "file"])
def test_missing_or_inaccessible_closest_falls_back_to_nearest_existing_ancestor(status, missing):
    instance = provider()
    error = GitlabGetError("not available", response_code=status)
    closest = error if missing == "project" else project(b"unused")
    if missing == "file":
        closest.files.get.side_effect = error
    route(instance, {
        "org/parent/child/pr-agent-settings": closest,
        "org/parent/pr-agent-settings": project(b"parent"),
    })

    assert instance._get_group_repo_settings() == b"parent"
    assert instance._get_group_repo_settings() == b"parent"
    assert instance.gl.projects.get.call_args_list == [
        call("org/parent/child/pr-agent-settings"), call("org/parent/pr-agent-settings")]


def test_empty_existing_closest_file_stops_search():
    instance = provider()
    route(instance, {
        "org/parent/child/pr-agent-settings": project(b""),
        "org/parent/pr-agent-settings": project(b"must-not-apply"),
    })
    assert instance._get_group_repo_settings() == b""
    assert instance._get_group_repo_settings() == b""
    instance.gl.projects.get.assert_called_once_with("org/parent/child/pr-agent-settings")


def test_absent_subgroups_do_not_fetch_top_level_again():
    instance = provider()
    assert instance._get_group_repo_settings() == ""
    assert instance.gl.projects.get.call_args_list == [
        call("org/parent/child/pr-agent-settings"), call("org/parent/pr-agent-settings")]


def test_single_group_project_keeps_existing_global_local_tiers():
    instance = provider(path="org/app")
    route(instance, {"org/pr-agent-settings": project(b"global"), "org/app": project(b"local")})
    assert instance.get_repo_settings() == [("global", b"global"), ("local", b"local")]
    assert instance.gl.projects.get.call_args_list == [
        call("org/pr-agent-settings"), call("org/app", lazy=True), call("org/app")]


@pytest.mark.parametrize("project_id", [42, "42"])
def test_numeric_ids_resolve_canonical_path_for_both_tiers(project_id):
    instance = provider(path=project_id)
    route(instance, {
        "42": SimpleNamespace(path_with_namespace="org/parent/child/app"),
        "org/pr-agent-settings": project(b"global"),
        "org/parent/child/pr-agent-settings": project(b"group"),
    })
    assert instance._get_global_repo_settings() == b"global"
    assert instance._get_group_repo_settings() == b"group"
    assert instance.get_owning_namespace() == "org"
    assert call("42/pr-agent-settings") not in instance.gl.projects.get.call_args_list


def test_unresolvable_numeric_id_skips_optional_group_settings():
    instance = provider(path=42)
    assert instance._get_group_repo_settings() == ""
    instance.gl.projects.get.assert_called_once_with("42")


@pytest.mark.parametrize("enabled,repo", [(False, "pr-agent-settings"), (True, "")])
def test_gate_and_empty_default_disable_both_tiers_but_not_local(settings, enabled, repo):
    settings.use_global_settings_file = enabled
    settings.global_settings_repo = repo
    instance = provider()
    route(instance, {instance.id_project: project(b"local")})
    assert instance.get_repo_settings() == [("local", b"local")]
    assert instance.gl.projects.get.call_args_list == [
        call(instance.id_project, lazy=True), call(instance.id_project)]


@pytest.mark.parametrize("tier", ["_get_global_repo_settings", "_get_group_repo_settings"])
@pytest.mark.parametrize("changed", ["host", "group", "repo", "token", "auth_type"])
def test_cache_isolates_settings_projects_and_credentials(settings, tier, changed):
    first = provider()
    first.gl.projects.get.side_effect = None
    first.gl.projects.get.return_value = project(b"first")
    assert getattr(first, tier)() == b"first"

    second = provider()
    if changed == "host":
        second.gitlab_url = "https://other.example"
    elif changed == "group":
        second.id_project = "other/parent/child/app" if tier == "_get_global_repo_settings" else "org/other/child/app"
    elif changed == "repo":
        settings.global_settings_repo = "other-settings"
    elif changed == "token":
        second.gl.oauth_token = "other-token"
    else:
        second.gl.private_token, second.gl.oauth_token = second.gl.oauth_token, None
    second.gl.projects.get.side_effect = None
    second.gl.projects.get.return_value = project(b"second")
    assert getattr(second, tier)() == b"second"
    assert len(git_provider._GLOBAL_SETTINGS_CACHE) == 2
    assert all("test-token" not in key and "other-token" not in key for key in git_provider._GLOBAL_SETTINGS_CACHE)


@pytest.mark.parametrize("tier", ["_get_global_repo_settings", "_get_group_repo_settings"])
def test_same_credential_reuses_cache_across_provider_instances_until_ttl(monkeypatch, tier):
    now = [100.0]
    monkeypatch.setattr(git_provider.time, "monotonic", lambda: now[0])
    first = provider()
    first.gl.projects.get.side_effect = None
    first.gl.projects.get.return_value = project(b"first")
    assert getattr(first, tier)() == b"first"
    second = provider()
    second.gl.projects.get.side_effect = None
    second.gl.projects.get.return_value = project(b"refreshed")
    assert getattr(second, tier)() == b"first"
    second.gl.projects.get.assert_not_called()
    now[0] += git_provider._GLOBAL_SETTINGS_CACHE_TTL_SECONDS + 1
    assert getattr(second, tier)() == b"refreshed"
    assert second.gl.projects.get.call_count == 1


@pytest.mark.parametrize("token", [None, "", Mock()])
@pytest.mark.parametrize("tier", ["_get_global_repo_settings", "_get_group_repo_settings"])
def test_unknown_credential_scope_bypasses_cache(token, tier):
    instance = provider(token=token)
    instance.gl.projects.get.side_effect = None
    instance.gl.projects.get.return_value = project(b"settings")
    assert getattr(instance, tier)() == b"settings"
    assert getattr(instance, tier)() == b"settings"
    assert instance.gl.projects.get.call_count == 2
    assert not git_provider._GLOBAL_SETTINGS_CACHE


@pytest.mark.parametrize("tier", ["_get_global_repo_settings", "_get_group_repo_settings"])
@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("failure_at", ["project", "file"])
def test_transient_failures_are_optional_but_not_cached(tier, status, failure_at):
    instance = provider(path="org/child/app")
    settings_project = project(b"recovered")
    error = GitlabGetError("temporary", response_code=status)
    if failure_at == "project":
        instance.gl.projects.get.side_effect = [error, settings_project]
    else:
        instance.gl.projects.get.side_effect = None
        instance.gl.projects.get.return_value = settings_project
        settings_project.files.get.side_effect = [error, Mock(decode=lambda: b"recovered")]
    assert getattr(instance, tier)() == ""
    assert not git_provider._GLOBAL_SETTINGS_CACHE
    assert getattr(instance, tier)() == b"recovered"
    assert instance.gl.projects.get.call_count == 2


def test_request_failure_retries_and_closest_transient_falls_back_best_effort():
    instance = provider()
    closest = project(b"recovered")
    calls = [RequestException("unavailable"), closest]
    parent = project(b"parent")

    def lookup(path):
        if path == "org/parent/pr-agent-settings":
            return parent
        result = calls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    instance.gl.projects.get.side_effect = lookup
    assert instance._get_group_repo_settings() == b"parent"
    assert instance._get_group_repo_settings() == b"recovered"
    assert instance.gl.projects.get.call_args_list == [
        call("org/parent/child/pr-agent-settings"), call("org/parent/pr-agent-settings"),
        call("org/parent/child/pr-agent-settings")]


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("failure_at", ["project", "file"])
def test_global_absence_is_cached_without_poisoning_other_credentials(status, failure_at):
    denied = provider()
    error = GitlabGetError("not available", response_code=status)
    if failure_at == "project":
        denied.gl.projects.get.side_effect = error
    else:
        settings_project = project(b"unused")
        settings_project.files.get.side_effect = error
        denied.gl.projects.get.side_effect = None
        denied.gl.projects.get.return_value = settings_project
    assert denied._get_global_repo_settings() == ""
    assert denied._get_global_repo_settings() == ""
    assert denied.gl.projects.get.call_count == 1

    allowed = provider(token="allowed-token")
    allowed.gl.projects.get.side_effect = None
    allowed.gl.projects.get.return_value = project(b"allowed")
    assert allowed._get_global_repo_settings() == b"allowed"
