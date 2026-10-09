"""Clear the progress comment when an /add_docs response carries no documentation."""
import asyncio
from unittest.mock import Mock

import pytest
from opentelemetry.trace import StatusCode

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import command_failed, get_run_details, init_run_details
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)
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

DOCUMENTED_TWICE = """Code Documentation:
- relevant file: |
    src/app.py
  relevant line: 1
  doc placement: |
    before
  documentation: |
    \"\"\"Do the first thing.\"\"\"
- relevant file: |
    src/app.py
  relevant line: 2
  doc placement: |
    after
  documentation: |
    \"\"\"Do the second thing.\"\"\"
"""

FILTERED_DOCUMENTATION = """Code Documentation:
- relevant file: |
    src/app.py
  relevant line: 1
  doc placement: |
    before
  documentation: ""
"""


class FakeGitProvider:
    def __init__(self, suggestion_results=None, comment_error=None, comment_result="comment"):
        self.comments = []
        self.suggestions = []
        self.initial_comment_removed = False
        self.diff_files = []
        self.suggestion_results = list(suggestion_results or [])
        self.comment_error = comment_error
        self.comment_result = comment_result

    def publish_comment(self, body, **kwargs):
        self.comments.append(body)
        if self.comment_error and self.comment_error in body:
            raise RuntimeError("fallback comment unavailable")
        return self.comment_result

    def remove_initial_comment(self):
        self.initial_comment_removed = True

    def publish_code_suggestions(self, suggestions):
        self.suggestions.append(suggestions)
        return self.suggestion_results.pop(0) if self.suggestion_results else True

    def get_diff_files(self):
        return self.diff_files


@pytest.fixture
def publish_output():
    settings = get_settings(use_context=False)
    original = settings.get("config.publish_output", True)
    settings.set("config.publish_output", True)
    yield settings
    settings.set("config.publish_output", original)


def run(prediction, monkeypatch, provider=None):
    async def fake_retry(fn=None, model_type=None, **kwargs):
        return prediction

    monkeypatch.setattr("pr_agent.tools.pr_add_docs.retry_with_fallback_models", fake_retry)
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = provider or FakeGitProvider()
    tool.prediction = prediction
    asyncio.run(tool.run())
    return tool.git_provider


def results(provider):
    return [c for c in provider.comments if "Generating Documentation" not in c]


@pytest.mark.asyncio
@pytest.mark.parametrize("suggestion_results, comment_error, expected_result", [
    ([False, False, False], None, False),
    ([False, False, False], "Failed to publish code documentation", False),
    ([True], None, True),
    ([False, False, True], None, True),
    ([False, None, False], None, True),
])
async def test_routed_publication_outcome_with_empty_run_details(
        publish_output, monkeypatch, suggestion_results, comment_error, expected_result):
    provider = FakeGitProvider(suggestion_results=suggestion_results, comment_error=comment_error)
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = provider
    tool.prediction = DOCUMENTED_TWICE

    async def fake_retry(*_args, **_kwargs):
        return DOCUMENTED_TWICE

    monkeypatch.setattr("pr_agent.tools.pr_add_docs.retry_with_fallback_models", fake_retry)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setitem(pr_agent_module.command2class, "add_docs", lambda *_args, **_kwargs: tool)
    span = Mock()
    settings = get_settings()
    previous = settings.get("config.propagate_tool_errors", False)
    settings.set("config.propagate_tool_errors", False)
    try:
        result = await pr_agent_module.PRAgent(ai_handler="fake-ai")._run_command(
            "https://example/pr/1", "/add_docs", None, span
        )
    finally:
        settings.set("config.propagate_tool_errors", previous)

    assert result is expected_result
    span.set_status.assert_called_once_with(StatusCode.OK if expected_result else StatusCode.ERROR)
    assert provider.initial_comment_removed
    assert len(provider.suggestions) == (1 if suggestion_results == [True] else 3)
    # The agent installs a collector per command; an empty one must not affect routing.
    details = get_run_details()
    assert details is not None
    assert details.num_ai_calls == 0
    assert details.model_used is None
    if not expected_result:
        assert results(provider) == ["Failed to publish code documentation for this PR."]
        span.set_attribute.assert_any_call("error.type", "documentation_publication_failed")


def test_publish_the_documented_response(publish_output, monkeypatch):
    """Keep publishing suggestions for a well-formed response."""
    provider = run(DOCUMENTED, monkeypatch)

    assert provider.suggestions and provider.suggestions[0]
    assert provider.initial_comment_removed


@pytest.mark.parametrize(
    "incomplete_error_class", [IncompleteBitbucketPullRequestFilesError, IncompletePullRequestFilesError]
)
def test_incomplete_provider_diff_is_re_raised_after_temporary_comment_cleanup(
        publish_output, monkeypatch, incomplete_error_class):
    async def fail_with_incomplete_diff(*_args, **_kwargs):
        raise incomplete_error_class("incomplete aggregate diff")

    monkeypatch.setattr("pr_agent.tools.pr_add_docs.retry_with_fallback_models", fail_with_incomplete_diff)
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = FakeGitProvider()

    with pytest.raises(incomplete_error_class):
        asyncio.run(tool.run())

    assert tool.git_provider.initial_comment_removed
@pytest.mark.parametrize("retry_results", [[True, False], [False, True]])
def test_partial_inline_publication_keeps_every_retry_and_is_not_a_failure(
        publish_output, monkeypatch, retry_results):
    provider = FakeGitProvider(suggestion_results=[False, *retry_results])
    init_run_details()

    run(DOCUMENTED_TWICE, monkeypatch, provider)

    assert [len(suggestions) for suggestions in provider.suggestions] == [2, 1, 1]
    assert results(provider) == []
    assert command_failed() is False


def test_all_false_inline_publication_records_the_existing_command_failure(
        publish_output, monkeypatch):
    provider = FakeGitProvider(suggestion_results=[False, False, False])
    init_run_details()

    run(DOCUMENTED_TWICE, monkeypatch, provider)

    assert [len(suggestions) for suggestions in provider.suggestions] == [2, 1, 1]
    assert results(provider) == ["Failed to publish code documentation for this PR."]
    assert command_failed() is True


@pytest.mark.parametrize("suggestion_results", [[None, False, False], [False, None, False]])
def test_ambiguous_inline_publication_is_not_classified_as_terminal_failure(
        publish_output, monkeypatch, suggestion_results):
    provider = FakeGitProvider(suggestion_results=suggestion_results)
    init_run_details()

    run(DOCUMENTED_TWICE, monkeypatch, provider)

    assert [len(suggestions) for suggestions in provider.suggestions] == [2, 1, 1]
    assert results(provider) == []
    assert command_failed() is False


def test_aggregate_inline_publication_success_does_not_retry(publish_output, monkeypatch):
    provider = FakeGitProvider(suggestion_results=[True])
    init_run_details()

    run(DOCUMENTED_TWICE, monkeypatch, provider)

    assert [len(suggestions) for suggestions in provider.suggestions] == [2]
    assert results(provider) == []
    assert command_failed() is False


def test_filtered_documentation_preserves_one_empty_aggregate_call(publish_output, monkeypatch):
    provider = FakeGitProvider(suggestion_results=[False])
    init_run_details()

    run(FILTERED_DOCUMENTATION, monkeypatch, provider)

    assert provider.suggestions == [[]]
    assert results(provider) == []
    assert command_failed() is False


def test_terminal_failure_comment_error_is_recorded_not_reclassified(publish_output, monkeypatch):
    provider = FakeGitProvider(
        suggestion_results=[False, False, False],
        comment_error="Failed to publish code documentation",
    )
    init_run_details()

    run(DOCUMENTED_TWICE, monkeypatch, provider)

    assert [len(suggestions) for suggestions in provider.suggestions] == [2, 1, 1]
    assert command_failed() is True


def test_terminal_failure_comment_error_propagates_when_configured(publish_output, monkeypatch):
    provider = FakeGitProvider(
        suggestion_results=[False, False, False],
        comment_error="Failed to publish code documentation",
    )
    previous = get_settings().get("config.propagate_tool_errors", False)
    get_settings().set("config.propagate_tool_errors", True)
    init_run_details()
    try:
        with pytest.raises(RuntimeError, match="fallback comment unavailable"):
            run(DOCUMENTED_TWICE, monkeypatch, provider)
    finally:
        get_settings().set("config.propagate_tool_errors", previous)

    assert command_failed() is True


def test_no_documentation_comment_return_value_is_not_terminal_publication_failure(
        publish_output, monkeypatch):
    provider = FakeGitProvider(comment_result=False)
    init_run_details()

    run("Code Documentation:\n", monkeypatch, provider)

    assert results(provider) == ["No code documentation found to improve this PR."]
    assert command_failed() is False


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
