"""Write-only editor for `pr_agent/settings/.secrets.toml`.

This is deliberately NOT the config editor. The generic editor previews a diff of the file
and keeps a plaintext backup of the previous content, both of which would put a credential
on a rendered page and on disk in a second place. `.secrets.toml` therefore stays excluded
from `config_files.discover`, and this module is the only path that touches it:

- values are written, never read back to the interface -- `status()` reports set / not set;
- there is no diff, no preview token and no backup, because a re-enterable secret is not
  worth a second plaintext copy;
- the file is created 0600 and kept 0600, and is written atomically so a crash cannot leave
  a half-written credential file behind;
- keys this module does not manage, and the comments around them, survive a write.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

import tomlkit

from pr_agent import config_loader
from pr_agent.config_loader import global_settings

# The one file pr-agent itself loads. Deriving it from the package rather than from the
# dashboard's repo_root matters: Dynaconf reads this exact path, so writing anywhere else
# would leave a credential on disk that nothing reads.
SECRETS_PATH = Path(config_loader.__file__).resolve().parent / "settings" / ".secrets.toml"


class SecretsError(ValueError):
    """Raised when a submitted field is unknown or the secrets file cannot be written."""


@dataclass(frozen=True)
class SecretField:
    """One credential the dashboard can set. ``key`` is the dotted pr-agent setting path."""

    key: str
    label: str
    help: str

    @property
    def name(self) -> str:
        """The form field name: dots do not survive a round trip through every form parser."""
        return self.key.replace(".", "__")

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def option(self) -> str:
        return self.key.split(".", 1)[1]


# Only credentials the dashboard itself needs to do its job: reach the two supported
# providers, and run a model. Every key here must also be in redaction's inventory, or a
# token entered through this page would not be redacted out of a run log -- there is a test.
FIELDS: tuple[SecretField, ...] = (
    SecretField("github.user_token", "GitHub personal access token",
                "Read pull requests and PR-Agent's comments on github.com. Needs repo scope."),
    SecretField("bitbucket.bearer_token", "Bitbucket access token",
                "Used when bitbucket.auth_type is bearer, which is the default."),
    SecretField("bitbucket.basic_token", "Bitbucket basic token",
                "Base64 of username:app_password. Used only when bitbucket.auth_type is basic."),
    SecretField("openai.key", "OpenAI API key", "Needed when the configured model is an OpenAI model."),
    SecretField("anthropic.key", "Anthropic API key", "Needed when the configured model is a Claude model."),
    SecretField("google_ai_studio.gemini_api_key", "Google AI Studio key",
                "Needed when the configured model is a Gemini model."),
)

_BY_NAME = {field.name: field for field in FIELDS}


def field_for(name: str) -> SecretField:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise SecretsError(f"unknown secret field {name!r}") from None


def _load_document(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    if not path.is_file() or path.is_symlink():
        # Same rule as the config editor: a FIFO would hang the request thread on read, and a
        # symlink would write the credential somewhere the user did not name.
        raise SecretsError(f"{path} is not a regular file")
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:  # tomlkit raises several distinct parse errors
        raise SecretsError(f"{path} is not valid TOML: {exc}") from exc


def status() -> dict[str, bool]:
    """Report which managed keys have a non-empty value. Never returns a value itself."""
    document = _load_document(SECRETS_PATH)
    result: dict[str, bool] = {}
    for field in FIELDS:
        table = document.get(field.section)
        value = table.get(field.option) if hasattr(table, "get") else None
        result[field.key] = isinstance(value, str) and bool(value.strip())
    return result


def _atomic_write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically, 0600 whether or not it already existed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Explicit, not copymode: an existing file that is somehow 0644 must be tightened by
        # this write rather than have its permissions preserved.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def apply(submitted: Mapping[str, str], clears: Optional[Iterable[str]] = None) -> list[str]:
    """Set every non-empty submitted value, clear every named key, and return what changed.

    A blank field is "leave it alone", not "delete it": the form cannot show the current
    value, so submitting the page must never wipe a credential the user could not see.
    Deleting is the explicit `clear` checkbox.
    """
    path = SECRETS_PATH
    document = _load_document(path)
    changed: list[str] = []

    for name in clears or ():
        field = field_for(name)
        table = document.get(field.section)
        if hasattr(table, "get") and field.option in table:
            del table[field.option]
            changed.append(f"{field.key} cleared")
            if not len(table):
                del document[field.section]

    for name, raw in submitted.items():
        if name not in _BY_NAME:
            continue
        value = raw.strip()
        if not value:
            continue
        field = _BY_NAME[name]
        table = document.get(field.section)
        if not hasattr(table, "get"):
            table = tomlkit.table()
            document[field.section] = table
        table[field.option] = value
        changed.append(f"{field.key} set")

    if not changed:
        return []

    _atomic_write_private(path, tomlkit.dumps(document))
    reload_settings()
    return changed


def reload_settings() -> None:
    """Re-read pr-agent's settings so a credential works without restarting the dashboard.

    `get_settings()` hands back a module-level Dynaconf object built at import time, so
    without this the token is on disk but `providers.credential_status` still reports the
    repository as unreachable, and `redaction.secret_values()` still does not know the new
    value -- which would leave it unredacted in a run log.
    """
    global_settings.reload()
