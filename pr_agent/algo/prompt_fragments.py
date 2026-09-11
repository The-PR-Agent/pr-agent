from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from pr_agent.config_loader import get_settings


def render_diff_hunk_format(*, include_line_numbers: bool, include_ai_metadata: bool) -> str:
    """Render the shared diff-hunk description before inserting it into a tool prompt."""
    environment = SandboxedEnvironment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = get_settings().prompt_fragments.diff_hunk_format
    return environment.from_string(template).render(
        include_line_numbers=include_line_numbers,
        include_ai_metadata=include_ai_metadata,
    ).strip()
