"""Parse an operator-configured model alias for one ``/review`` command."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pr_agent.algo.utils import ReasoningEffort

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_EFFORT_VALUES = frozenset(effort.value for effort in ReasoningEffort)


@dataclass(frozen=True)
class ReviewModelSelection:
    """One operator-configured model alias and reasoning effort."""

    alias: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class ReviewModelSelectionConfig:
    """Trusted alias controls loaded at command dispatch."""

    enabled: object
    aliases: object


class ReviewModelSelectionError(ValueError):
    """An actionable error caused by an invalid command model selector."""


def _is_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _get_aliases(raw_aliases, skip_invalid: bool = False) -> dict[str, str]:
    raw_aliases = raw_aliases or {}
    if not isinstance(raw_aliases, Mapping):
        if skip_invalid:
            return {}
        raise ReviewModelSelectionError(
            "The operator configuration `pr_reviewer.command_model_aliases` must be a TOML mapping."
        )

    aliases = {}
    for raw_alias, raw_model in raw_aliases.items():
        alias = str(raw_alias).strip().lower()
        if not _ALIAS_RE.fullmatch(alias):
            if skip_invalid:
                continue
            raise ReviewModelSelectionError(
                f"The configured model alias `{raw_alias}` is invalid; use letters, numbers, `.`, `_`, or `-`."
            )
        if not isinstance(raw_model, str) or not raw_model.strip():
            if skip_invalid:
                continue
            raise ReviewModelSelectionError(
                f"The configured model alias `{raw_alias}` must map to a non-empty model identifier."
            )
        aliases[alias] = raw_model.strip()
    return aliases


def _get_configured_alias_names(raw_aliases) -> set[str]:
    if not isinstance(raw_aliases, Mapping):
        return set()
    return {
        alias
        for raw_alias in raw_aliases
        if (alias := str(raw_alias).strip().lower()) and _ALIAS_RE.fullmatch(alias)
    }


def _split_selector(arg: str) -> tuple[str, str]:
    raw_alias, raw_effort = arg.split("+", 1)
    return raw_alias.strip().lower(), raw_effort.strip().lower()


def _is_selector_shaped(arg: str) -> bool:
    if arg.count("+") != 1:
        return False
    _, effort = _split_selector(arg)
    return effort in _EFFORT_VALUES


def parse_review_model_selection(
    args: Sequence[str], config: ReviewModelSelectionConfig
) -> tuple[ReviewModelSelection | None, list[str]]:
    """Extract at most one ``alias+effort`` selector and preserve other arguments."""
    if not any("+" in arg for arg in args):
        return None, list(args)

    selector_tokens = [arg for arg in args if _is_selector_shaped(arg)]
    if not _is_enabled(config.enabled):
        configured_alias_names = _get_configured_alias_names(config.aliases)
        configured_selector_tokens = [
            arg for arg in selector_tokens if _split_selector(arg)[0] in configured_alias_names
        ]
        if configured_selector_tokens:
            raise ReviewModelSelectionError(
                "Per-command model aliases are disabled. Ask an operator to enable "
                "`pr_reviewer.enable_command_model_aliases` in trusted global configuration."
            )
        return None, list(args)

    if selector_tokens:
        aliases = _get_aliases(config.aliases)
        configured_alias_names = set(aliases)
        if not aliases:
            raise ReviewModelSelectionError(
                "No command model aliases are configured. Ask an operator to set "
                "`pr_reviewer.command_model_aliases` in trusted global configuration."
            )
    else:
        aliases = _get_aliases(config.aliases, skip_invalid=True)
        configured_alias_names = _get_configured_alias_names(config.aliases)
    configured_model_ids = {model.lower() for model in aliases.values()}

    selection = None
    remaining_args = []
    valid_efforts = [effort.value for effort in reversed(list(ReasoningEffort))]
    for arg in args:
        if "+" not in arg:
            remaining_args.append(arg)
            continue

        alias, _ = _split_selector(arg)
        if (
            not _is_selector_shaped(arg)
            and alias not in configured_alias_names
            and alias not in configured_model_ids
        ):
            remaining_args.append(arg)
            continue
        if arg.count("+") != 1:
            raise ReviewModelSelectionError(
                f"Malformed model selector `{arg}`. Use exactly `alias+effort`, for example `fable+high`."
            )
        raw_alias, raw_effort = arg.split("+", 1)
        alias = raw_alias.strip().lower()
        effort = raw_effort.strip().lower()
        if "/" in alias or ":" in alias:
            raise ReviewModelSelectionError(
                f"Raw model identifier `{raw_alias}` is not allowed. Use an operator-configured alias instead."
            )
        if alias in configured_alias_names and alias not in aliases:
            _get_aliases(config.aliases)
        if alias not in aliases:
            available = ", ".join(sorted(aliases))
            raise ReviewModelSelectionError(
                f"Unknown model alias `{raw_alias}`. Available aliases: {available}."
            )
        try:
            effort = ReasoningEffort(effort).value
        except ValueError as error:
            raise ReviewModelSelectionError(
                f"Unsupported reasoning effort `{raw_effort}`. Choose one of: {', '.join(valid_efforts)}."
            ) from error
        if selection is not None:
            raise ReviewModelSelectionError(
                "Only one model selector is supported per review. Use `/review alias+effort`."
            )
        selection = ReviewModelSelection(alias=alias, model=aliases[alias], reasoning_effort=effort)

    return selection, remaining_args
