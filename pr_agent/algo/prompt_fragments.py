from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from pr_agent.config_loader import get_settings


def render_findings_field_instruction(*, num_max_findings: int) -> str:
    """Render the review prompt's key-issues field instruction.

    Kept out of the prompt file so its wording can be overridden per run
    (`prompt_fragments.findings_field`) without editing a template, which is what makes two
    eval rows comparable.
    """
    environment = SandboxedEnvironment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = get_settings().prompt_fragments.findings_field
    return environment.from_string(template).render(num_max_findings=num_max_findings).strip()


def render_diff_hunk_format(*, include_line_numbers: bool, include_ai_metadata: bool) -> str:
    """Render the shared diff-hunk description before inserting it into a tool prompt."""
    environment = SandboxedEnvironment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = get_settings().prompt_fragments.diff_hunk_format
    return environment.from_string(template).render(
        include_line_numbers=include_line_numbers,
        include_ai_metadata=include_ai_metadata,
    ).strip()
