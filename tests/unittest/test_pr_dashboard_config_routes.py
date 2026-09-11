"""Tests for dashboard configuration editor routes — never write outside discovery."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from pr_dashboard import app as app_module
from pr_dashboard import config_files, config_writer

_ORIGIN = "http://127.0.0.1"
_SAFE_HEADERS = {"Origin": _ORIGIN}
_PREVIEW_TOKEN_RE = re.compile(r'name="preview_token"\s+value="([^"]+)"')


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
    (settings / ".secrets.toml").write_text('token = "super-secret-value"\n', encoding="utf-8")
    return tmp_path


def _client(tmp_path, monkeypatch, repo: Path | None = None):
    repo_root = repo or _make_repo(tmp_path)
    monkeypatch.setattr(config_writer, "BACKUPS_DIR", tmp_path / "backups")
    application = app_module.create_app(
        registry_path=tmp_path / "pr_dashboard.toml",
        db_path=tmp_path / "usage.db",
        repo_root=repo_root,
    )
    return TestClient(application, base_url=_ORIGIN), repo_root


def _csrf_token(client, path="/config"):
    response = client.get(path)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, f"csrf_token input missing from {path}"
    return match.group(1)


def _config_index(repo: Path, name: str) -> int:
    for entry in config_files.discover(repo):
        if entry.path.name == name:
            return entry.index
    raise AssertionError(f"{name} not discovered")


def _post_preview(client, index: int, content: str):
    csrf = _csrf_token(client, path=f"/config/{index}")
    return client.post(
        f"/config/{index}/preview",
        data={"csrf_token": csrf, "content": content},
        headers=_SAFE_HEADERS,
    )


def _post_apply(client, index: int, content: str, preview_token: str):
    csrf = _csrf_token(client, path=f"/config/{index}")
    return client.post(
        f"/config/{index}",
        data={"csrf_token": csrf, "content": content, "preview_token": preview_token},
        headers=_SAFE_HEADERS,
    )


class TestConfigList:
    def test_listing_never_shows_secrets_toml(self, tmp_path, monkeypatch):
        """The configuration listing never renders `.secrets.toml` on the page"""
        client, _repo = _client(tmp_path, monkeypatch)
        response = client.get("/config")
        assert response.status_code == 200
        assert ".secrets.toml" not in response.text
        assert "configuration.toml" in response.text


class TestConfigBackupsRouteOrder:
    def test_get_config_backups_returns_200(self, tmp_path, monkeypatch):
        """GET /config/backups returns 200 and is not swallowed by the index route"""
        client, _repo = _client(tmp_path, monkeypatch)
        response = client.get("/config/backups")
        assert response.status_code == 200
        assert "Configuration backups" in response.text


class TestConfigPreview:
    def test_preview_returns_diff_and_token(self, tmp_path, monkeypatch):
        """POST preview renders a unified diff and a confirmation token"""
        client, repo = _client(tmp_path, monkeypatch)
        index = _config_index(repo, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 42\n\n[other]\nvalue = 2\n"
        response = _post_preview(client, index, submitted)
        assert response.status_code == 200
        assert "@@" in response.text
        assert _PREVIEW_TOKEN_RE.search(response.text)


class TestConfigApply:
    def test_apply_with_token_writes_file_and_creates_backup(self, tmp_path, monkeypatch):
        """Applying a preview token writes the file and records a backup"""
        client, repo = _client(tmp_path, monkeypatch)
        index = _config_index(repo, "configuration.toml")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        submitted = "# keep this comment\n[config]\nkey = 99\n\n[other]\nvalue = 2\n"
        preview = _post_preview(client, index, submitted)
        token = _PREVIEW_TOKEN_RE.search(preview.text).group(1)
        before_backups = len(config_writer.list_backups())
        apply_response = _post_apply(client, index, submitted, token)
        assert apply_response.status_code == 200
        assert target.read_text(encoding="utf-8") == submitted
        assert len(config_writer.list_backups()) == before_backups + 1

    def test_replaying_preview_token_is_refused_and_file_unchanged(self, tmp_path, monkeypatch):
        """A spent preview token is refused and leaves the on-disk file byte-for-byte unchanged"""
        client, repo = _client(tmp_path, monkeypatch)
        index = _config_index(repo, "configuration.toml")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        submitted = "# keep this comment\n[config]\nkey = 77\n\n[other]\nvalue = 2\n"
        preview = _post_preview(client, index, submitted)
        token = _PREVIEW_TOKEN_RE.search(preview.text).group(1)
        first = _post_apply(client, index, submitted, token)
        assert first.status_code == 200
        after_first = target.read_bytes()
        replay = _post_apply(client, index, submitted, token)
        assert replay.status_code == 200
        assert "already used" in replay.text
        assert target.read_bytes() == after_first


class TestConfigCsrf:
    def test_foreign_origin_post_writes_nothing(self, tmp_path, monkeypatch):
        """A POST with a foreign Origin is refused and the target file bytes are unchanged"""
        client, repo = _client(tmp_path, monkeypatch)
        index = _config_index(repo, "configuration.toml")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        before = target.read_bytes()
        submitted = "# keep this comment\n[config]\nkey = 55\n\n[other]\nvalue = 2\n"
        preview = _post_preview(client, index, submitted)
        token = _PREVIEW_TOKEN_RE.search(preview.text).group(1)
        csrf = _csrf_token(client, path=f"/config/{index}")
        response = client.post(
            f"/config/{index}",
            data={"csrf_token": csrf, "content": submitted, "preview_token": token},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert target.read_bytes() == before


class TestConfigErrors:
    def test_stale_index_renders_message_not_500(self, tmp_path, monkeypatch):
        """An out-of-range index renders a page-level message instead of raising"""
        client, repo = _client(tmp_path, monkeypatch)
        config_files.discover(repo)
        stale = len(config_files.discover(repo))
        response = client.get(f"/config/{stale}")
        assert response.status_code == 200
        assert "out of range" in response.text

    def test_invalid_toml_preview_renders_error_and_writes_nothing(self, tmp_path, monkeypatch):
        """Invalid TOML on preview shows the parse error and leaves the file unchanged"""
        client, repo = _client(tmp_path, monkeypatch)
        index = _config_index(repo, "configuration.toml")
        target = repo / "pr_agent" / "settings" / "configuration.toml"
        before = target.read_bytes()
        response = _post_preview(client, index, "not valid toml [[[")
        assert response.status_code == 200
        assert "invalid TOML" in response.text
        assert target.read_bytes() == before


class TestUnsafeEntryDoesNotCrashTheRoute:
    """assert_safe_target raises ConfigFileError, which is reachable from ordinary input."""

    def test_a_symlink_out_of_the_repo_gets_a_message_not_a_500(self, tmp_path, monkeypatch):
        """The spec's own example -- .pr_agent.toml -> outside -- renders an error page"""
        repo = _make_repo(tmp_path)
        # Outside tmp_path itself: tmp_path IS the repo root here, so a victim beneath it
        # would legitimately be inside an approved root and the test would prove nothing.
        victim = tmp_path.parent / f"{tmp_path.name}-outside" / "victim.toml"
        victim.parent.mkdir(exist_ok=True)
        victim.write_text("[secret]\nvalue = 1\n", encoding="utf-8")
        (repo / ".pr_agent.toml").symlink_to(victim)
        client, repo_root = _client(tmp_path, monkeypatch, repo=repo)
        index = _config_index(repo_root, ".pr_agent.toml")

        response = _post_preview(client, index, "[x]\ny = 2\n")

        assert response.status_code == 200
        # lstat's regular-file check fires before the approved-root check, so this is the
        # message a symlink actually produces -- the point is that it is refused and rendered.
        assert "is not a regular file" in response.text
        # The status code alone would pass a route that rendered the page after writing.
        assert victim.read_text(encoding="utf-8") == "[secret]\nvalue = 1\n"

    def test_a_fifo_entry_gets_a_message_not_a_500(self, tmp_path, monkeypatch):
        """A non-regular file in the editable set is refused at the route, not raised"""
        import os
        repo = _make_repo(tmp_path)
        # A prompt file, not configuration.toml: discovery gates that one on is_file(), which
        # a FIFO already fails, so it would never reach assert_safe_target.
        (repo / "pr_agent" / "settings" / "pr_reviewer_prompts.toml").unlink()
        os.mkfifo(repo / "pr_agent" / "settings" / "pr_reviewer_prompts.toml")
        client, repo_root = _client(tmp_path, monkeypatch, repo=repo)
        index = _config_index(repo_root, "pr_reviewer_prompts.toml")

        response = _post_preview(client, index, "[x]\ny = 2\n")

        assert response.status_code == 200
        assert "not a regular file" in response.text


class TestApplyConfirmation:
    def test_a_successful_write_names_its_backup(self, tmp_path, monkeypatch):
        """After writing, the page says where the previous content went and that nothing committed"""
        client, repo_root = _client(tmp_path, monkeypatch)
        index = _config_index(repo_root, "configuration.toml")
        submitted = "# keep this comment\n[config]\nkey = 42\n\n[other]\nvalue = 2\n"
        preview = _post_preview(client, index, submitted)
        token = _PREVIEW_TOKEN_RE.search(preview.text).group(1)

        response = _post_apply(client, index, submitted, token)

        assert response.status_code == 200
        backups = config_writer.list_backups()
        assert len(backups) == 1
        # Naming the actual backup path is the discriminating half: a hardcoded "saved!"
        # banner would pass an assertion that only looked for success wording.
        assert backups[0]["backup_path"] in response.text
        assert "Nothing was staged or committed" in response.text


class TestBrowserLineEndings:
    """A real textarea posts CRLF; the file on disk must not inherit it."""

    def test_crlf_from_the_editor_is_written_as_lf(self, tmp_path, monkeypatch):
        """Content posted with \r\n lands on disk with \n and no stray carriage returns"""
        client, repo_root = _client(tmp_path, monkeypatch)
        index = _config_index(repo_root, "configuration.toml")
        submitted = "[config]\r\nkey = 42\r\n\r\n[other]\r\nvalue = 2\r\n"

        preview = _post_preview(client, index, submitted)
        token = _PREVIEW_TOKEN_RE.search(preview.text).group(1)
        response = _post_apply(client, index, submitted, token)

        assert response.status_code == 200
        written = (repo_root / "pr_agent" / "settings" / "configuration.toml").read_bytes()
        assert b"\r" not in written
        assert written.decode("utf-8") == "[config]\nkey = 42\n\n[other]\nvalue = 2\n"

    def test_a_crlf_only_change_still_diffs_as_no_change(self, tmp_path, monkeypatch):
        """Re-posting the on-disk text with CRLF endings produces an empty diff, not a rewrite"""
        client, repo_root = _client(tmp_path, monkeypatch)
        index = _config_index(repo_root, "configuration.toml")
        target = repo_root / "pr_agent" / "settings" / "configuration.toml"
        as_crlf = target.read_text(encoding="utf-8").replace("\n", "\r\n")

        response = _post_preview(client, index, as_crlf)

        assert response.status_code == 200
        # Unchanged content cannot produce a confirmation token: there is nothing to write.
        assert "Confirm and write" not in response.text
