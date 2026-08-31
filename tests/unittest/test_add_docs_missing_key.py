"""Report an /add_docs response that carries no documentation, instead of going silent."""
import pytest

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

    def publish_comment(self, body, **kwargs):
        self.comments.append(body)
        return "comment"

    def publish_code_suggestions(self, suggestions):
        self.suggestions.append(suggestions)
        return True

    def get_diff_files(self):
        return []


def run(prediction):
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = FakeGitProvider()
    tool.prediction = prediction
    tool.push_inline_docs(tool._prepare_pr_code_docs())
    return tool.git_provider


def test_publish_the_documented_response():
    """Keep publishing suggestions for a well-formed response."""
    provider = run(DOCUMENTED)

    assert provider.suggestions and provider.suggestions[0]


@pytest.mark.parametrize("prediction, reason", [
    ("No documentation needed for this PR.\n", "the model answered in prose"),
    ("documentation:\n- relevant file: src/app.py\n", "the model renamed the key"),
    ("Code Documentation:\n", "the model emitted the key with no value"),
    ("::: not : valid : yaml :::\n\t- [", "the response could not be parsed at all"),
])
def test_tell_the_user_that_nothing_was_produced(prediction, reason):
    """A command that ran must leave a comment, never silence."""
    provider = run(prediction)

    assert provider.comments, reason
