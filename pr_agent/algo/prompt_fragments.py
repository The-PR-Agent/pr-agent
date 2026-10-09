from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from pr_agent.config_loader import get_settings


def _render_fragment(template: str, **variables) -> str:
    environment = SandboxedEnvironment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    return environment.from_string(template).render(**variables)


def render_diff_hunk_format(*, include_line_numbers: bool, include_ai_metadata: bool) -> str:
    """Render the shared diff-hunk description before inserting it into a tool prompt."""
    return _render_fragment(
        get_settings().prompt_fragments.diff_hunk_format,
        include_line_numbers=include_line_numbers,
        include_ai_metadata=include_ai_metadata,
    ).strip()


def render_skills_prefix(*, skills_context: str) -> str:
    """Render the shared skills block that opens the review tool system prompts.

    Returns the exact text the templates prepend to their tool-specific instructions, or an
    empty string when ``skills_context`` is empty. The text is left unstripped so callers can
    match it against the start of a rendered system prompt.
    """
    return _render_fragment(
        get_settings().prompt_fragments.skills_prefix,
        skills_context=skills_context,
    )
