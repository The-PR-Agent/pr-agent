import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import dynaconf

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

DEFAULT_ARTIFACT_INSTRUCTIONS = (
    "Consider this CI artifact as additional context when analyzing the PR. "
    "It was produced by a prior CI step."
)

_MISSING = object()


@dataclass
class _ArtifactContext:
    active: bool = True
    prepared: bool = False
    settings: Any = None
    originals: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    targets: frozenset[str] = frozenset()

    def bind(self, settings):
        if self.settings is None:
            self.remember(settings, "ARTIFACTS")
            self.settings = settings

    def remember(self, settings, key):
        value = settings.get(key, _MISSING)
        self.originals[key] = _MISSING if value is _MISSING else deepcopy(value)


_artifact_context: ContextVar[Optional[_ArtifactContext]] = ContextVar("pr_agent_artifact_context", default=None)


def _restore_artifact_settings(state):
    for key, value in state.originals.items():
        try:
            if "." not in key:
                if value is not _MISSING:
                    state.settings.set(key, value)
                elif key in state.settings:
                    state.settings.unset(key, force=True)
                continue
            section_name, leaf = key.split(".", 1)
            section = state.settings.get(section_name)
            if value is not _MISSING:
                if isinstance(section, dynaconf.DataDict):
                    section[leaf] = value
                else:
                    state.settings.set(section_name, {leaf: value})
            elif isinstance(section, dynaconf.DataDict):
                # Remove the leaf directly because Dynaconf's dotted unset can leave it behind.
                for stored in list(section):
                    if stored.lower() == leaf.lower():
                        section.pop(stored)
                        break
        except Exception as error:
            # Preserve the primary failure and omit setting values and raw exception details.
            phase = "artifact section" if "." not in key else "target instructions"
            get_logger().warning(f"Could not restore {phase} for artifact context ({type(error).__name__})")


@contextmanager
def artifact_context_scope(settings=None):
    """Keep one ingress's prepared artifact through dispatch, then restore its settings.

    Action supplies settings before its first repository merge. CLI binds lazily
    at injection, inside its existing settings-copy scope. Entry never reads a file.
    """
    state = _ArtifactContext()
    if settings is not None:
        state.bind(settings)
    token = _artifact_context.set(state)
    try:
        yield
    finally:
        # Invalidate copied task contexts before restoring settings and resetting the token.
        state.active = False
        try:
            _restore_artifact_settings(state)
        finally:
            _artifact_context.reset(token)


def _append_artifact_context(settings, text, targets):
    separator = "\n======\n\n"
    for key in settings:
        setting = settings.get(key)
        if isinstance(setting, dynaconf.DataDict) and key.lower() in targets and hasattr(setting, "extra_instructions"):
            extra_instructions = str(setting.extra_instructions or "")
            if text not in extra_instructions:
                setting.extra_instructions = extra_instructions + separator + text if extra_instructions else text


def reapply_artifact_context() -> None:
    """Compose already-read context after final command settings, without file I/O."""
    state = _artifact_context.get()
    if state is not None and state.active and state.prepared and state.text and get_settings() is state.settings:
        _append_artifact_context(state.settings, state.text, state.targets)


def resolve_artifact_path(path: str) -> Optional[Path]:
    if not path:
        return None
    try:
        workspace = os.environ.get("GITHUB_WORKSPACE", "")

        artifact_path = Path(path)
        if artifact_path.is_absolute():
            resolved = artifact_path.resolve()
        elif workspace:
            resolved = (Path(workspace) / artifact_path).resolve()
        else:
            resolved = artifact_path.resolve()

        if workspace:
            workspace_resolved = Path(workspace).resolve()
            under_workspace = resolved == workspace_resolved or resolved.is_relative_to(workspace_resolved)
            if not under_workspace:
                get_logger().warning(
                    f"Artifact path '{path}' resolves outside GITHUB_WORKSPACE: {resolved}"
                )
                return None

        return resolved if resolved.is_file() else None
    except OSError as e:
        get_logger().warning(f"Failed to resolve artifact path '{path}': {e}")
        return None


_TRUNCATION_MARKER = "\n\n[... content truncated due to size limit ...]"


def _read_and_truncate(path: Path, max_size: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_size + 1)
    except (OSError, IOError) as e:
        get_logger().warning(f"Failed to read artifact file {path}: {e}")
        return ""

    if len(content) > max_size:
        available = max_size - len(_TRUNCATION_MARKER)
        content = content[:available] + _TRUNCATION_MARKER if available > 0 else content[:max_size]
    return content


def format_artifact_content(content: str, label: str, instructions: str) -> str:
    header = f"CI Artifact: {label}" if label else "CI Artifact"
    instructions = (instructions or "").strip() or DEFAULT_ARTIFACT_INSTRUCTIONS
    return (
        f"{header}\n"
        f"=====\n"
        f"{content}\n"
        f"=====\n"
        f"{instructions}"
    )


def load_artifact() -> str:
    try:
        artifacts_settings = get_settings().get("ARTIFACTS", {})
    except AttributeError:
        return ""

    if not artifacts_settings:
        return ""

    enable = artifacts_settings.get("enable", False)
    if isinstance(enable, str):
        enable = enable.lower() == "true"
    if not enable:
        return ""

    artifact_path_str = artifacts_settings.get("artifact_path", "")
    if not artifact_path_str:
        return ""

    artifact_path = resolve_artifact_path(artifact_path_str)
    if not artifact_path:
        get_logger().warning(
            f"Artifact file not found or path rejected: '{artifact_path_str}' "
            f"(GITHUB_WORKSPACE={os.environ.get('GITHUB_WORKSPACE', 'not set')})"
        )
        return ""

    try:
        max_size = int(artifacts_settings.get("max_artifact_size", 50000))
    except (TypeError, ValueError):
        max_size = 50000
    if max_size <= 0:
        max_size = 50000
    content = _read_and_truncate(artifact_path, max_size)
    if not content:
        return ""

    label = artifacts_settings.get("artifact_label", "") or artifact_path.name
    instructions = artifacts_settings.get("artifact_instructions", "")
    return format_artifact_content(content, label, instructions)


def inject_artifact_context() -> None:
    """Append the CI artifact (see [artifacts]) to the extra_instructions of the target tools.

    ARTIFACT_PATH in the environment turns the feature on by itself. Called once before a
    command runs, by the GitHub Action runner and by the CLI.
    """
    state = _artifact_context.get()
    if state is not None:
        if not state.active:
            return
        settings = get_settings()
        state.bind(settings)
        if settings is not state.settings:
            return
        if state.prepared:
            reapply_artifact_context()
            return
        state.prepared = True

    artifact_path_env = (
        os.environ.get("ARTIFACT_PATH") or os.environ.get("PR_AGENT_ARTIFACT_PATH") or ""
    ).strip()
    artifact_instructions_env = (
        os.environ.get("ARTIFACT_INSTRUCTIONS") or os.environ.get("PR_AGENT_ARTIFACT_INSTRUCTIONS") or ""
    ).strip()
    if artifact_path_env:
        get_settings().set("ARTIFACTS.ENABLE", True)
        get_settings().set("ARTIFACTS.ARTIFACT_PATH", artifact_path_env)
        if artifact_instructions_env:
            get_settings().set("ARTIFACTS.ARTIFACT_INSTRUCTIONS", artifact_instructions_env)

    artifacts_enabled = get_settings().get("ARTIFACTS.ENABLE", False)
    if isinstance(artifacts_enabled, str):
        artifacts_enabled = artifacts_enabled.lower() == "true"
    if artifacts_enabled is not True:
        return

    try:
        artifact_text = load_artifact()
        if not artifact_text:
            return
        target_tools = get_settings().get(
            "ARTIFACTS.TARGET_TOOLS",
            ["pr_reviewer", "pr_description", "pr_code_suggestions"]
        )
        if isinstance(target_tools, str):
            target_tools = [t.strip() for t in target_tools.split(",") if t.strip()]
        target_tools = frozenset(str(t).lower() for t in target_tools)
        if state is not None:
            for target in target_tools:
                state.remember(state.settings, f"{target}.extra_instructions")
            state.text = artifact_text
            state.targets = target_tools
        _append_artifact_context(get_settings(), artifact_text, target_tools)
        get_logger().info(f"Injected artifact context into tools: {target_tools}")
    except (OSError, ValueError, TypeError) as e:
        get_logger().warning(f"Failed to process artifacts: {e}", exc_info=True)
