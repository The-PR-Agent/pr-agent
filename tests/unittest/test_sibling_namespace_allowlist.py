from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_security import is_repo_host_only_key
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _provider_with_namespaces(namespaces):
    """A provider whose settings expose repo_context_sibling_namespaces.

    GitProvider is abstract, so the shared helper is exercised through a concrete
    subclass that does not override it.
    """
    assert "is_sibling_namespace_allowed" not in vars(GitLabProvider)
    provider = GitLabProvider.__new__(GitLabProvider)
    settings = MagicMock()
    settings.config.get.side_effect = lambda key, default=None: {
        "repo_context_sibling_namespaces": namespaces,
    }.get(key, default)
    return provider, settings


def _is_allowed(namespaces, sibling, owning, **kwargs):
    provider, settings = _provider_with_namespaces(namespaces)
    with patch("pr_agent.git_providers.git_provider.get_settings", return_value=settings):
        return provider.is_sibling_namespace_allowed(sibling, owning, **kwargs)


class TestIsSiblingNamespaceAllowed:
    def test_own_namespace_is_allowed_without_configuration(self):
        assert _is_allowed([], "myorg", "myorg") is True

    def test_other_namespace_is_rejected_by_default(self):
        assert _is_allowed([], "sharedorg", "myorg") is False

    def test_listed_namespace_is_allowed(self):
        assert _is_allowed(["sharedorg", "myorg"], "sharedorg", "myorg") is True

    def test_unlisted_namespace_stays_rejected_when_others_are_listed(self):
        assert _is_allowed(["sharedorg"], "thirdorg", "myorg") is False

    def test_entries_are_stripped(self):
        assert _is_allowed(["  sharedorg/ "], "sharedorg", "myorg") is True

    def test_empty_sibling_namespace_is_rejected(self):
        assert _is_allowed(["myorg"], "", "myorg") is False

    @pytest.mark.parametrize("allowed", ["sharedorg", None, 42])
    def test_non_list_setting_is_rejected(self, allowed):
        assert _is_allowed(allowed, "sharedorg", "myorg") is False

    @pytest.mark.parametrize("entries", [[None, 7, "", "   "], []])
    def test_malformed_entries_are_skipped(self, entries):
        assert _is_allowed(entries, "sharedorg", "myorg") is False

    def test_case_sensitive_by_default(self):
        assert _is_allowed(["SHAREDORG"], "sharedorg", "myorg") is False
        assert _is_allowed([], "MYORG", "myorg") is False

    def test_case_insensitive_when_requested(self):
        assert _is_allowed(["SHAREDORG"], "sharedorg", "myorg", case_sensitive=False) is True
        assert _is_allowed([], "MYORG", "myorg", case_sensitive=False) is True

    def test_listed_namespace_allowed_without_a_resolvable_owner(self):
        # An unresolvable owning namespace still fails closed unless the host named the
        # sibling namespace explicitly.
        assert _is_allowed([], "sharedorg", None) is False
        assert _is_allowed(["sharedorg"], "sharedorg", None) is True


class TestSubmoduleTargetAllowed:
    """A submodule gitlink is expanded only when the repository allowlist, the namespace
    allowlist and the requester's read access all approve it."""

    def _provider(self, path, namespaces, owning="myorg"):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.get_owning_namespace = MagicMock(return_value=owning)
        provider._requester_can_read_sibling_project = MagicMock(return_value=True)
        project = MagicMock()
        project.path_with_namespace = path
        settings = MagicMock()
        settings.config.get.side_effect = lambda key, default=None: {
            "repo_context_sibling_repos": [path],
            "repo_context_sibling_namespaces": namespaces,
        }.get(key, default)
        return provider, project, settings

    def _allowed(self, path, namespaces, owning="myorg"):
        provider, project, settings = self._provider(path, namespaces, owning)
        with patch("pr_agent.git_providers.git_provider.get_settings", return_value=settings):
            return provider._submodule_target_allowed(project)

    def test_cross_namespace_submodule_rejected_by_default(self):
        assert self._allowed("sharedorg/lib", []) is False

    def test_cross_namespace_submodule_allowed_once_listed(self):
        assert self._allowed("sharedorg/lib", ["sharedorg"]) is True

    def test_same_namespace_submodule_needs_no_entry(self):
        assert self._allowed("myorg/lib", []) is True

    def test_requester_read_access_is_still_required(self):
        provider, project, settings = self._provider("sharedorg/lib", ["sharedorg"])
        provider._requester_can_read_sibling_project = MagicMock(return_value=False)
        with patch("pr_agent.git_providers.git_provider.get_settings", return_value=settings):
            assert provider._submodule_target_allowed(project) is False

    def test_repo_allowlist_is_still_required(self):
        provider = GitLabProvider.__new__(GitLabProvider)
        provider.get_owning_namespace = MagicMock(return_value="myorg")
        provider._requester_can_read_sibling_project = MagicMock(return_value=True)
        project = MagicMock()
        project.path_with_namespace = "sharedorg/lib"
        settings = MagicMock()
        settings.config.get.side_effect = lambda key, default=None: {
            "repo_context_sibling_repos": [],
            "repo_context_sibling_namespaces": ["sharedorg"],
        }.get(key, default)
        with patch("pr_agent.git_providers.git_provider.get_settings", return_value=settings):
            assert provider._submodule_target_allowed(project) is False


def test_setting_is_host_only():
    assert is_repo_host_only_key("config", "repo_context_sibling_namespaces") is True
