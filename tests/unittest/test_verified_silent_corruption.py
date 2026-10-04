"""Regression tests for three defects that silently produced wrong output.

Each was found by running the affected code, and each is reproduced here against the real
implementation rather than a stub, so the test fails on the pre fix code.

1. ``azuredevops_provider.get_diff_files`` fed an empty file side into
   ``load_large_diff``, which renders an empty side as a whole file addition or deletion, so
   a transient fetch failure became invented changes in the review.
2. ``github_provider.get_pr_labels`` reported a failed read as an empty list, which callers
   could not tell apart from an unlabeled PR. Because ``publish_labels`` replaces the entire
   label set, one API error deleted every label a human had added.
3. ``generate_summarized_suggestions`` swallowed every rendering error and returned an empty
   string, which the caller then used to overwrite the persistent review.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from github import GithubException

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import load_large_diff
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from pr_agent.log import get_logger
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from pr_agent.tools.pr_reviewer import PRReviewer

# ---------------------------------------------------------------------------
# 1. Azure DevOps must not invent a whole file addition or deletion
# ---------------------------------------------------------------------------


def test_load_large_diff_renders_an_empty_side_as_a_whole_file_change():
    """The mechanism the provider must not rely on when a fetch failed.

    A half empty pair is not a partial diff. It is a claim that the file was entirely added
    or entirely deleted, which is why a failed fetch cannot be represented this way.
    """
    assert load_large_diff("f.py", "", "old\n", show_warning=False) == "@@ -1 +0,0 @@\n-old\n"
    assert load_large_diff("f.py", "new\n", "", show_warning=False) == "@@ -0,0 +1 @@\n+new\n"


def _azure_change(change_type="edit"):
    return SimpleNamespace(
        additional_properties={
            "item": {"path": "/src/app.py", "gitObjectType": "blob"},
            "changeType": change_type,
        }
    )


def _azure_provider(*get_item_results, change=None):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.repo_slug = "my-repo"
    provider.workspace_slug = "my-project"
    provider.pr_num = 1
    provider.pr = SimpleNamespace(
        last_merge_target_commit=SimpleNamespace(commit_id="base-sha"),
        last_merge_commit=SimpleNamespace(commit_id="head-sha"),
    )
    provider.azure_devops_client = MagicMock()
    provider.azure_devops_client.get_pull_request_iterations.return_value = [SimpleNamespace(id=7)]
    provider.azure_devops_client.get_pull_request_iteration_changes.side_effect = [
        SimpleNamespace(change_entries=[change or _azure_change()], next_skip=0, next_top=0)
    ]
    provider.azure_devops_client.get_item.side_effect = get_item_results
    provider.diff_files = None
    provider._diff_path_map = None
    provider._pr_iteration_changes_cache = None
    provider.incremental = None
    provider.unreviewed_files_map = {}
    return provider


def _diff_for(change, *get_item_results):
    provider = _azure_provider(*get_item_results, change=change)
    return provider.get_diff_files()


def test_azure_failed_head_fetch_emits_no_patch():
    diff_files = _diff_for(_azure_change(), Exception("head fetch failed"),
                           SimpleNamespace(content="old content\n"))

    assert len(diff_files) == 1, "the file must stay in the diff so it is not a blind spot"
    assert diff_files[0].filename == "/src/app.py"
    assert diff_files[0].edit_type == EDIT_TYPE.MODIFIED
    # Regression: this used to be the whole file rendered as deleted.
    assert diff_files[0].patch == ""


def test_azure_failed_base_fetch_emits_no_patch():
    diff_files = _diff_for(_azure_change(), SimpleNamespace(content="new content\n"),
                           Exception("base fetch failed"))

    assert len(diff_files) == 1
    assert diff_files[0].edit_type == EDIT_TYPE.MODIFIED
    # Regression: this used to be the whole file rendered as added.
    assert diff_files[0].patch == ""


def test_azure_failed_fetch_reports_no_line_counts():
    diff_file = _diff_for(_azure_change(), Exception("head fetch failed"),
                          SimpleNamespace(content="old content\n"))[0]

    assert diff_file.num_plus_lines == 0
    assert diff_file.num_minus_lines == 0


def test_azure_failed_fetch_is_flagged_on_the_diff_entry():
    """The empty patch alone is not enough: downstream would drop the file without complaint."""
    diff_file = _diff_for(_azure_change(), Exception("head fetch failed"),
                          SimpleNamespace(content="old content\n"))[0]

    assert diff_file.content_fetch_failed is True


def test_azure_healthy_fetch_is_not_flagged():
    diff_file = _diff_for(_azure_change(), SimpleNamespace(content="new content\n"),
                          SimpleNamespace(content="old content\n"))[0]

    assert diff_file.content_fetch_failed is False


def test_unreadable_file_reaches_the_model_instead_of_being_dropped():
    """Regression: an empty patch is skipped by diff generation, so the file vanished silently."""
    from pr_agent.algo.pr_processing import pr_generate_extended_diff

    unreadable = FilePatchInfo("old\n", "", patch="", filename="/src/app.py",
                               edit_type=EDIT_TYPE.MODIFIED, content_fetch_failed=True)
    healthy = FilePatchInfo("old\n", "new\n", patch="@@ -1 +1 @@\n-old\n+new\n",
                            filename="/src/ok.py", edit_type=EDIT_TYPE.MODIFIED)
    token_handler = MagicMock()
    token_handler.count_tokens.return_value = 10

    patches, _, _, _ = pr_generate_extended_diff(
        [{"files": [unreadable, healthy]}], token_handler, add_line_numbers_to_hunks=False)

    rendered = "\n".join(patches)
    assert "could not be read" in rendered
    assert "/src/app.py" in rendered
    # The healthy file must still render as a real diff.
    assert "-old" in rendered


def test_unreadable_file_notice_has_no_duplicate_file_header():
    """Regression: without line numbers generate_full_patch writes the header itself, so the
    notice must not carry a second one."""
    from pr_agent.algo.pr_processing import pr_generate_compressed_diff

    unreadable = FilePatchInfo("old\n", "", patch="", filename="/src/app.py",
                               edit_type=EDIT_TYPE.MODIFIED, content_fetch_failed=True)
    unreadable.tokens = 10
    langs = [{"language": "Python", "files": [unreadable]}]
    token_handler = MagicMock()
    token_handler.count_tokens.return_value = 10
    token_handler.prompt_tokens = 0

    patches_list, _, _, _, _, _ = pr_generate_compressed_diff(
        langs, token_handler, soft_token_budget=10000, hard_token_budget=10000,
        convert_hunks_to_line_numbers=False, large_pr_handling=False,
    )

    rendered = "".join(patches_list[0])
    assert "could not be read" in rendered
    assert rendered.count("## File:") == 1, f"duplicated header:\n{rendered}"


def test_azure_healthy_fetch_still_emits_a_real_patch():
    diff_file = _diff_for(_azure_change(), SimpleNamespace(content="new content\n"),
                          SimpleNamespace(content="old content\n"))[0]

    assert "-old content" in diff_file.patch
    assert "+new content" in diff_file.patch


def test_azure_deleted_file_keeps_its_legitimately_empty_head():
    """A deletion really does have empty head content, so it must not count as a failure."""
    diff_file = _diff_for(_azure_change("delete"), Exception("head is gone"),
                          SimpleNamespace(content="deleted content\n"))[0]

    assert diff_file.edit_type == EDIT_TYPE.DELETED
    assert diff_file.patch, "a real deletion still produces a patch"


def test_azure_added_file_keeps_its_legitimately_empty_base():
    diff_file = _diff_for(_azure_change("add"), SimpleNamespace(content="brand new\n"),
                          Exception("base is never read"))[0]

    assert diff_file.edit_type == EDIT_TYPE.ADDED
    assert diff_file.patch, "a real addition still produces a patch"


# ---------------------------------------------------------------------------
# 2. A failed label read must not look like an unlabeled PR
# ---------------------------------------------------------------------------


def _label_provider(response=None, error=None):
    def request(*args, **kwargs):
        if error is not None:
            raise error
        return {}, response

    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.pr = SimpleNamespace(
        issue_url="https://api.github.com/repos/owner/repo/issues/1",
        labels=response,
        _requester=SimpleNamespace(requestJsonAndCheck=request),
    )
    return provider


def test_label_read_failure_is_not_reported_as_no_labels():
    provider = _label_provider(error=GithubException(500, {"message": "boom"}, {}))

    # Regression: this used to return [], which callers turned into a PUT that replaced
    # every label on the PR.
    assert provider.get_pr_labels(update=True) is None


def test_label_read_failure_does_not_reuse_a_stale_previous_read():
    """Regression: the earlier snapshot could be missing a label added since that read.

    Callers publish a whole set replacement, so serving a stale set would drop exactly the
    labels this guard exists to protect.
    """
    provider = _label_provider(response=[{"name": "bug"}, {"name": "priority/high"}])
    assert provider.get_pr_labels(update=True) == ["bug", "priority/high"]

    provider.pr._requester = SimpleNamespace(
        requestJsonAndCheck=lambda *a, **kw: (_ for _ in ()).throw(
            GithubException(500, {"message": "boom"}, {}))
    )
    assert provider.get_pr_labels(update=True) is None


def test_a_genuinely_unlabeled_pr_still_reads_as_empty():
    """An empty result is still meaningful when the read succeeded, so it is not None."""
    provider = _label_provider(response=[])

    assert provider.get_pr_labels(update=True) == []


def test_providers_without_label_support_keep_returning_a_list():
    """A real empty result must stay distinguishable from a failed read."""
    assert PlainDiffGitProvider.get_pr_labels(object.__new__(PlainDiffGitProvider)) == []


@pytest.mark.asyncio
async def test_generate_labels_skips_publishing_when_the_read_failed():
    """Regression: an unreadable label set used to be published as an empty one, wiping labels."""
    settings = get_settings(use_context=False)
    original = settings.config.publish_output
    settings.config.publish_output = True
    try:
        provider = MagicMock()
        provider.is_supported.return_value = True
        # get_user_labels(None) is empty, so the pre fix path published the model labels alone.
        provider.get_pr_labels.return_value = None
        provider.publish_comment.return_value = MagicMock()

        tool = PRGenerateLabels.__new__(PRGenerateLabels)
        tool.git_provider = provider
        tool.token_handler = MagicMock()
        tool.pr_id = "repo#1"
        tool.patches_diff = None
        tool.prediction = None
        tool.data = None
        tool.variables = {}
        tool.ai_handler = SimpleNamespace()
        tool.vars = {"title": "Title", "branch": "f", "description": "d", "language": "Python",
                     "diff": "", "extra_instructions": "", "commit_messages_str": "",
                     "enable_custom_labels": False, "custom_labels_class": ""}
        tool._get_prediction = AsyncMock(return_value="labels:\n- bug fix\n")

        with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
            await tool.run()
    finally:
        settings.config.publish_output = original

    provider.publish_labels.assert_not_called()


def test_review_labels_skip_publishing_when_the_read_failed():
    """Regression: the review label path would otherwise publish over unreadable labels."""
    settings = get_settings(use_context=False)
    original = (
        settings.config.publish_output,
        settings.pr_reviewer.require_estimate_effort_to_review,
        settings.pr_reviewer.require_security_review,
        settings.pr_reviewer.enable_review_labels_effort,
        settings.pr_reviewer.enable_review_labels_security,
    )
    settings.config.publish_output = True
    settings.pr_reviewer.require_estimate_effort_to_review = True
    settings.pr_reviewer.require_security_review = False
    settings.pr_reviewer.enable_review_labels_effort = True
    settings.pr_reviewer.enable_review_labels_security = False
    try:
        provider = MagicMock()
        provider.is_supported.return_value = True
        provider.get_pr_labels.return_value = None

        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.git_provider = provider
        reviewer.pr_url = "https://example/pr/1"
        reviewer.set_review_labels({
            "review": {"estimated_effort_to_review_[1-5]": "3, moderate"}
        })
    finally:
        (settings.config.publish_output,
         settings.pr_reviewer.require_estimate_effort_to_review,
         settings.pr_reviewer.require_security_review,
         settings.pr_reviewer.enable_review_labels_effort,
         settings.pr_reviewer.enable_review_labels_security) = original

    provider.publish_labels.assert_not_called()


@pytest.mark.asyncio
async def test_describe_survives_an_unreadable_label_set(monkeypatch):
    """An unknown label set must not abort the run before the description is published.

    Regression: the read result was fed straight into get_user_labels and set(), so an
    unknown set raised TypeError and the whole /describe run failed after the model had
    already produced a description.
    """
    from pr_agent.tools import pr_description as pr_description_module

    settings = get_settings(use_context=False)
    tracked = {
        "publish_output": settings.config.publish_output,
        "propagate_tool_errors": settings.config.propagate_tool_errors,
        "publish_labels": settings.pr_description.publish_labels,
        "markers": settings.pr_description.use_description_markers,
        "semantic": settings.pr_description.enable_semantic_files_types,
    }
    settings.config.publish_output = True
    settings.config.propagate_tool_errors = False
    settings.pr_description.publish_labels = True
    settings.pr_description.use_description_markers = False
    settings.pr_description.enable_semantic_files_types = False
    try:
        provider = MagicMock()
        provider.get_pr_labels.return_value = None
        provider.is_supported.return_value = True
        provider.publish_comment.return_value = MagicMock(name="progress_comment")

        description = PRDescription.__new__(PRDescription)
        description.pr_id = "1"
        description.git_provider = provider
        description.vars = {"title": "A title"}
        description.prediction = "a description"
        description.data = {"labels": "enhancement"}
        description.file_label_dict = None
        description._prepare_data = MagicMock()
        description._prepare_labels = MagicMock(return_value=["enhancement"])

        monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())
        monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", AsyncMock())

        await description.run()
    finally:
        settings.config.publish_output = tracked["publish_output"]
        settings.config.propagate_tool_errors = tracked["propagate_tool_errors"]
        settings.pr_description.publish_labels = tracked["publish_labels"]
        settings.pr_description.use_description_markers = tracked["markers"]
        settings.pr_description.enable_semantic_files_types = tracked["semantic"]

    provider.publish_description.assert_called_once()
    provider.publish_labels.assert_not_called()


# ---------------------------------------------------------------------------
# 3. A rendering error must not overwrite the persistent review
# ---------------------------------------------------------------------------


def _suggestions_tool():
    patch = "@@ -1,3 +1,3 @@\n ctx\n-bad\n+good\n ctx2"
    diff_file = FilePatchInfo("a.py", "b.py", patch=patch, filename="a.py",
                              edit_type=EDIT_TYPE.MODIFIED)
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = SimpleNamespace(diff_files=[diff_file], is_supported=lambda feature: False)
    return tool


def _suggestion(**overrides):
    suggestion = {
        "relevant_file": "a.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "body": "fix this",
        "score": 9,
        "label": "bug",
        "label_name": "bug",
        "suggestion_content": "c",
        "existing_code": "bad",
        "improved_code": "good",
        "one_sentence_summary": "s",
    }
    suggestion.update(overrides)
    return {"code_suggestions": [suggestion]}


def test_summary_renders_a_table_for_a_well_formed_suggestion():
    out = _suggestions_tool().generate_summarized_suggestions(_suggestion())

    assert "<table" in out
    assert "one sentence" not in out


def test_non_numeric_score_is_not_swallowed_into_an_empty_review():
    """Regression: this returned "", and the caller overwrote the persistent review with it."""
    with pytest.raises(ValueError, match="summarized code suggestions"):
        _suggestions_tool().generate_summarized_suggestions(_suggestion(score="high"))


def test_missing_field_is_not_swallowed_into_an_empty_review():
    broken = _suggestion()
    del broken["code_suggestions"][0]["suggestion_content"]

    with pytest.raises(ValueError, match="summarized code suggestions"):
        _suggestions_tool().generate_summarized_suggestions(broken)


def test_empty_suggestion_list_still_reports_no_suggestions():
    """The legitimate no suggestions answer must keep working and is not an error."""
    out = _suggestions_tool().generate_summarized_suggestions({"code_suggestions": []})

    assert "No suggestions found" in out


def test_review_render_failure_is_logged_loudly():
    """The log used to be info level, which is invisible at default verbosity."""
    broken = _suggestion()
    del broken["code_suggestions"][0]["improved_code"]

    messages = []
    sink_id = get_logger().add(lambda message: messages.append(str(message)), format="{message}")
    try:
        with pytest.raises(ValueError):
            _suggestions_tool().generate_summarized_suggestions(broken)
    finally:
        get_logger().remove(sink_id)

    assert any("summarized code suggestions" in message for message in messages)
