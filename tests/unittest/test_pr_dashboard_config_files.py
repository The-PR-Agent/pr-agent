"""Tests for dashboard config file discovery and safety checks."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pr_dashboard import config_files


def _make_repo(tmp_path: Path) -> Path:
    settings = tmp_path / "pr_agent" / "settings"
    settings.mkdir(parents=True)
    (settings / "configuration.toml").write_text("[config]\nkey = 1\n", encoding="utf-8")
    (settings / "pr_reviewer_prompts.toml").write_text('[pr_reviewer]\nprompt = "hi"\n', encoding="utf-8")
    code_suggestions = settings / "code_suggestions"
    code_suggestions.mkdir()
    (code_suggestions / "pr_code_suggestions_prompts.toml").write_text("[x]\ny = 1\n", encoding="utf-8")
    return tmp_path


class TestDiscover:
    def test_secrets_toml_never_discovered(self, tmp_path):
        """`.secrets.toml` is never discovered even when present under settings"""
        repo = _make_repo(tmp_path)
        (repo / "pr_agent" / "settings" / ".secrets.toml").write_text("token = \"secret\"\n", encoding="utf-8")
        paths = {entry.path.name for entry in config_files.discover(repo)}
        assert ".secrets.toml" not in paths

    def test_settings_prod_never_discovered(self, tmp_path):
        """`settings_prod/` contents are never discovered"""
        repo = _make_repo(tmp_path)
        prod = repo / "pr_agent" / "settings" / "settings_prod"
        prod.mkdir()
        (prod / "configuration.toml").write_text("[config]\nprod = true\n", encoding="utf-8")
        (prod / "pr_reviewer_prompts.toml").write_text("[pr_reviewer]\nprompt = \"prod\"\n", encoding="utf-8")
        discovered = {entry.path for entry in config_files.discover(repo)}
        assert not any("settings_prod" in path.parts for path in discovered)

    def test_genuine_settings_file_passes_assert_safe_target(self, tmp_path):
        """A regular settings file beneath an approved root passes assert_safe_target"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        config_files.assert_safe_target(target)


class TestAssertSafeTarget:
    def test_symlink_outside_root_refused_and_target_unchanged(self, tmp_path):
        """assert_safe_target refuses a symlink and leaves its target byte-for-byte unchanged"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        outside = tmp_path / "outside.toml"
        original = "outside-secret = \"keep-me\"\n"
        outside.write_text(original, encoding="utf-8")
        link = repo / ".pr_agent.toml"
        link.symlink_to(outside)
        with pytest.raises(config_files.ConfigFileError):
            config_files.assert_safe_target(link)
        assert outside.read_bytes() == original.encode("utf-8")

    def test_fifo_refused(self, tmp_path):
        """A FIFO path is refused by assert_safe_target"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        fifo = repo / "pr_agent" / "settings" / "configuration.toml"
        fifo.unlink()
        os.mkfifo(fifo)
        with pytest.raises(config_files.ConfigFileError):
            config_files.assert_safe_target(fifo)


class TestResolve:
    def test_out_of_range_index_raises(self, tmp_path):
        """resolve on an out-of-range index raises rather than returning a neighbour"""
        repo = _make_repo(tmp_path)
        entries = config_files.discover(repo)
        with pytest.raises(config_files.ConfigFileError):
            config_files.resolve(len(entries))
        with pytest.raises(config_files.ConfigFileError):
            config_files.resolve(-1)
