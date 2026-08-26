from types import SimpleNamespace
from unittest.mock import MagicMock

from jinja2 import Environment, meta

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def test_reviewer_vars_cover_system_and_user_prompt_contract(monkeypatch):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    provider = MagicMock()
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_pr_description.return_value = ("desc", [])

    monkeypatch.setattr(
        pr_reviewer_module,
        "get_git_provider_with_context",
        lambda pr_url: provider,
    )
    monkeypatch.setattr(
        pr_reviewer_module,
        "get_main_pr_language",
        lambda languages, files: "Python",
    )
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())

    reviewer = PRReviewer(
        "https://example/pr/1",
        ai_handler=lambda: SimpleNamespace(main_pr_language=None),
    )

    environment = Environment()
    prompt_settings = get_settings().pr_review_prompt
    referenced_variables = set()
    for template in (prompt_settings.system, prompt_settings.user):
        parsed_template = environment.parse(template)
        referenced_variables.update(
            meta.find_undeclared_variables(parsed_template)
        )

    missing_variables = referenced_variables - set(reviewer.vars)
    assert not missing_variables, (
        "PR reviewer prompt variables missing from reviewer.vars: "
        f"{sorted(missing_variables)}"
    )
