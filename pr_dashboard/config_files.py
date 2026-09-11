"""Discover editable configuration and prompt files and refuse unsafe write targets."""
from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

APPROVED_ROOTS: tuple[Path, ...] = ()

_discovered: list["ConfigFile"] = []


def _excluded_from_discovery(path: Path) -> bool:
    """Paths that must never appear in the editable set."""
    return path.name == ".secrets.toml" or "settings_prod" in path.parts


class ConfigFileError(ValueError):
    """Raised when a config file path is unsafe, unknown, or out of range."""


@dataclass(frozen=True)
class ConfigFile:
    index: int
    path: Path
    group: str
    is_prompt: bool


def discover(repo_root: Path) -> list[ConfigFile]:
    """Glob the editable set for a repository, excluding secrets and settings_prod."""
    global APPROVED_ROOTS
    repo_root = repo_root.resolve()
    settings_dir = repo_root / "pr_agent" / "settings"
    APPROVED_ROOTS = (repo_root, settings_dir.resolve())

    entries: list[ConfigFile] = []
    index = 0

    entries.append(
        ConfigFile(index=index, path=repo_root / ".pr_agent.toml", group="repository", is_prompt=False)
    )
    index += 1

    configuration = settings_dir / "configuration.toml"
    if configuration.is_file():
        entries.append(
            ConfigFile(index=index, path=configuration, group="defaults", is_prompt=False)
        )
        index += 1

    if settings_dir.is_dir():
        for path in sorted(settings_dir.rglob("*_prompts.toml")):
            if _excluded_from_discovery(path):
                continue
            entries.append(ConfigFile(index=index, path=path, group="prompts", is_prompt=True))
            index += 1

        code_suggestions = settings_dir / "code_suggestions"
        if code_suggestions.is_dir():
            for path in sorted(code_suggestions.glob("*.toml")):
                if _excluded_from_discovery(path):
                    continue
                entries.append(ConfigFile(index=index, path=path, group="prompts", is_prompt=True))
                index += 1

    entries = [entry for entry in entries if not _excluded_from_discovery(entry.path)]
    entries = [
        ConfigFile(index=idx, path=entry.path, group=entry.group, is_prompt=entry.is_prompt)
        for idx, entry in enumerate(entries)
    ]

    global _discovered
    _discovered = entries
    return entries


def resolve(index: int) -> ConfigFile:
    """Return the config file at ``index`` from the last ``discover`` call."""
    if index < 0 or index >= len(_discovered):
        raise ConfigFileError(f"config file index {index} out of range")
    return _discovered[index]


def file_identity(path: Path) -> tuple[int, int]:
    """Return ``(st_dev, st_ino)`` for a regular file's identity check at write time."""
    info = path.stat()
    return (info.st_dev, info.st_ino)


def _assert_beneath_approved_root(resolved: Path, described: Path) -> None:
    if not any(resolved.is_relative_to(root) for root in APPROVED_ROOTS):
        raise ConfigFileError(f"{described} resolves outside the approved roots")


def assert_safe_target(path: Path) -> None:
    """Refuse anything but a regular file, or a creatable path, beneath an approved root."""
    # lstat, not stat: stat follows the link and would happily report the *target* as a
    # regular file, which is exactly the case being rejected. A discovered
    # `.pr_agent.toml -> ~/.ssh/config` is a legitimate discovery result whose target must
    # never be written.
    try:
        info = path.lstat()
    except FileNotFoundError:
        # `.pr_agent.toml` is in the editable set whether or not it exists yet -- the spec
        # has it "created if absent" -- so an absent path is legitimate rather than an
        # error. There is no file to type-check, so the approved-root rule is applied to the
        # directory the file would be created in, which is the traversal surface here.
        _assert_beneath_approved_root(path.parent.resolve(), path)
        return
    if not stat.S_ISREG(info.st_mode):
        raise ConfigFileError(f"{path} is not a regular file")
    _assert_beneath_approved_root(path.resolve(), path)
