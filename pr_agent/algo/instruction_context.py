"""Resolve protected system instructions shared by every model request."""

from pr_agent.config_loader import get_settings


def get_protected_system_instructions() -> str:
    """Return host-only instructions that must accompany every tool prompt."""
    value = get_settings().config.get("system_instructions", "")
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def append_protected_system_instructions(system_prompt: str) -> str:
    """Append host-only instructions after the tool's own system prompt."""
    protected = get_protected_system_instructions()
    if not protected:
        return system_prompt
    if not system_prompt or not system_prompt.strip():
        return protected
    return f"{system_prompt.rstrip()}\n\n{protected}"
