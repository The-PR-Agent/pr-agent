"""
Agent skills loader.

Discovers ``SKILL.md`` files from configured filesystem paths, parses their YAML
frontmatter, and formats them as prompt context for review/improve/describe tools.

A skill is a directory containing a ``SKILL.md`` file with the structure:

    ---
    name: terraform-standards
    description: Use when reviewing Terraform code...
    ---

    # Terraform Review Guidance
    ...

Activation is description-based: every discovered skill is included with its
name, description, and body. The model decides which guidance applies based on
the descriptions.

Resources alongside SKILL.md
----------------------------
The agent-skills standard supports bundled files for progressive disclosure:
``references/`` (markdown context loaded on demand), ``scripts/`` (executables
the agent can invoke), and ``assets/`` (templates / images / data). PR-Agent
runs single-shot model calls and has no tool-use loop, so progressive disclosure
is not implementable here. Instead, this loader inlines every text resource
directly into the prompt:

* All ``*.md`` files in the skill directory tree (including ``references/``)
  are gathered and appended after the SKILL.md body.
* ``scripts/`` and ``assets/`` subdirectories are skipped: scripts are
  executables we cannot safely run from a one-shot prompt, and assets are
  typically binary. Skills that depend on script execution will not work.

In short, this implementation supports **text-only** agent skills.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import yaml
from starlette_context import context
from starlette_context.errors import ContextDoesNotExistError

from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.utils import clip_tokens
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

_FRONTMATTER_DELIMITER = "---"
_DEFAULT_MAX_SKILLS_TOKENS = 8000
# Subdirectories whose contents are intentionally excluded from inlining,
# matching the agent-skills standard's executable/binary conventions.
_EXCLUDED_RESOURCE_DIRS = frozenset({"scripts", "assets"})
_CONTEXT_CACHE_KEY = "skills_context"
# Per-resource-file size cap. Defence-in-depth against pathological skill
# directories (large markdown dumps, accidental inclusion of generated docs,
# or a misconfigured paths entry pointing at a directory with huge files).
_MAX_RESOURCE_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class SkillResource:
    """A non-SKILL.md text file bundled with a skill (e.g. references/guide.md)."""
    relative_path: str
    content: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    resources: Tuple[SkillResource, ...] = field(default_factory=tuple)


def _count_tokens(text: str) -> int:
    return len(TokenEncoder.get_token_encoder().encode(text))


def _gather_resources(skill_md_path: str) -> Tuple[SkillResource, ...]:
    """Walk the skill's directory tree and collect sibling ``*.md`` files.

    SKILL.md itself is excluded. Subdirectories named ``scripts`` or ``assets``
    are skipped wholesale. If a nested directory contains its own SKILL.md it
    is treated as a separate skill and not descended into.
    """
    skill_dir = os.path.dirname(skill_md_path)
    resources: List[SkillResource] = []

    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_RESOURCE_DIRS]
        if root != skill_dir and "SKILL.md" in files:
            dirs[:] = []
            continue
        for filename in files:
            if not filename.endswith(".md"):
                continue
            if root == skill_dir and filename == "SKILL.md":
                continue
            full = os.path.join(root, filename)
            try:
                size = os.path.getsize(full)
            except OSError as e:
                get_logger().warning(f"Skill resource unreadable: {full} ({e})")
                continue
            if size > _MAX_RESOURCE_FILE_BYTES:
                get_logger().warning(
                    f"Skill resource skipped (exceeds {_MAX_RESOURCE_FILE_BYTES} bytes): {full} ({size} bytes)"
                )
                continue
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                get_logger().warning(f"Skill resource unreadable: {full} ({e})")
                continue
            rel = os.path.relpath(full, skill_dir)
            resources.append(SkillResource(relative_path=rel, content=content))

    resources.sort(key=lambda r: r.relative_path)
    return tuple(resources)


def _parse_skill_file(file_path: str) -> Optional[Skill]:
    """Parse a single SKILL.md file. Returns None and logs a warning on malformed input."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        get_logger().warning(f"Skill file unreadable: {file_path} ({e})")
        return None

    lines = content.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        get_logger().warning(f"Skill file missing opening frontmatter delimiter: {file_path}")
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIMITER:
            end_idx = i
            break
    if end_idx is None:
        get_logger().warning(f"Skill file missing closing frontmatter delimiter: {file_path}")
        return None

    frontmatter_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        meta = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        get_logger().warning(f"Skill frontmatter is not valid YAML: {file_path} ({e})")
        return None

    if not isinstance(meta, dict):
        get_logger().warning(f"Skill frontmatter must be a mapping: {file_path}")
        return None

    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        get_logger().warning(f"Skill missing required 'name' field: {file_path}")
        return None
    if not isinstance(description, str) or not description.strip():
        get_logger().warning(f"Skill missing required 'description' field: {file_path}")
        return None

    return Skill(
        name=name.strip(),
        description=description.strip(),
        body=body,
        resources=_gather_resources(file_path),
    )


def discover_skills(paths: List[str]) -> List[Skill]:
    """Scan the given filesystem paths for ``*/SKILL.md`` files.

    Each entry in ``paths`` may be either a directory containing skill
    subdirectories (recursive search) or a path to a SKILL.md file directly.
    Environment variables and ``~`` are expanded. Missing paths are skipped
    with a warning.
    """
    skills: List[Skill] = []
    seen: set = set()

    for raw_path in paths or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        expanded = os.path.expanduser(os.path.expandvars(raw_path.strip()))
        if not os.path.exists(expanded):
            get_logger().warning(f"Skills path does not exist: {expanded}")
            continue

        if os.path.isfile(expanded):
            candidates = [expanded] if os.path.basename(expanded) == "SKILL.md" else []
        else:
            candidates = []
            for root, _dirs, files in os.walk(expanded):
                if "SKILL.md" in files:
                    candidates.append(os.path.join(root, "SKILL.md"))

        for candidate in candidates:
            real = os.path.realpath(candidate)
            if real in seen:
                continue
            seen.add(real)
            skill = _parse_skill_file(candidate)
            if skill is not None:
                skills.append(skill)

    skills.sort(key=lambda s: s.name)
    return skills


def _format_skill(skill: Skill) -> str:
    """Render a skill (and its inlined resources) as a prompt-ready string."""
    parts = [
        f"### Skill: {skill.name}",
        f"When to use: {skill.description}",
        "",
        skill.body.rstrip(),
    ]
    for resource in skill.resources:
        parts.append("")
        parts.append(f"#### {resource.relative_path}")
        parts.append(resource.content.rstrip())
    return "\n".join(parts).rstrip()


def format_skills_context(skills: List[Skill], max_tokens: int) -> str:
    """Format skills into a prompt-ready string under a token budget.

    Skills are emitted in order; once the running token count would exceed the
    budget, remaining skills are dropped. If the first skill alone exceeds the
    budget, its formatted text is clipped via ``clip_tokens`` and a marker is
    appended. Returns an empty string when nothing fits.
    """
    if not skills:
        return ""
    if max_tokens is None or max_tokens <= 0:
        return ""

    truncate_marker = "\n\n[truncated]"
    separator = "\n\n---\n\n"
    sep_tokens = _count_tokens(separator)
    marker_tokens = _count_tokens(truncate_marker)
    pieces: List[str] = []
    used = 0
    for skill in skills:
        formatted = _format_skill(skill)
        tokens = _count_tokens(formatted)
        addition = (sep_tokens if pieces else 0) + tokens
        if used + addition > max_tokens:
            if not pieces:
                budget = max(1, max_tokens - marker_tokens)
                truncated = clip_tokens(formatted, budget, add_three_dots=False)
                pieces.append(truncated + truncate_marker)
                if len(skills) > 1:
                    get_logger().info(
                        f"First skill exceeded budget; truncated and dropped {len(skills) - 1} skill(s)"
                    )
            else:
                get_logger().info(
                    f"Skills context budget reached; dropping {len(skills) - len(pieces)} skill(s)"
                )
            break
        pieces.append(formatted)
        used += addition

    return separator.join(pieces).strip()


def _get_cached_context() -> Optional[str]:
    try:
        return context.get(_CONTEXT_CACHE_KEY, None)
    except ContextDoesNotExistError:
        # No request-scoped context (e.g. CLI runs): nothing is memoised, so
        # report a cache miss and let the caller compute the value.
        return None


def _set_cached_context(value: str) -> None:
    try:
        context[_CONTEXT_CACHE_KEY] = value
    except ContextDoesNotExistError:
        # No request-scoped context (e.g. CLI runs): skip memoisation. The value
        # is recomputed on each call, which is harmless for a single CLI command.
        return


def get_skills_context() -> str:
    """Read settings, discover skills, and format them for prompt injection.

    Memoised per request via ``starlette_context`` so the three tools that
    inject ``skills_context`` (review, improve, describe) share a single
    discovery + parse + format. Returns ``''`` when skills are disabled, no
    paths are configured, or no skills are found.
    """
    cached = _get_cached_context()
    if cached is not None:
        return cached

    settings = get_settings()
    if not settings.skills.enabled:
        _set_cached_context("")
        return ""
    paths = list(settings.skills.paths or [])
    raw_max = settings.skills.max_skills_tokens
    try:
        max_tokens = int(raw_max)
    except (TypeError, ValueError):
        get_logger().warning(
            f"Invalid skills.max_skills_tokens={raw_max!r}; falling back to {_DEFAULT_MAX_SKILLS_TOKENS}"
        )
        max_tokens = _DEFAULT_MAX_SKILLS_TOKENS
    skills = discover_skills(paths)
    out = format_skills_context(skills, max_tokens) if skills else ""
    _set_cached_context(out)
    return out
