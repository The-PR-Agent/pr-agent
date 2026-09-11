"""The key-issues field instruction is a fragment, not prompt-file text.

Step 1 of the review-quality work plan A/Bs that one sentence across eval rows. Rows are only
comparable if the wording is the only thing that differs between them, so the sentence lives in
``prompt_fragments.findings_field`` and is swapped with ``--set``; editing the prompt file per
variant would change the tree under the runs being compared.
"""

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.prompt_fragments import render_findings_field_instruction
from pr_agent.config_loader import get_settings

# The wording shipped before the fragment existed, with the count interpolated. Variant A of the
# Step 1 experiment is "today's wording", so a drift here silently retires the control.
SHIPPED_WORDING = (
    'A concise list (0-3 issues) of bugs, security vulnerabilities, or significant performance concerns introduced in this PR. Only include issues you are confident about. If confidence is limited but the potential impact is high (e.g., data loss, security), you may include it only if you explicitly note what remains uncertain. Each issue must identify a concrete problem with a realistic trigger scenario. An empty list is acceptable if no clear issues are found.'
)


def test_default_fragment_reproduces_the_shipped_wording():
    assert render_findings_field_instruction(num_max_findings=3) == SHIPPED_WORDING


def test_the_count_interpolates_into_the_fragment():
    assert "(0-12 issues)" in render_findings_field_instruction(num_max_findings=12)


def test_an_override_replaces_the_wording_and_still_interpolates(monkeypatch):
    monkeypatch.setitem(
        get_settings().prompt_fragments, "findings_field",
        "Report every defect you can evidence (up to {{ num_max_findings }}).")
    rendered = render_findings_field_instruction(num_max_findings=7)
    assert rendered == "Report every defect you can evidence (up to 7)."
    assert "concise" not in rendered


def test_the_review_prompt_carries_the_placeholder_and_renders_an_override(monkeypatch):
    """An override must reach the model: the prompt file holds the placeholder, not the text."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    template = get_settings().pr_review_prompt.system
    assert "{{ findings_field_instruction }}" in template
    assert "A concise list (0-" not in template

    monkeypatch.setitem(
        get_settings().prompt_fragments, "findings_field", "SENTINEL-{{ num_max_findings }}")

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_pr_description.return_value = ("desc", [])
    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())
    reviewer = pr_reviewer_module.PRReviewer(
        "https://example/pr/1", ai_handler=lambda: SimpleNamespace(main_pr_language=None))

    rendered = Environment(undefined=StrictUndefined).from_string(template).render(reviewer.vars)
    assert f"SENTINEL-{get_settings().pr_reviewer.num_max_findings}" in rendered
    assert "Only include issues you are confident about" not in rendered
