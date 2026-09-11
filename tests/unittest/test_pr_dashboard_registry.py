import importlib
from pathlib import Path

import pytest

from pr_dashboard import registry


class TestLazyDefaultPath:
    def test_import_never_calls_path_home(self, monkeypatch):
        """Reloading the module with a broken Path.home() must not raise -- it is import time"""
        def boom():
            raise RuntimeError("HOME is unset and this user has no passwd entry")

        monkeypatch.setattr(Path, "home", boom)
        importlib.reload(registry)  # must not raise: the module body never calls Path.home()

    def test_default_registry_path_is_only_computed_on_access(self, monkeypatch):
        """DEFAULT_REGISTRY_PATH still works as an attribute, computed lazily on that access"""
        def boom():
            raise RuntimeError("HOME is unset and this user has no passwd entry")

        monkeypatch.setattr(Path, "home", boom)
        with pytest.raises(RuntimeError):
            _ = registry.DEFAULT_REGISTRY_PATH


class TestRepoValidation:
    def test_rejects_unknown_provider(self):
        """Only github and bitbucket are accepted, and the message names the value"""
        with pytest.raises(registry.RegistryError) as excinfo:
            registry.Repo(provider="gitlab", slug="o/r").validate()
        assert "gitlab" in str(excinfo.value)

    def test_rejects_slug_without_owner(self):
        """A slug must be owner/name"""
        with pytest.raises(registry.RegistryError):
            registry.Repo(provider="github", slug="block_rush").validate()

    def test_rejects_slug_with_quotes(self):
        """Characters that would need TOML escaping are refused outright"""
        with pytest.raises(registry.RegistryError):
            registry.Repo(provider="github", slug='o/r"evil').validate()

    def test_accepts_dots_and_dashes(self):
        """Real-world repository names with dots and dashes are valid"""
        registry.Repo(provider="bitbucket", slug="my-team/some.repo").validate()


class TestRegistryFile:
    def test_load_missing_file_returns_empty(self, tmp_path):
        """A first run has no registry file and that is not an error"""
        assert registry.load(tmp_path / "absent.toml") == []

    def test_round_trip(self, tmp_path):
        """Saved repositories load back identically and in order"""
        path = tmp_path / "pr_dashboard.toml"
        repos = [
            registry.Repo(provider="github", slug="samer2373/block_rush"),
            registry.Repo(provider="bitbucket", slug="team/service"),
        ]
        registry.save(repos, path)
        assert registry.load(path) == repos

    def test_add_rejects_duplicate(self, tmp_path):
        """The same provider and slug cannot be registered twice"""
        path = tmp_path / "pr_dashboard.toml"
        repo = registry.Repo(provider="github", slug="o/r")
        registry.add(repo, path)
        with pytest.raises(registry.RegistryError):
            registry.add(repo, path)

    def test_remove_unknown_is_an_error(self, tmp_path):
        """Removing something absent reports it instead of silently succeeding"""
        path = tmp_path / "pr_dashboard.toml"
        registry.add(registry.Repo(provider="github", slug="o/r"), path)
        with pytest.raises(registry.RegistryError):
            registry.remove("github", "other/repo", path)

    def test_remove_leaves_the_rest(self, tmp_path):
        """Removing one entry keeps the others"""
        path = tmp_path / "pr_dashboard.toml"
        registry.add(registry.Repo(provider="github", slug="o/one"), path)
        registry.add(registry.Repo(provider="github", slug="o/two"), path)
        remaining = registry.remove("github", "o/one", path)
        assert [r.slug for r in remaining] == ["o/two"]

    def test_load_skips_malformed_entries(self, tmp_path):
        """A hand-edited file with a bad entry loads the good ones and drops the bad"""
        path = tmp_path / "pr_dashboard.toml"
        path.write_text(
            '[[repo]]\nprovider = "github"\nslug = "o/good"\n\n'
            '[[repo]]\nprovider = "gitlab"\nslug = "o/bad"\n',
            encoding="utf-8",
        )
        assert [r.slug for r in registry.load(path)] == ["o/good"]
