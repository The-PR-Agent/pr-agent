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
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import yaml

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Approximate characters-per-token used to keep the skills block under budget.
_CHARS_PER_TOKEN = 4
_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str


def _parse_skill_file(file_path: str) -> Optional[Skill]:
    """Parse a single SKILL.md file. Returns None and logs a warning on malformed input."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
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

    return Skill(name=name.strip(), description=description.strip(), body=body, path=file_path)


def discover_skills(paths: List[str]) -> List[Skill]:
    """Scan the given filesystem paths for ``*/SKILL.md`` files.

    Each entry in ``paths`` may be either a directory containing skill
    subdirectories (recursive search) or a path to a SKILL.md file directly.
    Missing paths are skipped with a warning.
    """
    skills: List[Skill] = []
    seen: set = set()

    for raw_path in paths or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = os.path.expanduser(raw_path.strip())
        if not os.path.exists(path):
            get_logger().warning(f"Skills path does not exist: {path}")
            continue

        if os.path.isfile(path):
            candidates = [path] if os.path.basename(path) == "SKILL.md" else []
        else:
            candidates = []
            for root, _dirs, files in os.walk(path):
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
    return (
        f"### Skill: {skill.name}\n"
        f"When to use: {skill.description}\n\n"
        f"{skill.body}".rstrip()
    )


def format_skills_context(skills: List[Skill], max_tokens: int) -> str:
    """Format skills into a prompt-ready string under a token budget.

    Skills are emitted in order; once the running character count would exceed
    the budget (estimated as ``max_tokens * 4`` characters), remaining skills are
    dropped. Returns an empty string if no skills fit.
    """
    if not skills:
        return ""
    if max_tokens is None or max_tokens <= 0:
        return ""

    char_budget = max_tokens * _CHARS_PER_TOKEN
    pieces: List[str] = []
    used = 0
    separator = "\n\n---\n\n"
    for skill in skills:
        formatted = _format_skill(skill)
        addition = (separator if pieces else "") + formatted
        if used + len(addition) > char_budget:
            if not pieces:
                # First skill alone exceeds the budget; truncate its body so we
                # still provide partial context rather than dropping everything.
                truncated = formatted[: max(0, char_budget - len("\n\n[truncated]"))]
                pieces.append(truncated + "\n\n[truncated]")
                used = len(pieces[0])
            else:
                get_logger().info(
                    f"Skills context budget reached; dropping {len(skills) - len(pieces)} skill(s)"
                )
            break
        pieces.append(formatted)
        used += len(addition)

    return separator.join(pieces).strip()


def get_skills_context() -> str:
    """Convenience helper: read settings, discover, and format. Returns ''
    when skills are disabled or no paths yield content. Never raises."""
    try:
        settings = get_settings()
        skills_cfg = settings.get("skills", None)
        if not skills_cfg:
            return ""
        if not skills_cfg.get("enabled", False):
            return ""
        paths = skills_cfg.get("paths", []) or []
        max_tokens = int(skills_cfg.get("max_skills_tokens", 4000) or 0)
        skills = discover_skills(list(paths))
        if not skills:
            return ""
        return format_skills_context(skills, max_tokens)
    except Exception as e:
        get_logger().warning(f"Failed to build skills context: {e}")
        return ""
