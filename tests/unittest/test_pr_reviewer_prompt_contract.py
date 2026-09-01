"""Contract tests for the variables ``pr_reviewer_prompts.toml`` depends on.

``PRReviewer._get_prediction`` renders both the system and user halves of
``pr_review_prompt`` with ``undefined=StrictUndefined`` against a copy of
``PRReviewer.vars``. Under ``StrictUndefined`` a bare ``{%- if x %}`` raises
``UndefinedError`` exactly like a direct reference, so every name either prompt
mentions is a hard requirement on that dict. A missing name is not a degraded
review, it is ``/review`` failing outright on every PR.

These tests derive the requirement from the templates (via
``jinja2.meta.find_undeclared_variables``) instead of restating it, and check it
against the dict the tool actually builds by driving the real
``PRReviewer.__init__``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, StrictUndefined, meta, select_autoescape

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _build_reviewer(monkeypatch):
    """Run the real ``PRReviewer.__init__`` so ``self.vars`` is the shipped dict."""
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_pr_description.return_value = ("desc", [])

    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())

    return PRReviewer(
        "https://example/pr/1",
        ai_handler=lambda: SimpleNamespace(main_pr_language=None),
    )


def _referenced_variables(half):
    template = getattr(get_settings().pr_review_prompt, half)
    # Environment() here only needs to parse; production rendering happens in
    # PRReviewer._get_prediction with the same StrictUndefined setting. autoescape
    # mirrors pr_line_questions._render_prompts and is irrelevant to parsing.
    environment = Environment(
        autoescape=select_autoescape(default_for_string=False),
        undefined=StrictUndefined,
    )
    return meta.find_undeclared_variables(environment.parse(template))


@pytest.mark.parametrize("half", ["system", "user"])
def test_pr_review_prompt_variables_are_all_supplied(monkeypatch, half):
    reviewer = _build_reviewer(monkeypatch)
    provided = set(reviewer.vars)

    # Subset, not equality: vars legitimately carries keys the review prompts do
    # not use (e.g. language, commit_messages_str, custom_labels).
    referenced = _referenced_variables(half)
    assert referenced, f"expected the '{half}' prompt to reference variables; it references none"
    missing = referenced - provided
    assert not missing, (
        f"pr_reviewer_prompts.toml '{half}' prompt references {sorted(missing)}, "
        f"but PRReviewer.vars does not supply them; /review would raise UndefinedError "
        f"on every PR."
    )


def test_user_prompt_contributes_variables_of_its_own(monkeypatch):
    """The user half references names the system half does not.

    Before this file, only the system prompt was rendered under StrictUndefined in
    the test suite, so those user-only names had no coverage. Keep an explicit check
    that dropping one from ``vars`` is visible to the derived subset test above.
    """
    reviewer = _build_reviewer(monkeypatch)
    user_referenced = _referenced_variables("user")
    user_only = user_referenced - _referenced_variables("system")
    assert user_only, "expected the user prompt to reference variables the system prompt does not"
    assert user_only <= set(reviewer.vars)

    # Dropping one such name from vars is what the subset test above would flag.
    dropped = next(iter(user_only))
    assert user_referenced - (set(reviewer.vars) - {dropped}) == {dropped}
