"""Tests for dashboard config validation, preview tokens, and atomic writes."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pr_dashboard import config_files, config_writer


def _make_repo(tmp_path: Path) -> Path:
    settings = tmp_path / "pr_agent" / "settings"
    settings.mkdir(parents=True)
    (settings / "configuration.toml").write_text(
        "# keep this comment\n[config]\nkey = 1\n\n[other]\nvalue = 2\n",
        encoding="utf-8",
    )
    (settings / "pr_reviewer_prompts.toml").write_text(
        '[pr_reviewer]\nprompt = "{{ name }}"\n',
        encoding="utf-8",
    )
    return tmp_path


def _config_entry(repo: Path, name: str) -> config_files.ConfigFile:
    config_files.discover(repo)
    entries = config_files.discover(repo)
    for entry in entries:
        if entry.path.name == name:
            return entry
    raise AssertionError(f"{name} not discovered")


class TestValidate:
    def test_invalid_toml_refused_and_file_unchanged(self, tmp_path, monkeypatch):
        """Invalid TOML is refused and the on-disk file is byte-unchanged"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        before = target.read_bytes()
        with pytest.raises(config_writer.ConfigWriteError):
            config_writer.validate("not valid toml [[[", is_prompt=False)
        assert target.read_bytes() == before

    def test_unclosed_jinja_block_refused(self, tmp_path):
        """A prompt with an unclosed Jinja block is refused at validation time"""
        repo = _make_repo(tmp_path)
        bad = '[pr_reviewer]\nprompt = "{% if x %}"\n'
        with pytest.raises(config_writer.ConfigWriteError):
            config_writer.validate(bad, is_prompt=True)


class TestApplyTokens:
    def test_spent_token_refused(self, tmp_path, monkeypatch):
        """A spent preview token is refused on a second apply"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 2\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        config_writer.apply(token.value, submitted)
        with pytest.raises(config_writer.ConfigWriteError, match="already used"):
            config_writer.apply(token.value, submitted)

    def test_expired_token_refused(self, tmp_path, monkeypatch):
        """An expired preview token is refused"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 9\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        state = config_writer._tokens[token.value]
        state.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(config_writer.ConfigWriteError, match="expired"):
            config_writer.apply(token.value, submitted)

    def test_original_hash_mismatch_refused(self, tmp_path, monkeypatch):
        """A token is refused when the original-content hash no longer matches"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 3\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        entry.path.write_text("# keep this comment\n[config]\nkey = 99\n\n[other]\nvalue = 2\n", encoding="utf-8")
        with pytest.raises(config_writer.ConfigWriteError, match="file changed"):
            config_writer.apply(token.value, submitted)

    def test_a_token_writes_only_the_file_it_was_minted_for(self, tmp_path, monkeypatch):
        """apply takes its target from server-side token state, so no other file is touched"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry_a = _config_entry(repo, "configuration.toml")
        entry_b = _config_entry(repo, "pr_reviewer_prompts.toml")
        before_b = entry_b.path.read_bytes()
        submitted = "# keep this comment\n[config]\nkey = 4\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry_a, submitted)

        config_writer.apply(token.value, submitted)

        # The absence assertion is the point: apply has no parameter naming a file, so the
        # only way B could change is a target derived from something other than the token.
        assert entry_b.path.read_bytes() == before_b
        assert entry_a.path.read_text(encoding="utf-8") == submitted


class TestWritePipeline:
    def test_round_trip_preserves_comment_and_section_order(self, tmp_path, monkeypatch):
        """After a write the exact comment text remains and section order is unchanged"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 5\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        config_writer.apply(token.value, submitted)
        text = entry.path.read_text(encoding="utf-8")
        assert "# keep this comment" in text
        assert text.index("[config]") < text.index("[other]")

    def test_backup_content_equals_previous_file(self, tmp_path, monkeypatch):
        """Applying a write creates a backup whose content equals the previous file"""
        repo = _make_repo(tmp_path)
        backups = tmp_path / "backups"
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", backups)
        entry = _config_entry(repo, "configuration.toml")
        before = entry.path.read_text(encoding="utf-8")
        submitted = "# keep this comment\n[config]\nkey = 6\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        backup_path = config_writer.apply(token.value, submitted)
        assert backup_path.read_text(encoding="utf-8") == before

    def test_restore_refuses_bad_backup_hash(self, tmp_path, monkeypatch):
        """restore refuses a backup whose recorded hash does not verify"""
        repo = _make_repo(tmp_path)
        backups = tmp_path / "backups"
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", backups)
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 7\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        config_writer.apply(token.value, submitted)
        listed = config_writer.list_backups()
        backup_id = listed[0]["backup_id"]
        metadata_path = backups / backup_id / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        with pytest.raises(config_writer.ConfigWriteError, match="hash does not verify"):
            config_writer.restore(backup_id)



class TestAtomicWrite:
    def test_temp_file_created_in_target_directory(self, tmp_path, monkeypatch):
        """_atomic_write creates its temp file in the target directory, not /tmp"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        recorded: dict[str, str] = {}
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args, **kwargs):
            if kwargs.get("dir") is not None:
                recorded["dir"] = kwargs["dir"]
            elif len(args) >= 3:
                recorded["dir"] = args[2]
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(config_writer.tempfile, "mkstemp", tracking_mkstemp)
        config_writer._atomic_write(target, target.read_text(encoding="utf-8"))
        assert recorded["dir"] == str(target.parent)

    def test_no_tmp_file_left_after_atomic_write(self, tmp_path, monkeypatch):
        """After _atomic_write no `.tmp` file remains in the target directory"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        config_writer._atomic_write(target, target.read_text(encoding="utf-8"))
        leftovers = list(target.parent.glob("*.tmp"))
        assert leftovers == []


class TestCreatingAnAbsentFile:
    """`.pr_agent.toml` is in the editable set whether or not it exists yet."""

    def test_an_absent_pr_agent_toml_can_be_created(self, tmp_path, monkeypatch):
        """Writing the repository override for the first time creates it rather than raising"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, ".pr_agent.toml")
        assert not entry.path.exists()
        submitted = "[pr_reviewer]\nrequire_tests_review = false\n"

        _, token = config_writer.make_preview(entry, submitted)
        config_writer.apply(token.value, submitted)

        assert entry.path.read_text(encoding="utf-8") == submitted
        # mkstemp creates 0600; a config file the user is expected to read and commit must
        # not silently inherit that.
        assert (entry.path.stat().st_mode & 0o777) == 0o644

    def test_a_path_outside_the_approved_roots_is_still_refused_when_absent(self, tmp_path):
        """The absent-file branch checks the parent directory, so traversal stays closed"""
        repo = _make_repo(tmp_path)
        config_files.discover(repo)
        outside = tmp_path.parent / "not-in-the-repo" / "evil.toml"
        with pytest.raises(config_files.ConfigFileError, match="outside the approved roots"):
            config_files.assert_safe_target(outside)


class TestBackupIsolation:
    def test_two_writes_in_the_same_second_keep_separate_backups(self, tmp_path, monkeypatch):
        """metadata.json is 1:1 with a backup, so a same-second write cannot orphan one"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        for name in ("configuration.toml", "pr_reviewer_prompts.toml"):
            entry = _config_entry(repo, name)
            submitted = entry.path.read_text(encoding="utf-8") + "\nadded = true\n"
            _, token = config_writer.make_preview(entry, submitted)
            config_writer.apply(token.value, submitted)

        backups = config_writer.list_backups()
        assert len(backups) == 2
        assert {Path(b["target"]).name for b in backups} == {
            "configuration.toml", "pr_reviewer_prompts.toml",
        }


class TestRestoreBacksUpWhatItDestroys:
    def test_restore_backs_up_the_content_it_overwrites(self, tmp_path, monkeypatch):
        """Restoring is a write, so the content it replaces is recoverable afterwards"""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
        entry = _config_entry(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 9\n\n[other]\nvalue = 2\n"
        _, token = config_writer.make_preview(entry, submitted)
        config_writer.apply(token.value, submitted)

        doomed = "# keep this comment\n[config]\nkey = 12345\n"
        entry.path.write_text(doomed, encoding="utf-8")
        before = config_writer.list_backups()

        config_writer.restore(before[0]["backup_id"])

        after = config_writer.list_backups()
        assert len(after) == len(before) + 1
        # The content restore destroyed must be readable back out of some backup, or the
        # user has lost it with no way to notice.
        saved = [Path(b["backup_path"]).read_text(encoding="utf-8") for b in after]
        assert doomed in saved
