"""Clear the progress comment when an /add_docs response carries no documentation."""
import asyncio

import pytest

import pr_agent.tools.pr_add_docs as pr_add_docs
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_add_docs import PRAddDocs

DOCUMENTED = """Code Documentation:
- relevant file: |
    src/app.py
  relevant line: 1
  doc placement: |
    before
  documentation: |
    \"\"\"Do the thing.\"\"\"
"""


class FakeGitProvider:
    def __init__(self):
        self.comments = []
        self.suggestions = []
        self.initial_comment_removed = False
        self.diff_files = []

    def publish_comment(self, body, **kwargs):
        self.comments.append(body)
        return "comment"

    def remove_initial_comment(self):
        self.initial_comment_removed = True

    def publish_code_suggestions(self, suggestions):
        self.suggestions.append(suggestions)
        return True

    def get_diff_files(self):
        return self.diff_files


@pytest.fixture
def publish_output():
    settings = get_settings(use_context=False)
    original = settings.get("config.publish_output", True)
    settings.set("config.publish_output", True)
    yield settings
    settings.set("config.publish_output", original)


def run(prediction, monkeypatch):
    async def fake_retry(fn=None, model_type=None):
        return prediction

    monkeypatch.setattr(pr_add_docs, "retry_with_fallback_models", fake_retry)
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = FakeGitProvider()
    tool.prediction = prediction
    asyncio.run(tool.run())
    return tool.git_provider


def results(provider):
    return [c for c in provider.comments if "Generating Documentation" not in c]


def test_publish_the_documented_response(publish_output, monkeypatch):
    """Keep publishing suggestions for a well-formed response."""
    provider = run(DOCUMENTED, monkeypatch)

    assert provider.suggestions and provider.suggestions[0]
    assert provider.initial_comment_removed


@pytest.mark.parametrize("prediction, reason", [
    ("No documentation needed for this PR.\n", "the model answered in prose"),
    ("documentation:\n- relevant file: src/app.py\n", "the model renamed the key"),
    ("Code Documentation:\n", "the model emitted the key with no value"),
    ("::: not : valid : yaml :::\n\t- [", "the response could not be parsed at all"),
])
def test_clear_the_progress_comment_when_nothing_was_produced(publish_output, monkeypatch,
                                                              prediction, reason):
    """The 'Generating Documentation...' placeholder must not be left behind."""
    provider = run(prediction, monkeypatch)

    assert provider.initial_comment_removed, reason


@pytest.mark.parametrize("prediction", [
    "No documentation needed for this PR.\n",
    "documentation:\n- relevant file: src/app.py\n",
    "Code Documentation:\n",
    "::: not : valid : yaml :::\n\t- [",
])
def test_tell_the_user_that_nothing_was_produced(publish_output, monkeypatch, prediction):
    """A command that ran must leave a result, never just a stale placeholder."""
    provider = run(prediction, monkeypatch)

    assert results(provider) == ["No code documentation found to improve this PR."]
