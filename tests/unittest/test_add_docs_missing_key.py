"""Clear the progress comment when an /add_docs response carries no documentation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from opentelemetry.trace import StatusCode

import pr_agent.agent.pr_agent as pr_agent_module
from pr_agent.algo.run_details import command_failed, get_run_details, init_run_details
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError,
    IncompletePullRequestFilesError,
)
from pr_agent.tools.pr_add_docs import PRAddDocs, get_suggestion_fence
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

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
        self.failures = []
        self.suggestions = []
        self.initial_comment_removed = False
        self.diff_files = [FilePatchInfo(
            base_file="", head_file="def f():\n    return 1", patch="", filename="src/app.py"
        )]
        self.suggestion_results = list(suggestion_results or [])
        self.comment_error = comment_error
        self.comment_result = comment_result

    def publish_comment(self, body, **kwargs):
        self.comments.append(body)
        if self.comment_error and self.comment_error in body:
            raise RuntimeError("fallback comment unavailable")
        return self.comment_result

    def publish_tool_failure(self, tool, message):
        self.failures.append((tool, message))
        return self.publish_comment(message)

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
        return tool._prepare_pr_code_docs()

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
        return tool._prepare_pr_code_docs()

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


@pytest.mark.parametrize("separator,newline", [
    pytest.param("", "\n", id="lf"),
    pytest.param("", "\r\n", id="crlf"),
    pytest.param("\u2028", "\n", id="unicode-separator-lf"),
    pytest.param("\u2028", "\r\n", id="unicode-separator-crlf"),
    pytest.param("\f", "\n", id="form-feed"),
])
def test_published_documentation_preserves_git_source_lines(publish_output, monkeypatch, separator, newline):
    provider = FakeGitProvider()
    head = newline.join([f'TEXT = "left{separator}right"', "def f():", "    return 1", ""])
    provider.diff_files[0].head_file = head
    prediction = DOCUMENTED.replace("relevant line: 1", "relevant line: 2").replace("    before", "    after")

    run(prediction, monkeypatch, provider)

    assert [len(batch) for batch in provider.suggestions] == [1]
    suggestion = provider.suggestions[0][0]
    assert suggestion["relevant_file"] == "src/app.py"
    assert suggestion["relevant_lines_start"] == suggestion["relevant_lines_end"] == 2
    replacement = suggestion["body"].split("```suggestion\n", 1)[1].removesuffix("\n```")
    assert replacement == 'def f():\n    """Do the thing."""'
    # Accept the suggestion using Git's LF-based line positions, preserving inline separators.
    source_lines = head.split("\n")
    source_lines[1] = replacement
    compile("\n".join(source_lines), "src/app.py", "exec")


@pytest.mark.parametrize("include_valid", [False, True])
def test_missing_head_content_is_filtered_before_publication(include_valid):
    provider = FakeGitProvider()
    provider.diff_files.append(FilePatchInfo(
        base_file="", head_file="", patch="@@ -0,0 +1,2 @@\n+def g():\n+    return 2",
        filename="src/unloaded.py"
    ))
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = provider
    missing = {"relevant file": "src/unloaded.py", "relevant line": 1,
               "doc placement": "after", "documentation": '"""Return a value."""'}
    docs = [missing, {**missing, "relevant file": "src/app.py"}] if include_valid else [missing]

    tool.push_inline_docs({"Code Documentation": docs})

    published = [suggestion for batch in provider.suggestions for suggestion in batch]
    assert [suggestion["relevant_file"] for suggestion in published] == (["src/app.py"] if include_valid else [])
    if include_valid:
        assert 'def f():\n    """Return a value."""' in published[0]["body"]


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


def test_filtered_documentation_does_not_publish_an_empty_batch(publish_output, monkeypatch):
    provider = FakeGitProvider(suggestion_results=[False])
    init_run_details()

    run(FILTERED_DOCUMENTATION, monkeypatch, provider)

    assert provider.suggestions == []
    assert results(provider) == []
    assert command_failed() is False


def test_run_skips_without_recording_failure_when_all_changed_files_are_filtered(publish_output, monkeypatch):
    async def prepare_once(fn, **_kwargs):
        await fn("primary-model")

    monkeypatch.setattr("pr_agent.tools.pr_add_docs.retry_with_fallback_models", prepare_once)
    monkeypatch.setattr(
        "pr_agent.tools.pr_add_docs.AttemptTokenBudget.for_prompt_attempt",
        lambda *_args, **_kwargs: Mock(),
    )
    # A lockfile-only or asset-only PR has no diff left after file-type filtering.
    monkeypatch.setattr("pr_agent.tools.pr_add_docs.get_pr_diff", lambda *_args, **_kwargs: "")
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = FakeGitProvider()
    tool.ai_handler = Mock()
    tool.vars = {"diff": ""}
    tool.prediction = None
    init_run_details()

    asyncio.run(tool.run())

    assert results(tool.git_provider) == []
    assert tool.git_provider.suggestions == []
    assert tool.git_provider.initial_comment_removed
    assert command_failed() is False


@pytest.mark.parametrize("include_valid", [False, True])
def test_rejected_documentation_is_filtered_before_fence_and_publication(monkeypatch, include_valid):
    provider = FakeGitProvider()
    provider.diff_files = [FilePatchInfo(
        base_file="", head_file="def f():\n\treturn 1", patch="", filename="a.py", edit_type=EDIT_TYPE.MODIFIED
    )]
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = provider
    invalid = {"relevant file": "a.py", "relevant line": 1, "doc placement": "after",
               "documentation": '    """Summary.\n  Incompatible.\n    """'}
    valid = {**invalid, "documentation": '"""Example:\n```python\npass\n```\n"""'}
    fence = Mock(wraps=get_suggestion_fence)
    monkeypatch.setattr("pr_agent.tools.pr_add_docs.get_suggestion_fence", fence)

    result = tool.push_inline_docs({"Code Documentation": [invalid, valid] if include_valid else [invalid]})

    if not include_valid:
        assert result is None
        assert provider.suggestions == []
        fence.assert_not_called()
    else:
        assert result is True
        assert [len(batch) for batch in provider.suggestions] == [1]
        aligned = 'def f():\n\t"""Example:\n\t```python\n\tpass\n\t```\n\t"""'
        fence.assert_called_once_with(aligned)
        assert provider.suggestions[0][0]["body"] == (
            "**Suggestion:** Proposed documentation\n````suggestion\n" + aligned + "\n````"
        )


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


@pytest.fixture
def fallback_tool(publish_output, monkeypatch):
    settings = {
        "config.model": "primary-model",
        "config.fallback_models": ["fallback-model"],
        "config.propagate_tool_errors": False,
        "model_routing.enable": False,
        "openai.deployment_id": None,
        "openai.fallback_deployments": [],
    }
    snapshot = snapshot_settings(settings)
    for key, value in settings.items():
        get_settings().set(key, value)
    tool = PRAddDocs.__new__(PRAddDocs)
    tool.git_provider = FakeGitProvider()
    tool.ai_handler = Mock()
    tool.vars = {"diff": ""}
    tool.prediction = None
    tool._get_prediction = AsyncMock(return_value=DOCUMENTED)
    budget = Mock()
    budget.fit_prompt_variable.return_value = SimpleNamespace(
        optional_text="diff", system_prompt="system", user_prompt="user"
    )
    monkeypatch.setattr("pr_agent.tools.pr_add_docs.AttemptTokenBudget.for_prompt_attempt", lambda *_a, **_kw: budget)
    monkeypatch.setattr("pr_agent.tools.pr_add_docs.get_pr_diff", lambda *_a, **_kw: "diff")
    init_run_details()
    yield tool
    restore_settings(snapshot)


@pytest.mark.parametrize("prediction", [
    None,
    pytest.param([{"type": "text", "text": DOCUMENTED}], id="non-text-model-response"),
    "",
    "No documentation needed for this PR.\n",
    "documentation:\n- relevant file: src/app.py\n",
    "Code Documentation: not a list\n",
    "Code Documentation:\n- malformed entry\n- 123\n",
    pytest.param("Code Documentation: [{}]\n", id="empty-documentation-object"),
    pytest.param("Code Documentation:\n- documentation: missing location\n", id="missing-documentation-fields"),
    pytest.param(DOCUMENTED.replace("relevant file: |\n    src/app.py", "relevant file: null"), id="null-file"),
    pytest.param(DOCUMENTED.replace("relevant line: 1", "relevant line: not-an-integer"), id="invalid-line"),
    pytest.param(DOCUMENTED.replace("doc placement: |\n    before", "doc placement: null"), id="null-placement"),
    pytest.param(DOCUMENTED.replace('documentation: |\n    """Do the thing."""', 'documentation: null'),
                 id="null-documentation"),
    "::: not : valid : yaml :::\n\t- [",
])
def test_invalid_response_uses_fallback_before_publishing(fallback_tool, prediction):
    fallback_tool._get_prediction.side_effect = [prediction, DOCUMENTED]

    asyncio.run(fallback_tool.run())

    assert fallback_tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert [len(batch) for batch in fallback_tool.git_provider.suggestions] == [1]
    assert results(fallback_tool.git_provider) == []
    assert fallback_tool.git_provider.initial_comment_removed
    assert command_failed() is False


@pytest.mark.parametrize("prediction", ["[]", "Code Documentation: []", "Code Documentation:\n"])
def test_explicit_empty_response_does_not_use_fallback(fallback_tool, prediction):
    fallback_tool._get_prediction.return_value = prediction

    asyncio.run(fallback_tool.run())

    fallback_tool._get_prediction.assert_awaited_once_with("primary-model")
    assert fallback_tool.git_provider.suggestions == []
    assert results(fallback_tool.git_provider) == ["No code documentation found to improve this PR."]
    assert fallback_tool.git_provider.initial_comment_removed
    assert command_failed() is False


def test_mixed_documentation_entries_keep_valid_suggestions_without_fallback(fallback_tool):
    fallback_tool._get_prediction.return_value = DOCUMENTED.replace("Code Documentation:\n", "- malformed entry\n")

    asyncio.run(fallback_tool.run())

    fallback_tool._get_prediction.assert_awaited_once_with("primary-model")
    assert [len(batch) for batch in fallback_tool.git_provider.suggestions] == [1]
    assert command_failed() is False


def test_empty_documentation_value_does_not_use_fallback(fallback_tool):
    fallback_tool._get_prediction.return_value = FILTERED_DOCUMENTATION

    asyncio.run(fallback_tool.run())

    fallback_tool._get_prediction.assert_awaited_once_with("primary-model")
    assert fallback_tool.git_provider.suggestions == []
    assert results(fallback_tool.git_provider) == []
    assert command_failed() is False


@pytest.mark.parametrize("comment_error", [None, "Failed to generate code documentation"])
def test_exhausted_invalid_responses_report_failure_and_clear_progress(fallback_tool, comment_error):
    fallback_tool._get_prediction.return_value = "not a documentation response"
    fallback_tool.git_provider.comment_error = comment_error

    asyncio.run(fallback_tool.run())

    assert fallback_tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert fallback_tool.git_provider.suggestions == []
    assert results(fallback_tool.git_provider) == ["Failed to generate code documentation for this PR."]
    assert fallback_tool.git_provider.initial_comment_removed
    assert command_failed() is True
