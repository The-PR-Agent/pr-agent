"""Validate, preview, and apply configuration edits with atomic replace and backups."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tomlkit
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError

from pr_dashboard import config_files
from pr_dashboard.config_files import ConfigFile, assert_safe_target, file_identity

BACKUPS_DIR = Path.home() / ".pr_dashboard" / "backups"
TOKEN_TTL = timedelta(minutes=15)

_path_locks: defaultdict[Path, threading.Lock] = defaultdict(threading.Lock)
_tokens: dict[str, "_PreviewTokenState"] = {}


class ConfigWriteError(ValueError):
    """Raised when validation, preview confirmation, or restore fails."""


@dataclass
class _PreviewTokenState:
    target: Path
    identity: tuple[int, int] | None
    original_hash: str
    submitted_hash: str
    is_prompt: bool
    expires_at: datetime
    spent: bool = False


class PreviewToken:
    """Opaque single-use confirmation token minted by ``make_preview``."""

    def __init__(self, value: str) -> None:
        self.value = value


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repo_root() -> Path:
    if not config_files.APPROVED_ROOTS:
        raise ConfigWriteError("no approved roots; call discover() first")
    return config_files.APPROVED_ROOTS[0]


def _relative_backup_path(path: Path) -> str:
    resolved = path.resolve()
    return str(resolved.relative_to(_repo_root()))


def validate(text: str, is_prompt: bool) -> None:
    """Parse TOML and, for prompt files, compile the text as a Jinja2 template."""
    try:
        tomlkit.parse(text)
    except Exception as exc:
        raise ConfigWriteError(f"invalid TOML: {exc}") from exc
    if is_prompt:
        env = Environment(undefined=StrictUndefined)
        try:
            env.from_string(text)
        except TemplateSyntaxError as exc:
            raise ConfigWriteError(f"invalid Jinja2 template: {exc}") from exc


def _read_original(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _preview_identity(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    assert_safe_target(path)
    return file_identity(path)


def make_preview(config_file: ConfigFile, submitted: str) -> tuple[str, PreviewToken]:
    """Validate ``submitted``, return a unified diff and a single-use confirmation token."""
    validate(submitted, config_file.is_prompt)
    path = config_file.path
    original = _read_original(path)
    identity = _preview_identity(path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            submitted.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    token_value = secrets.token_urlsafe(32)
    _tokens[token_value] = _PreviewTokenState(
        target=path.resolve(),
        identity=identity,
        original_hash=_content_hash(original),
        submitted_hash=_content_hash(submitted),
        is_prompt=config_file.is_prompt,
        expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
    )
    return diff, PreviewToken(token_value)


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` atomically so a crash cannot leave a truncated config behind."""
    # Same directory, because os.replace is only atomic within a filesystem. A temp file in
    # /tmp could land on another device and degrade to a copy, reintroducing the torn write.
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            # Creating the file for the first time (`.pr_agent.toml` is in the editable set
            # whether or not it exists). copymode has no source to copy from, so set the
            # mode explicitly rather than leaving mkstemp's private 0600 on a config file.
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        # fsync the directory too: without it the rename itself can be lost on power failure
        # even though the file contents were flushed.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_backup(path: Path, content: str) -> Path:
    """Write a fsynced 0600 backup and metadata before the target is touched."""
    # One directory per backup, never one per timestamp: metadata.json describes exactly one
    # target, so two files backed up in the same second would share a directory and the
    # second would overwrite the first's metadata, orphaning a backup that list_backups can
    # no longer report and restore can no longer find.
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S.%fZ")
    backup_dir = BACKUPS_DIR / f"{timestamp}-{secrets.token_hex(4)}"
    relative = _relative_backup_path(path)
    backup_path = backup_dir / relative
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with backup_path.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(backup_path, 0o600)
    metadata = {
        "target": str(path.resolve()),
        "relative": relative,
        "sha256": _content_hash(content),
        "backup_path": str(backup_path),
    }
    metadata_path = backup_dir / "metadata.json"
    with metadata_path.open("w", encoding="utf-8") as stream:
        json.dump(metadata, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(metadata_path, 0o600)
    dir_fd = os.open(str(backup_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return backup_path


def _load_token(token_value: str, submitted: str) -> _PreviewTokenState:
    state = _tokens.get(token_value)
    if state is None:
        raise ConfigWriteError("unknown preview token")
    if state.spent:
        raise ConfigWriteError("preview token already used")
    if datetime.now(timezone.utc) >= state.expires_at:
        raise ConfigWriteError("preview token expired")
    if _content_hash(submitted) != state.submitted_hash:
        raise ConfigWriteError("submitted content does not match preview")
    return state


def _check_target_unchanged(path: Path, state: _PreviewTokenState) -> None:
    original = _read_original(path)
    if _content_hash(original) != state.original_hash:
        raise ConfigWriteError("file changed since preview; redo the preview")
    if state.identity is None:
        if path.exists():
            raise ConfigWriteError("file changed since preview; redo the preview")
        return
    if not path.exists():
        raise ConfigWriteError("file changed since preview; redo the preview")
    current_identity = file_identity(path)
    if current_identity != state.identity:
        raise ConfigWriteError("file changed since preview; redo the preview")


def apply(token_value: str, submitted: str) -> Path:
    """Apply a confirmed edit after re-checking the preview token and target safety."""
    # The target comes from server-side token state alone. Taking a caller-supplied
    # ConfigFile and comparing it to the token would be safe only for as long as nobody
    # weakens the comparison; there is no parameter to guard if there is no parameter.
    state = _load_token(token_value, submitted)
    path = state.target
    validate(submitted, state.is_prompt)
    lock = _path_locks[path]
    with lock:
        _check_target_unchanged(path, state)
        assert_safe_target(path)
        original = _read_original(path)
        backup_path = _write_backup(path, original)
        _atomic_write(path, submitted)
        state.spent = True
    return backup_path


def list_backups() -> list[dict]:
    """Return backup metadata dicts, newest first."""
    if not BACKUPS_DIR.is_dir():
        return []
    entries: list[dict] = []
    for backup_dir in sorted(BACKUPS_DIR.iterdir(), reverse=True):
        if not backup_dir.is_dir():
            continue
        metadata_path = backup_dir / "metadata.json"
        if not metadata_path.is_file():
            continue
        with metadata_path.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        metadata["backup_id"] = backup_dir.name
        entries.append(metadata)
    return entries


def restore(backup_id: str) -> None:
    """Restore a backup after verifying its recorded content hash."""
    backup_dir = BACKUPS_DIR / backup_id
    metadata_path = backup_dir / "metadata.json"
    if not metadata_path.is_file():
        raise ConfigWriteError(f"unknown backup {backup_id!r}")
    with metadata_path.open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    backup_path = Path(metadata["backup_path"])
    target = Path(metadata["target"])
    expected_hash = metadata["sha256"]
    content = backup_path.read_text(encoding="utf-8")
    if _content_hash(content) != expected_hash:
        raise ConfigWriteError("backup hash does not verify")
    lock = _path_locks[target.resolve()]
    with lock:
        assert_safe_target(target)
        # A restore is a write like any other, so it takes its own backup first. Without
        # this, restoring silently destroys whatever is in the file now -- the one edit in
        # the whole pipeline that would have no way back.
        _write_backup(target, _read_original(target))
        _atomic_write(target, content)
