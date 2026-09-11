"""R-16 wiring: retrieved context must reach the prompt, and flip the caveats that contradict it.

The system prompt otherwise tells the model "you only see changed code segments, not the entire
codebase" and "do not speculate ... unless you can identify the specific affected code path from
the diff context". Sending retrieved code while those sentences stand would hand the model context
and instruct it to distrust the context in the same breath.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_reviewer as pr_reviewer_module

DIFF = (
    "\n\n## File: 'lib/ads_service.dart'\n\n@@ -1,3 +1,4 @@\n+  await showAd().timeout(d);\n"
    "\n\n## File: 'lib/other.dart'\n\n@@ -1,2 +1,2 @@\n+var x = 1;\n"
)


@pytest.fixture
def reviewer(monkeypatch):
    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_pr_description.return_value = ("desc", [])
    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())
    return pr_reviewer_module.PRReviewer(
        "https://example/pr/1", ai_handler=lambda: SimpleNamespace(main_pr_language=None))


@pytest.fixture
def retrieval_settings():
    settings = get_settings()
    before = (settings.pr_reviewer.get("enable_symbol_retrieval", False),
              settings.pr_reviewer.get("repo_checkout_path", ""))
    yield settings
    settings.set("pr_reviewer.enable_symbol_retrieval", before[0])
    settings.set("pr_reviewer.repo_checkout_path", before[1])


def _render_system(reviewer, overrides):
    variables = dict(reviewer.vars)
    variables.update(overrides)
    template = get_settings().pr_review_prompt.system
    return Environment(undefined=StrictUndefined).from_string(template).render(variables)


def _render_user(reviewer, overrides):
    variables = dict(reviewer.vars)
    variables.update(overrides)
    template = get_settings().pr_review_prompt.user
    return Environment(undefined=StrictUndefined).from_string(template).render(variables)


def test_without_retrieval_the_prompt_is_unchanged(reviewer):
    system = _render_system(reviewer, {"has_retrieved_context": False})
    user = _render_user(reviewer, {"retrieved_context": "", "diff": DIFF})
    assert "you only see changed code segments" in system
    assert "from the diff context" in system
    assert "Retrieved repository context" not in user


def test_with_retrieval_the_caveats_flip(reviewer):
    system = _render_system(reviewer, {"has_retrieved_context": True})
    assert "you only see changed code segments (diff hunks in a PR), not the entire codebase" not in system
    assert "Retrieved repository context" in system
    assert ", or from the retrieved repository context" in system, (
        "the speculation rule must accept retrieved code as an identified path, or the retrieval "
        "is contradicted by the instruction next to it")


def test_retrieved_context_reaches_the_user_prompt(reviewer):
    user = _render_user(reviewer, {"retrieved_context": "lib/x.dart:10: RETRIEVED-SNIPPET", "diff": DIFF})
    assert "RETRIEVED-SNIPPET" in user
    assert "Retrieved repository context" in user


def test_retrieval_is_off_by_default(reviewer):
    assert reviewer.vars["has_retrieved_context"] is False
    assert reviewer.vars["retrieved_context"] == ""
    assert reviewer._retrieved_context_for(DIFF) == ""


def test_a_missing_checkout_path_degrades_instead_of_raising(reviewer, retrieval_settings):
    retrieval_settings.set("pr_reviewer.enable_symbol_retrieval", True)
    retrieval_settings.set("pr_reviewer.repo_checkout_path", "/nonexistent/checkout")
    assert reviewer._retrieved_context_for(DIFF) == ""


def test_retrieval_pulls_cross_file_context_from_a_real_checkout(reviewer, retrieval_settings, tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "ads_service.dart").write_text("Future<void> showAd() async {}\n")
    (tmp_path / "lib" / "caller.dart").write_text("void go() {\n  showAd();\n}\n")
    retrieval_settings.set("pr_reviewer.enable_symbol_retrieval", True)
    retrieval_settings.set("pr_reviewer.repo_checkout_path", str(tmp_path))

    retrieved = reviewer._retrieved_context_for(
        "\n\n## File: 'lib/ads_service.dart'\n\n@@ -1,2 +1,2 @@\n+Future<void> showAd() async {}\n")

    assert "caller.dart" in retrieved, "a caller in an untouched file is the point of R-16"
    assert "lib/ads_service.dart:" not in retrieved, "the changed file is already in the diff"


def test_the_index_is_built_once_per_reviewer(reviewer, retrieval_settings, tmp_path, monkeypatch):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.dart").write_text("class A {}\n")
    retrieval_settings.set("pr_reviewer.enable_symbol_retrieval", True)
    retrieval_settings.set("pr_reviewer.repo_checkout_path", str(tmp_path))

    calls = []
    real = pr_reviewer_module.build_repo_symbol_index
    monkeypatch.setattr(pr_reviewer_module, "build_repo_symbol_index",
                        lambda root, **kw: (calls.append(root), real(root, **kw))[1])

    reviewer._retrieved_context_for(DIFF)
    reviewer._retrieved_context_for(DIFF)
    assert len(calls) == 1, "indexing walks the whole checkout; once per chunk would multiply it"
