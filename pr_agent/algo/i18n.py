"""Translate the fixed text of PR-Agent comments into the `config.response_language` locale.

Catalogs are TOML files under `pr_agent/settings/i18n/`, named by locale (`en.toml`, `es.toml`) and holding one table
per area. English is the source of every key. Another locale may leave keys out: a key it lacks, or a translation whose
placeholders do not match, falls back to English. Only visible labels are translated; hidden markers and any text the
code matches back stay in the code.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

CATALOG_DIR = Path(__file__).resolve().parent.parent / "settings" / "i18n"
DEFAULT_LOCALE = "en"


@cache
def _shipped_catalogs() -> dict[str, Path]:
    # Paths come from this listing only, so a repository-controlled response_language never selects a file path.
    return {path.stem.lower(): path for path in CATALOG_DIR.glob("*.toml")}


@cache
def _load_catalog(locale: str) -> dict[str, str]:
    """Return the flat `table.key -> text` map of a shipped locale; a broken non-English catalog reads as empty."""
    try:
        with open(_shipped_catalogs()[locale], "rb") as file:
            data = tomllib.load(file)
        return {
            f"{table}.{key}": text
            for table, entries in data.items()
            for key, text in entries.items()
            if isinstance(text, str)
        }
    except Exception as e:
        if locale == DEFAULT_LOCALE:
            raise
        get_logger().warning(f"Failed to load the '{locale}' text catalog; using English: {e}")
        return {}


def locale_chain(language) -> list[str]:
    """Return the shipped locales to try for a language tag, most specific first and always ending in English.

    Tags are matched case-insensitively with `-` or `_` separators, dropping subtags from the end (RFC 4647 lookup):
    `es-AR` tries `es-ar`, then `es`, then `en`. A value that names no shipped locale resolves to English alone.
    """
    shipped = _shipped_catalogs()
    chain = []
    if isinstance(language, str):
        subtags = language.strip().lower().replace("_", "-").split("-", 8)
        for end in range(len(subtags), 0, -1):
            candidate = "-".join(subtags[:end])
            if candidate in shipped and candidate not in chain:
                chain.append(candidate)
    if DEFAULT_LOCALE not in chain:
        chain.append(DEFAULT_LOCALE)
    return chain


def t(key: str, **values) -> str:
    """Return the text for `key` in the current response language, with `{name}` placeholders filled from `values`.

    Values are inserted as strings, so format numbers before passing them. A key missing from the English catalog is
    a programming error and raises `KeyError`.
    """
    language = get_settings().get("config.response_language", DEFAULT_LOCALE)
    replacements = {name: str(value) for name, value in values.items()}
    for locale in locale_chain(language):
        text = _load_catalog(locale).get(key)
        if text is None:
            continue
        try:
            return text.format_map(replacements)
        except (KeyError, IndexError, ValueError, AttributeError) as e:
            if locale == DEFAULT_LOCALE:
                raise
            get_logger().warning(f"Invalid '{key}' text in the '{locale}' catalog; using a fallback: {e}")
    raise KeyError(key)
