"""Tests for the write-only credentials editor.

The property under test throughout is that a stored credential never comes back out: not on
the credentials page, not through the generic config editor, not in a backup, and not in any
other rendered route. Every test points `secrets_editor.SECRETS_PATH` at a tmp file, so the
real `pr_agent/settings/.secrets.toml` is never read or written by the suite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import config_files, config_writer, redaction, secrets_editor

_ORIGIN = "http://127.0.0.1"
_SAFE_HEADERS = {"Origin": _ORIGIN}
_SECRET = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _make_repo(tmp_path: Path) -> Path:
    settings = tmp_path / "pr_agent" / "settings"
    settings.mkdir(parents=True)
    (settings / "configuration.toml").write_text("[config]\nkey = 1\n", encoding="utf-8")
    (settings / "pr_reviewer_prompts.toml").write_text('[pr_reviewer]\nprompt = "{{ x }}"\n', encoding="utf-8")
    return tmp_path


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "settings" / ".secrets.toml"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(secrets_editor, "SECRETS_PATH", path)
    # The real reload re-reads pr-agent's own settings files, which are not these; calling it
    # would be a no-op here and slow, so the wiring is pinned separately by its own test.
    monkeypatch.setattr(secrets_editor, "reload_settings", lambda: None)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch, secrets_file):
    monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml",
        db_path=tmp_path / "usage.db",
        repo_root=_make_repo(tmp_path),
    )
    return TestClient(application, base_url=_ORIGIN)


def _csrf(client, path="/secrets") -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get(path).text)
    assert match, f"csrf_token missing from {path}"
    return match.group(1)


def _save(client, **fields):
    data = {"csrf_token": _csrf(client)}
    data.update(fields)
    return client.post("/secrets", data=data, headers=_SAFE_HEADERS)


class TestWriting:
    def test_a_submitted_token_reaches_the_file_pr_agent_reads(self, client, secrets_file):
        """The value lands under the dotted key pr-agent resolves, not some other spelling"""
        response = _save(client, github__user_token=_SECRET)

        assert response.status_code == 200
        assert f'[github]\nuser_token = "{_SECRET}"' in secrets_file.read_text(encoding="utf-8")

    def test_the_file_is_private_even_if_it_already_was_not(self, client, secrets_file):
        """A pre-existing 0644 secrets file is tightened to 0600 by the write, not preserved"""
        secrets_file.write_text('[openai]\nkey = "old"\n', encoding="utf-8")
        secrets_file.chmod(0o644)

        _save(client, github__user_token=_SECRET)

        assert secrets_file.stat().st_mode & 0o777 == 0o600

    def test_unmanaged_keys_and_comments_survive_a_write(self, client, secrets_file):
        """Writing one key does not rewrite the file: other credentials and comments stay"""
        secrets_file.write_text(
            "# my own note\n[openai]\nkey = \"keep-me\"\n\n[azure_devops.org]\npat = \"keep-me-too\"\n",
            encoding="utf-8",
        )

        _save(client, github__user_token=_SECRET)

        written = secrets_file.read_text(encoding="utf-8")
        assert "# my own note" in written
        assert 'key = "keep-me"' in written
        assert 'pat = "keep-me-too"' in written

    def test_a_blank_field_leaves_the_stored_value_alone(self, client, secrets_file):
        """Submitting the page with an empty box does not wipe a credential the user cannot see"""
        secrets_file.write_text(f'[github]\nuser_token = "{_SECRET}"\n', encoding="utf-8")

        response = _save(client, github__user_token="", openai__key="")

        assert _SECRET in secrets_file.read_text(encoding="utf-8")
        assert "Nothing changed" in response.text

    def test_clear_removes_the_key(self, client, secrets_file):
        """The explicit Clear checkbox is the only way a value is deleted"""
        secrets_file.write_text(f'[github]\nuser_token = "{_SECRET}"\n', encoding="utf-8")

        response = _save(client, clear__github__user_token="1")

        assert _SECRET not in secrets_file.read_text(encoding="utf-8")
        assert "github.user_token cleared" in response.text

    def test_an_unchecked_clear_box_does_not_delete(self, client, secrets_file):
        """An unchecked checkbox posts nothing; a falsy value must not be read as 'clear'"""
        secrets_file.write_text(f'[github]\nuser_token = "{_SECRET}"\n', encoding="utf-8")

        _save(client, clear__github__user_token="")

        assert _SECRET in secrets_file.read_text(encoding="utf-8")

    def test_a_foreign_origin_cannot_write_a_credential(self, client, secrets_file):
        """The origin guard covers this route too, and the file is untouched"""
        secrets_file.write_text('[openai]\nkey = "old"\n', encoding="utf-8")
        before = secrets_file.read_bytes()

        response = client.post(
            "/secrets",
            data={"csrf_token": _csrf(client), "github__user_token": _SECRET},
            headers={"Origin": "http://evil.example"},
        )

        assert response.status_code == 403
        assert secrets_file.read_bytes() == before


class TestNeverEchoed:
    def test_the_page_reports_set_without_showing_the_value(self, client, secrets_file):
        """Status is set/not set; the stored token itself never reaches the page"""
        secrets_file.write_text(f'[github]\nuser_token = "{_SECRET}"\n', encoding="utf-8")

        page = client.get("/secrets").text

        assert _SECRET not in page
        assert "set" in page

    def test_no_route_renders_a_stored_credential(self, client, secrets_file):
        """After saving, the value is absent from every rendered route -- the leak test"""
        _save(client, github__user_token=_SECRET, openai__key=_SECRET)

        paths = ["/secrets", "/config", "/config/backups", "/repos", "/runs", "/usage", "/findings", "/"]
        paths += [f"/config/{entry.index}" for entry in config_files.discover(Path(client.app.state.repo_root))]
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path
            assert _SECRET not in response.text, f"credential rendered by {path}"

    def test_the_confirmation_names_the_key_not_the_value(self, client):
        """'github.user_token set' is the whole message; the token is not in it"""
        response = _save(client, github__user_token=_SECRET)

        assert "github.user_token set" in response.text
        assert _SECRET not in response.text

    def test_saving_writes_no_backup(self, client, secrets_file, tmp_path):
        """The config editor's backup pipeline is not reused: no plaintext copy is made"""
        secrets_file.write_text('[openai]\nkey = "previous-value"\n', encoding="utf-8")

        _save(client, github__user_token=_SECRET)

        backups = tmp_path / "backups"
        copies = list(backups.rglob("*")) if backups.exists() else []
        assert copies == [], f"a credential was copied to {copies}"


class TestStillExcludedFromTheConfigEditor:
    def test_the_secrets_file_is_not_discovered(self, client, tmp_path, secrets_file):
        """Widening discovery to `.secrets.toml` would route credentials through diffs again"""
        # Discovery looks under the repo root, so plant one there as well as at SECRETS_PATH.
        planted = tmp_path / "pr_agent" / "settings" / ".secrets.toml"
        planted.write_text(f'[github]\nuser_token = "{_SECRET}"\n', encoding="utf-8")

        listing = client.get("/config")

        assert ".secrets.toml" not in listing.text
        assert all(entry.path != planted for entry in config_files.discover(tmp_path))


class TestRedactionCoversWhatWeCanSet:
    def test_every_managed_key_is_in_the_redaction_inventory(self):
        """A key settable here but unknown to redaction would show up in a run log"""
        missing = [field.key for field in secrets_editor.FIELDS
                   if field.key not in redaction._SECRET_SETTING_KEYS]
        assert missing == [], f"settable but never redacted: {missing}"


class TestSettingsAreReloaded:
    def test_a_write_reloads_pr_agent_settings(self, tmp_path, monkeypatch):
        """Without the reload the token is on disk but credential_status still reports it missing"""
        path = tmp_path / ".secrets.toml"
        monkeypatch.setattr(secrets_editor, "SECRETS_PATH", path)
        calls: list[str] = []
        # Recorded through the module the writer actually calls, so removing the reload from
        # apply() fails this test rather than a stub of it.
        monkeypatch.setattr(secrets_editor.global_settings, "reload", lambda: calls.append("reload"))

        secrets_editor.apply({"github__user_token": _SECRET})

        assert calls == ["reload"]

    def test_nothing_submitted_means_no_write_and_no_reload(self, tmp_path, monkeypatch):
        """An empty submission must not rewrite the file or churn the settings object"""
        path = tmp_path / ".secrets.toml"
        path.write_text('[openai]\nkey = "old"\n', encoding="utf-8")
        monkeypatch.setattr(secrets_editor, "SECRETS_PATH", path)
        before = path.stat().st_mtime_ns
        calls: list[str] = []
        monkeypatch.setattr(secrets_editor.global_settings, "reload", lambda: calls.append("reload"))

        changed = secrets_editor.apply({"github__user_token": "   "})

        assert changed == []
        assert calls == []
        assert path.stat().st_mtime_ns == before


class TestUnsafeTargets:
    def test_a_symlinked_secrets_file_is_refused(self, client, secrets_file, tmp_path):
        """A symlink would write the credential somewhere the user did not name"""
        victim = tmp_path / "elsewhere.toml"
        victim.write_text("[keep]\nme = 1\n", encoding="utf-8")
        secrets_file.unlink(missing_ok=True)
        secrets_file.symlink_to(victim)

        response = _save(client, github__user_token=_SECRET)

        assert response.status_code == 200
        assert "is not a regular file" in response.text
        assert victim.read_text(encoding="utf-8") == "[keep]\nme = 1\n"


class TestTheFileWeWriteIsTheFilePrAgentLoads:
    def test_secrets_path_is_one_of_dynaconf_settings_files(self):
        """Writing anywhere Dynaconf does not load would store a credential nothing reads"""
        from pr_agent import config_loader

        loaded = {Path(entry).resolve() for entry in config_loader.global_settings.settings_file}
        assert secrets_editor.SECRETS_PATH.resolve() in loaded
