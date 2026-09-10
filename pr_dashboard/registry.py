"""The list of repositories the dashboard shows.

The file is entirely machine-owned, so it is written by regenerating it rather than by
round-tripping with tomlkit. That is only safe because every value is validated first:
the provider comes from a fixed set and the slug cannot contain a character that would
need TOML escaping. Credentials never appear here; they stay in .secrets.toml and the
environment, read through pr_agent's get_settings().
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_PROVIDERS = ("github", "bitbucket")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
DEFAULT_REGISTRY_PATH = Path.home() / ".pr_dashboard" / "pr_dashboard.toml"


class RegistryError(ValueError):
    """Raised for an invalid, duplicate, or unknown registry entry."""


@dataclass(frozen=True)
class Repo:
    provider: str
    slug: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.slug}"

    def validate(self) -> "Repo":
        """Return self when valid, else raise RegistryError naming the offending value."""
        if self.provider not in SUPPORTED_PROVIDERS:
            raise RegistryError(
                f"unsupported provider {self.provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
            )
        if not SLUG_PATTERN.match(self.slug):
            raise RegistryError(f"invalid repository slug {self.slug!r}; expected owner/name")
        return self


def load(path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Read the registry, dropping entries a human edit made invalid."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    repos: list[Repo] = []
    for entry in data.get("repo", []):
        if not isinstance(entry, dict):
            continue
        candidate = Repo(provider=str(entry.get("provider", "")), slug=str(entry.get("slug", "")))
        try:
            repos.append(candidate.validate())
        except RegistryError:
            continue
    return repos


def save(repos: list[Repo], path: Path | str = DEFAULT_REGISTRY_PATH) -> None:
    """Rewrite the whole registry file from validated entries."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["# Managed by pr-dashboard. Credentials are NOT stored here.\n"]
    for repo in repos:
        repo.validate()
        blocks.append(f'[[repo]]\nprovider = "{repo.provider}"\nslug = "{repo.slug}"\n')
    path.write_text("\n".join(blocks), encoding="utf-8")


def add(repo: Repo, path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Append a repository, refusing a duplicate."""
    repo.validate()
    repos = load(path)
    if any(existing.key == repo.key for existing in repos):
        raise RegistryError(f"{repo.slug} is already registered for {repo.provider}")
    repos.append(repo)
    save(repos, path)
    return repos


def remove(provider: str, slug: str, path: Path | str = DEFAULT_REGISTRY_PATH) -> list[Repo]:
    """Drop a repository, reporting when it was not registered."""
    repos = load(path)
    remaining = [repo for repo in repos if repo.key != f"{provider}:{slug}"]
    if len(remaining) == len(repos):
        raise RegistryError(f"{slug} is not registered for {provider}")
    save(remaining, path)
    return remaining
