"""The opt-in gate and wiring of the chunked `/review` flow.

The merge rules themselves live in tests/unittest/test_review_chunk_merge.py; what is
covered here is when chunking runs at all, what it does with a chunk that fails, and what
the published review says about having been assembled from several calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.git_patch_processing import quote_diff_filename
from pr_agent.algo.pr_processing import (
    PackedPRDiffs,
    PreparedPRDiff,
    _pack_pr_multi_diffs,
    add_ai_summary_top_patch,
    retry_with_fallback_models,
)
from pr_agent.algo.review_finding_state import ParsedReviewState, reconcile_review_findings
from pr_agent.algo.review_json_output import hosted_markdown_artifact, hosted_markdown_export
from pr_agent.algo.run_details import get_run_details
from pr_agent.algo.token_budget import FallbackEligibleError
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("pr_reviewer.enable_large_pr_chunking", "pr_reviewer.max_number_of_calls",
                 "pr_reviewer.num_max_findings")

CHUNK_A = """review:
  score: 90
  key_issues_to_review:
    - relevant_file: |
        a.py
      issue_header: |
        Possible Issue
      issue_content: |
        the index is never checked
      start_line: 3
      end_line: 4
  security_concerns: |
    No
"""

CHUNK_B = """review:
  score: 40
  key_issues_to_review: []
  security_concerns: |
    SQL injection: the query is built by string concatenation
"""


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.ai_handler = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.token_handler.prompt_tokens = 0
    reviewer.token_handler.count_tokens.side_effect = len
    reviewer.token_handler.system = ""
    reviewer.token_handler.user = "{{ diff }}"
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.partial_files_list = []
    reviewer.prediction = None
    return reviewer


def _make_file_notice_reviewer(source_count, patchless, user_template="{{ diff }}", include_notices=True):
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

    files = []
    for index in range(source_count):
        old = "".join(f"old_value_{line} = {line}\n" for line in range(300))
        new = "".join(f"source_marker_{line} = {line + 1}\n" for line in range(300))
        patch_text = "@@ -1,300 +1,300 @@\n"
        patch_text += "".join("-" + line for line in old.splitlines(keepends=True))
        patch_text += "".join("+" + line for line in new.splitlines(keepends=True))
        files.append(FilePatchInfo(old, new, patch_text, f"large{index}.py", edit_type=EDIT_TYPE.MODIFIED))
    if include_notices:
        files.append(FilePatchInfo("gone\n", "", "" if patchless else "@@ -1 +0,0 @@\n-gone\n",
                                   "gone.py", edit_type=EDIT_TYPE.DELETED))
    reviewer = _make_reviewer()
    reviewer.git_provider.get_diff_files.return_value = files
    reviewer.git_provider.get_languages.return_value = {"Python": 100}
    reviewer.git_provider.get_filtered_diff_file_names.return_value = ["vendor.lock"] if include_notices else []
    reviewer.vars = {"diff": ""}
    reviewer._raw_prompt_vars = reviewer.vars.copy()
    reviewer.token_handler = TokenHandler(None, reviewer.vars, "Review.", user_template, model="gpt-4o")
    reviewer.ai_handler = LiteLLMAIHandler()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=(CHUNK_A, "stop"))
    return reviewer


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [1, 2])
@pytest.mark.parametrize("patchless", [False, True])
async def test_source_chunks_retain_deleted_and_filtered_notices(chunking_enabled, monkeypatch,
                                                                source_count, patchless):
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    control = _make_file_notice_reviewer(source_count, patchless, include_notices=False)
    reviewer = _make_file_notice_reviewer(source_count, patchless)
    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await control._prepare_prediction("gpt-4o")
        await reviewer._prepare_prediction("gpt-4o")
        budget = reviewer._review_attempt_budget("gpt-4o")
        prompts = reviewer.ai_handler.chat_completion.await_args_list
        control_prompts = control.ai_handler.chat_completion.await_args_list
        assert len(prompts) == len(control_prompts) == source_count
        for call, control_call in zip(prompts, control_prompts, strict=True):
            assert call.kwargs["user"].startswith(control_call.kwargs["user"])
            assert "Deleted files:\n\ngone.py" in call.kwargs["user"]
            assert "vendor.lock" in call.kwargs["user"]
            request = budget.prepare_request(reviewer.ai_handler, call.kwargs["system"], call.kwargs["user"])
            assert request.input_tokens <= budget.input_token_limit(1000, preserve_minimum=True)
    assert reviewer.partial_files_list == [f"large{index}.py" for index in range(source_count)]
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [1, 2])
@pytest.mark.parametrize("notice_branch", [
    "{{ diff * 10 }}", "{{ missing_notice_variable }}", "{{ 1 / 0 }}", "{{ diff + 1 }}",
])
async def test_chunk_notices_cannot_displace_source_in_a_custom_prompt(
    chunking_enabled, monkeypatch, source_count, notice_branch,
):
    template = (
        "{% if 'Deleted files:' in diff or 'file-type filtering:' in diff %}"
        + notice_branch + "{% else %}{{ diff }}{% endif %}"
    )
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", template)
    control = _make_file_notice_reviewer(source_count, True, template, include_notices=False)
    reviewer = _make_file_notice_reviewer(source_count, True, template)
    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await control._prepare_prediction("gpt-4o")
        await reviewer._prepare_prediction("gpt-4o")
    prompts = reviewer.ai_handler.chat_completion.await_args_list
    control_prompts = control.ai_handler.chat_completion.await_args_list
    assert len(prompts) == len(control_prompts) == source_count
    assert [call.kwargs["user"] for call in prompts] == [call.kwargs["user"] for call in control_prompts]
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [1, 2])
async def test_optional_notices_cannot_abort_request_preparation(chunking_enabled, monkeypatch, source_count):
    from pr_agent.algo.token_budget import AttemptTokenBudget

    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(source_count, True)
    control = _make_file_notice_reviewer(source_count, True, include_notices=False)
    prepare_request = AttemptTokenBudget.prepare_request
    rejected = []

    def prepare(budget, ai_handler, system_prompt, user_prompt, **kwargs):
        if "Deleted files:" in user_prompt or "file-type filtering:" in user_prompt:
            rejected.append(user_prompt)
            raise TypeError("optional request preparation failed")
        return prepare_request(budget, ai_handler, system_prompt, user_prompt, **kwargs)

    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000),
        patch.object(AttemptTokenBudget, "prepare_request", prepare),
    ):
        await control._prepare_prediction("gpt-4o")
        await reviewer._prepare_prediction("gpt-4o")

    assert rejected
    prompts = reviewer.ai_handler.chat_completion.await_args_list
    control_prompts = control.ai_handler.chat_completion.await_args_list
    assert len(prompts) == len(control_prompts) == source_count
    assert [call.kwargs["user"] for call in prompts] == [call.kwargs["user"] for call in control_prompts]
    assert reviewer.remaining_files_list == ["gone.py"]

    def fail_source(budget, ai_handler, system_prompt, user_prompt, **kwargs):
        raise TypeError("required request preparation failed")

    with (
        patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000),
        patch.object(AttemptTokenBudget, "prepare_request", fail_source),
        pytest.raises(TypeError, match="required request preparation failed"),
    ):
        await reviewer._get_prediction("gpt-4o", "source_marker")

    assert reviewer.ai_handler.chat_completion.await_count == source_count


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", [False, True])
@pytest.mark.parametrize("source_size", ["none", "small", "large"])
@pytest.mark.parametrize("patchless", [True, False])
async def test_deletion_notice_and_coverage_with_or_without_chunking(chunking_enabled, monkeypatch,
                                                                   chunking, source_size, patchless):
    monkeypatch.setitem(get_settings().pr_reviewer, "enable_large_pr_chunking", chunking)
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(0 if source_size == "none" else 1, patchless)
    if source_size == "none":
        # Exercise a genuinely deletion-only PR, with no filtered-file context.
        reviewer.git_provider.get_filtered_diff_file_names.return_value = []
    if source_size == "small":
        reviewer.git_provider.get_diff_files.return_value[0] = FilePatchInfo(
            "old\n", "source_marker\n", "@@ -1 +1 @@\n-old\n+source_marker\n",
            "active.py", edit_type=EDIT_TYPE.MODIFIED,
        )

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await reviewer._prepare_prediction("gpt-4o")

    if source_size == "none":
        reviewer.ai_handler.chat_completion.assert_not_awaited()
        assert reviewer._review_done
        assert not reviewer.patches_diff
    else:
        reviewer.ai_handler.chat_completion.assert_awaited_once()
        call = reviewer.ai_handler.chat_completion.await_args
        assert "gone.py" in call.kwargs["user"]
        assert "vendor.lock" in call.kwargs["user"]
        assert "-gone" not in call.kwargs["user"]
        if source_size == "small" or chunking:
            assert "source_marker" in call.kwargs["user"]
    assert reviewer.remaining_files_list == (
        ["large0.py", "gone.py"] if source_size == "large" and not chunking else ["gone.py"]
    )
    assert reviewer.partial_files_list == (["large0.py"] if source_size == "large" and chunking else [])
    if not chunking:
        assert not hasattr(reviewer, "_chunked_patches_diff_list")


@pytest.mark.asyncio
@pytest.mark.parametrize("max_input,fits", [(1700, False), (4000, True)])
async def test_multiline_deletion_filename_is_an_atomic_notice(monkeypatch, max_input, fits):
    monkeypatch.setitem(get_settings().pr_reviewer, "enable_large_pr_chunking", False)
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(1, True)
    filename = "dir/first.py\n" + "segment/" * 400 + "tail.py"
    reviewer.git_provider.get_diff_files.return_value[-1].filename = filename
    reviewer.git_provider.get_filtered_diff_file_names.return_value = []

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=max_input):
        await reviewer._prepare_prediction("gpt-4o")

    assert filename in reviewer.remaining_files_list
    reviewer.ai_handler.chat_completion.assert_awaited_once()
    user = reviewer.ai_handler.chat_completion.await_args.kwargs["user"]
    assert (filename in user) is fits
    assert ("dir/first.py" in user) is fits
    assert "dir/first.py\\n" not in user


@pytest.mark.asyncio
async def test_reusing_a_reviewer_clears_deleted_file_notices():
    reviewer = _make_reviewer()
    reviewer._review_deleted_files = ["old.py"]
    reviewer.git_provider.get_files.return_value = []

    await reviewer.run()

    assert not hasattr(reviewer, "_review_deleted_files")


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,patterns", [
    ("glob", ["generated/**"]), ("regex", [r"^generated/"]), ("regex", ["[", r"^generated/"]),
])
@pytest.mark.parametrize("source_count", [0, 1])
async def test_patchless_deletion_notices_respect_path_ignores(chunking_enabled, monkeypatch,
                                                             rule, patterns, source_count):
    monkeypatch.setitem(get_settings().ignore, rule, patterns)
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(source_count, True)
    reviewer.git_provider.get_diff_files.return_value.append(
        FilePatchInfo("secret\n", "", "", "generated/hidden.py", edit_type=EDIT_TYPE.DELETED)
    )

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await reviewer._prepare_prediction("gpt-4o")

    if source_count:
        user = reviewer.ai_handler.chat_completion.await_args.kwargs["user"]
        assert "gone.py" in user
        assert "generated/hidden.py" not in user
    else:
        # Deletions plus filtered filenames cannot trigger a notice-only call.
        reviewer.ai_handler.chat_completion.assert_not_awaited()
        assert reviewer._review_done
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [1, 2])
@pytest.mark.parametrize("patchless,fetch_failed", [(False, False), (True, False), (True, True)])
async def test_fallback_keeps_deleted_source_unreviewed_and_reuses_successes(chunking_enabled, monkeypatch,
                                                                          source_count, patchless, fetch_failed):
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(source_count, patchless)
    reviewer.git_provider.get_diff_files.return_value[-1].content_fetch_failed = fetch_failed
    reviewer.ai_handler.chat_completion.side_effect = (
        [(CHUNK_A, "stop")] * (source_count - 1) + [RuntimeError("model refused")]
    )

    with patch("pr_agent.algo.token_budget.get_max_tokens",
               side_effect=lambda model, **kwargs: 4000 if model == "gpt-4o" else 20000):
        with pytest.raises(RuntimeError, match="model refused"):
            await reviewer._prepare_prediction("gpt-4o")
        reviewer.ai_handler.chat_completion.side_effect = None
        await reviewer._prepare_prediction("gpt-4o-mini")

    calls = reviewer.ai_handler.chat_completion.await_args_list
    assert len(calls) == source_count + 1
    assert ("large0.py" in calls[-1].kwargs["user"]) is (source_count == 1)
    assert f"large{source_count - 1}.py" in calls[-1].kwargs["user"]
    assert "gone.py" in calls[-1].kwargs["user"]
    # The line-number renderer never emits deleted source, even on a full-diff retry.
    assert "-gone" not in calls[-1].kwargs["user"]
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", [False, True])
async def test_fallback_retains_previously_known_deletions(chunking_enabled, monkeypatch, chunking):
    monkeypatch.setitem(get_settings().pr_reviewer, "enable_large_pr_chunking", chunking)
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(1, True)
    reviewer.ai_handler.chat_completion.side_effect = [RuntimeError("retry"), (CHUNK_A, "stop")]

    with patch("pr_agent.algo.token_budget.get_max_tokens",
               side_effect=lambda model, **kwargs: 4000 if chunking and model == "gpt-4o" else 20000):
        with pytest.raises(RuntimeError, match="retry"):
            await reviewer._prepare_prediction("gpt-4o")
        assert reviewer.remaining_files_list == ["gone.py"]
        assert "gone.py" in reviewer.ai_handler.chat_completion.await_args.kwargs["user"]

        # Even if subsequent preparation no longer lists a deletion, it has not
        # been reviewed. Retain the known omission for this run.
        reviewer.git_provider.get_diff_files.return_value.pop()
        await reviewer._prepare_prediction("gpt-4o-mini")

    assert reviewer.ai_handler.chat_completion.await_count == 2
    user = reviewer.ai_handler.chat_completion.await_args.kwargs["user"]
    assert "source_marker" in user
    assert "gone.py" in user
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
async def test_cached_fallback_tracks_newly_prepared_deletions(chunking_enabled, monkeypatch):
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(2, True, include_notices=False)
    reviewer.ai_handler.chat_completion.side_effect = [(CHUNK_A, "stop"), RuntimeError("retry")]

    with patch("pr_agent.algo.token_budget.get_max_tokens",
               side_effect=lambda model, **kwargs: 4000 if model == "gpt-4o" else 20000):
        with pytest.raises(RuntimeError, match="retry"):
            await reviewer._prepare_prediction("gpt-4o")
        reviewer.git_provider.get_diff_files.return_value.append(
            FilePatchInfo("gone\n", "", "", "gone.py", edit_type=EDIT_TYPE.DELETED),
        )
        reviewer.ai_handler.chat_completion.side_effect = None
        await reviewer._prepare_prediction("gpt-4o-mini")

    assert reviewer.ai_handler.chat_completion.await_count == 3
    user = reviewer.ai_handler.chat_completion.await_args.kwargs["user"]
    assert "large0.py" not in user
    assert "large1.py" in user
    assert "gone.py" in user
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", [False, True])
async def test_filtered_only_pr_still_skips_model_calls(chunking_enabled, monkeypatch, chunking):
    monkeypatch.setitem(get_settings().pr_reviewer, "enable_large_pr_chunking", chunking)
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(0, False, include_notices=False)
    reviewer.git_provider.get_filtered_diff_file_names.return_value = ["uv.lock"]

    await reviewer._prepare_prediction("gpt-4o")

    reviewer.ai_handler.chat_completion.assert_not_awaited()
    assert reviewer.remaining_files_list == []


@pytest.mark.asyncio
async def test_file_notices_do_not_hide_a_source_request_overflow(chunking_enabled, monkeypatch):
    template = "{{ diff * 10 }}"
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", template)
    reviewer = _make_file_notice_reviewer(1, True, template)

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        with pytest.raises(FallbackEligibleError, match="required prompt exceeds"):
            await reviewer._prepare_prediction("gpt-4o")

    reviewer.ai_handler.chat_completion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", [False, True])
async def test_full_fitting_source_survives_oversized_deletion_metadata(chunking_enabled, monkeypatch, chunking):
    monkeypatch.setitem(get_settings().pr_reviewer, "enable_large_pr_chunking", chunking)
    template = "{% if 'gone.py' in diff %}{{ diff * 200 }}{% else %}{{ diff }}{% endif %}"
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", template)
    reviewer = _make_file_notice_reviewer(1, True, template)
    reviewer.git_provider.get_filtered_diff_file_names.return_value = []
    reviewer.git_provider.get_diff_files.return_value[0] = FilePatchInfo(
        "old\n", "source_marker\n", "@@ -1 +1 @@\n-old\n+source_marker\n",
        "active.py", edit_type=EDIT_TYPE.MODIFIED,
    )

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await reviewer._prepare_prediction("gpt-4o")

    reviewer.ai_handler.chat_completion.assert_awaited_once()
    user = reviewer.ai_handler.chat_completion.await_args.kwargs["user"]
    assert "source_marker" in user
    assert "gone.py" not in user
    assert reviewer.remaining_files_list == ["gone.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("multiline_filtered", [False, True])
async def test_chunked_notices_keep_filtered_context_with_many_deletions(
    chunking_enabled, monkeypatch, multiline_filtered,
):
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")
    monkeypatch.setitem(get_settings().pr_review_prompt, "system", "Review.")
    monkeypatch.setitem(get_settings().pr_review_prompt, "user", "{{ diff }}")
    reviewer = _make_file_notice_reviewer(2, True)
    names = [f"archive/old_package/legacy_component/deleted_{index}.py" for index in range(300)]
    reviewer.git_provider.get_diff_files.return_value.extend(
        FilePatchInfo("old", "", "", name, edit_type=EDIT_TYPE.DELETED) for name in names
    )
    filtered = ["uv.lock"]
    if multiline_filtered:
        filtered.append("dir/first_fragment.lock\n" + "segment/" * 1000 + "tail.lock")
    reviewer.git_provider.get_filtered_diff_file_names.return_value = filtered

    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=4000):
        await reviewer._prepare_prediction("gpt-4o")

    assert reviewer.ai_handler.chat_completion.await_count == 2
    for call in reviewer.ai_handler.chat_completion.await_args_list:
        user = call.kwargs["user"]
        assert "source_marker" in user
        assert "uv.lock" in user
        assert "gone.py" in user
        assert "dir/first_fragment.lock" not in user
        assert names[-1] not in user
    assert set(reviewer.remaining_files_list) == {"gone.py", *names}


@pytest.fixture
def chunking_enabled():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    get_settings().set("pr_reviewer.enable_large_pr_chunking", True)
    get_settings().set("pr_reviewer.max_number_of_calls", 3)
    with patch("pr_agent.algo.token_budget.get_max_tokens", return_value=10000):
        yield
    restore_settings(snapshot)


@pytest.mark.asyncio
async def test_chunking_is_off_by_default_even_when_the_token_budget_truncated_the_diff():
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["left_out.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs") as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_not_called()
    assert reviewer.prediction == CHUNK_A
    assert reviewer.prediction_data is None
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_a_diff_that_fits_is_never_chunked(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", [])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs") as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_not_called()
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_a_truncated_diff_is_reviewed_chunk_by_chunk_and_merged(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], ["still_left_out.py"], [])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    get_pr_multi_diffs.assert_called_once_with(
        reviewer.git_provider,
        reviewer.token_handler,
        "model",
        max_calls=3,
        add_line_numbers=True,
        return_coverage=True,
        include_filtered_file_names=False,
        output_token_reserve=reviewer.ai_handler.get_output_token_reserve,
    )
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == ["chunk-a", "chunk-b"]

    review = reviewer.prediction_data["review"]
    assert review["score"] == 40  # the worst chunk sets the score
    assert [issue["relevant_file"].strip() for issue in review["key_issues_to_review"]] == ["a.py"]
    assert review["security_concerns"].startswith("SQL injection:")
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    # the coverage footer keeps reporting what even chunking could not fit
    assert reviewer.remaining_files_list == ["still_left_out.py"]


def _findings_chunk(prefix, count):
    lines = ["review:", "  score: 50", "  key_issues_to_review:"]
    for index in range(count):
        lines += [
            "    - relevant_file: |",
            f"        {prefix}{index}.py",
            "      issue_header: |",
            f"        Issue {prefix}{index}",
            "      issue_content: |",
            f"        problem {index} in {prefix}",
            "      start_line: 10",
            "      end_line: 12",
        ]
    lines += ["  security_concerns: |", "    No", ""]
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_chunked_review_caps_findings_to_the_configured_limit(chunking_enabled):
    """Every chunk may report its own full allowance, so the merged review must be cut back."""
    get_settings().set("pr_reviewer.num_max_findings", 3)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(
        side_effect=[_findings_chunk("a", 3), _findings_chunk("b", 3)])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
    ):
        await reviewer._prepare_prediction("model")

    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert len(issues) == 3
    # chunk order wins, so the kept findings are the whole of the first chunk
    assert [issue["relevant_file"].strip() for issue in issues] == ["a0.py", "a1.py", "a2.py"]


@pytest.mark.asyncio
async def test_a_capped_chunked_review_does_not_resolve_earlier_findings(chunking_enabled):
    """A finding cut by the limit is left out, not absent, so resolution must stay off."""
    get_settings().set("pr_reviewer.num_max_findings", 3)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(
        side_effect=[_findings_chunk("a", 3), _findings_chunk("b", 3)])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
    ):
        await reviewer._prepare_prediction("model")

    previous = reconcile_review_findings(
        None,
        [{"path": "fixed.py", "body": "old fixed problem", "line_start": 3, "line_end": 4}],
        allow_resolution=False,
        head_sha="head-1",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"

    reviewer._prepare_review_finding_state(reviewer.prediction_data)

    state = reviewer._review_state_result.state
    assert state["last_run"]["complete"] is False
    assert next(f for f in state["findings"] if f["path"] == "fixed.py")["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_final_fit_clipping_marks_a_review_chunk_failed(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._raw_prompt_vars = None
    reviewer.vars = {"diff": ""}
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=(CHUNK_A, "stop"))

    class SelectiveBudget:
        def fit_prompt_variable(self, _variables, _name, optional_text, **_kwargs):
            fitted_text = optional_text[:-1] if optional_text == "chunk-b" else optional_text
            return SimpleNamespace(
                optional_text=fitted_text,
                system_prompt="system",
                user_prompt="user",
            )

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch(
            "pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
            return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], []),
        ),
        patch(
            "pr_agent.commands.pr_reviewer.AttemptTokenBudget.for_attempt",
            return_value=SelectiveBudget(),
        ),
        pytest.raises(ValueError, match="complete packed review diff"),
    ):
        await reviewer._prepare_prediction("model")

    assert list(reviewer._chunked_results) == [0]
    assert reviewer.prediction is None
    reviewer.ai_handler.chat_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_chunked_review_reuses_the_prepared_diff_for_the_same_model_attempt(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])
    prepared = PreparedPRDiff(
        diff="first compressed diff",
        remaining_files_list=["b.py"],
        file_dict={"a.py": {"patch": "chunk-a", "tokens": 10}},
    )

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=prepared) as get_pr_diff,
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], ["still_left_out.py"], [])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    assert get_pr_diff.call_args.kwargs["return_prepared"] is True
    assert get_pr_multi_diffs.call_args.kwargs["prepared_diff"] is prepared
    assert get_pr_diff.call_args.kwargs["output_token_reserve"] is reviewer.ai_handler.get_output_token_reserve
    assert get_pr_multi_diffs.call_args.kwargs["output_token_reserve"] is reviewer.ai_handler.get_output_token_reserve
    assert reviewer.review_chunk_count == 2
    assert reviewer.remaining_files_list == ["still_left_out.py"]


@pytest.mark.asyncio
async def test_max_number_of_calls_bounds_the_number_of_chunks(chunking_enabled):
    get_settings().set("pr_reviewer.max_number_of_calls", 7)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])) as get_pr_multi_diffs,
    ):
        await reviewer._prepare_prediction("model")

    assert get_pr_multi_diffs.call_args.kwargs["max_calls"] == 7


@pytest.mark.asyncio
async def test_a_diff_that_fits_in_one_chunk_is_reviewed_by_the_single_call_flow(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer.partial_files_list = ["stale-from-prior-attempt.py"]
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["only-chunk"], [], ["unused.py"])),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.assert_awaited_once_with("model")
    assert reviewer.prediction == CHUNK_A
    assert reviewer.prediction_data is None
    assert reviewer.review_chunk_count == 1
    assert reviewer.patches_diff == "only-chunk"
    assert reviewer.remaining_files_list == []
    assert reviewer.partial_files_list == ["unused.py"]
    assert not any(hasattr(reviewer, name) for name in (
        "_chunked_patches_diff_list", "_chunked_remaining_files_list",
        "_chunked_partial_files_list", "_chunked_results",
    ))


@pytest.mark.asyncio
async def test_smaller_fallback_replans_a_single_clipped_chunk(chunking_enabled):
    reviewer = _make_reviewer()
    old_lines = [f"old_value_{i} = {i}\n" for i in range(300)]
    new_lines = [f"source_marker_{i} = {i + 1}\n" for i in range(300)]
    source_patch = "@@ -1,300 +1,300 @@\n" + "".join("-" + line for line in old_lines)
    source_patch += "".join("+" + line for line in new_lines)
    reviewer.git_provider.pr = SimpleNamespace(title="PR")
    reviewer.git_provider.get_diff_files.return_value = [FilePatchInfo(
        "".join(old_lines), "".join(new_lines), source_patch, "large.py", edit_type=EDIT_TYPE.MODIFIED,
    )]
    reviewer.git_provider.get_languages.return_value = {"Python": 100}
    reviewer.git_provider.get_filtered_diff_file_names.return_value = []
    reviewer.vars = {"diff": ""}
    reviewer._raw_prompt_vars = reviewer.vars.copy()
    requests = []

    async def chat_completion(*, model, user, **kwargs):
        requests.append((model, user))
        if model == "gpt-4o":
            raise FallbackEligibleError("Primary request failed")
        return CHUNK_A, "stop"

    reviewer.ai_handler = SimpleNamespace(
        chat_completion=chat_completion,
        get_output_token_reserve=lambda model, default: default,
    )
    with (
        patch.dict(get_settings().config, {
            "model": "gpt-4o", "fallback_models": ["gpt-4o-mini"],
            "large_patch_policy": "clip", "patch_extra_lines_before": 0, "patch_extra_lines_after": 0,
        }),
        patch.dict(get_settings().pr_review_prompt, {"system": "Review the source changes.", "user": "{{ diff }}"}),
        patch("pr_agent.algo.token_budget.get_max_tokens",
              side_effect=lambda model, **kwargs: 4000 if model == "gpt-4o" else 2400),
    ):
        await retry_with_fallback_models(reviewer._prepare_prediction)

    assert isinstance(reviewer.token_handler, TokenHandler)
    assert [model for model, _ in requests] == ["gpt-4o", "gpt-4o-mini"]
    assert 0 < requests[1][1].count("source_marker_") < requests[0][1].count("source_marker_")
    assert reviewer.partial_files_list == ["large.py"]
    assert reviewer.remaining_files_list == []
    assert reviewer._chunked_primary_model == "gpt-4o"
    assert reviewer.review_chunk_count == 1


@pytest.mark.asyncio
async def test_larger_fallback_uses_fresh_full_diff_after_primary_single_clipped_chunk(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("primary failed"), CHUNK_A])
    clipped_chunk = "## File: 'large.py'\n+first change\n"
    full_diff = clipped_chunk + "+change beyond primary budget\n"

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", side_effect=[
            PreparedPRDiff("only filenames", ["large.py"]),
            PreparedPRDiff(full_diff, []),
        ]) as get_diff,
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs([clipped_chunk], [], ["large.py"])) as get_multi,
    ):
        with pytest.raises(RuntimeError, match="primary failed"):
            await reviewer._prepare_prediction("primary")
        assert reviewer.patches_diff == clipped_chunk
        assert reviewer.partial_files_list == ["large.py"]
        await reviewer._prepare_prediction("fallback")

    assert get_diff.call_count == 2
    get_multi.assert_called_once()
    assert [call.args for call in reviewer._get_prediction.await_args_list] == [
        ("primary",), ("fallback",),
    ]
    assert reviewer.patches_diff == full_diff
    assert reviewer.prediction == CHUNK_A
    assert reviewer.remaining_files_list == []
    assert reviewer.partial_files_list == []


@pytest.mark.asyncio
async def test_a_chunk_that_fails_does_not_lose_the_chunks_that_succeeded(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.side_effect = [CHUNK_A]
    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-a",
    ]


@pytest.mark.asyncio
async def test_a_failed_chunk_recovery_preserves_finding_state_lifecycle(chunking_enabled):
    """Verify that a recovered chunked review participates in the finding-state lifecycle."""
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.side_effect = [CHUNK_A]
    await reviewer._prepare_chunked_prediction("model")

    previous_state = reconcile_review_findings(
        None,
        [{"path": "a.py", "body": "old finding", "line_start": 3, "line_end": 4}],
        allow_resolution=False,
        head_sha="head-1",
        timestamp="2026-09-09T00:00:00+00:00",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(
        previous_state, present=True, valid=True
    )
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"

    reviewer._prepare_review_finding_state(reviewer.prediction_data)

    assert reviewer._review_state_result is not None
    assert reviewer._review_state_result.state["last_run"]["complete"] is True
    assert reviewer._review_state_result.state["findings"][0]["state"] == "RESOLVED"


@pytest.mark.asyncio
async def test_a_malformed_chunk_is_retried_without_repeating_successful_chunks(chunking_enabled):
    reviewer = _make_reviewer()
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, "review: {}", chunk_c])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b", "chunk-c"], [], [])),
        pytest.raises(ValueError, match="non-empty review"),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 3
    assert reviewer.prediction_data is None

    reviewer._get_prediction.side_effect = [CHUNK_B]
    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 0
    assert reviewer.prediction_data["review"]["score"] == 40
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-c", "chunk-b",
    ]


@pytest.mark.asyncio
async def test_hosted_export_captures_completed_review_from_reviewer():
    reviewer = _make_reviewer()
    reviewer.vars = {}
    reviewer._get_prediction = AsyncMock(return_value=CHUNK_A)
    settings_values = {
        "config.model": "primary",
        "config.fallback_models": [],
        "config.publish_output": False,
        "pr_reviewer.enable_large_pr_chunking": False,
    }
    snapshot = snapshot_settings(settings_values)
    try:
        for key, value in settings_values.items():
            get_settings().set(key, value)
        with (
            patch("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", new_callable=AsyncMock),
            patch("pr_agent.commands.pr_reviewer.fit_related_tickets_to_prompt_budget",
                  return_value=({}, reviewer.token_handler)),
            patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", [])),
            patch.object(reviewer, "_prepare_pr_review", return_value="# Complete review"),
        ):
            export_token = hosted_markdown_export.set(True)
            artifact_token = hosted_markdown_artifact.set(None)
            try:
                await reviewer._run_review()
                assert hosted_markdown_artifact.get() == "# Complete review"
            finally:
                hosted_markdown_artifact.reset(artifact_token)
                hosted_markdown_export.reset(export_token)
    finally:
        restore_settings(snapshot)

    assert reviewer.review_failed_chunk_count == 0
    reviewer.git_provider.publish_comment.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_successful_chunks,hosted_export,primary_is_single", [
    (True, False, False), (False, False, False), (True, True, False), (True, False, True),
])
async def test_exhausted_fallbacks_publish_partial_review_only_when_chunks_succeeded(
    chunking_enabled, has_successful_chunks, hosted_export, primary_is_single,
):
    reviewer = _make_reviewer()
    reviewer.vars = {}
    reviewer.git_provider.should_publish_review_as_thread.return_value = False
    reviewer.git_provider.supports_review_comment_identity.return_value = False
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    primary = [CHUNK_A, "review: {}", chunk_c] if has_successful_chunks else ["review: {}"] * 3
    fallback = [RuntimeError("context limit exceeded")] * (1 if has_successful_chunks else 3)
    if primary_is_single:
        primary, fallback = [FallbackEligibleError("primary request failed")], primary
    reviewer._get_prediction = AsyncMock(side_effect=primary + fallback)
    settings_values = {
        "config.model": "primary",
        "config.fallback_models": ["fallback"],
        "config.publish_output": not hosted_export,
        "config.output_run_details": primary_is_single,
        "config.is_auto_command": False,
        "config.propagate_command_errors": False,
        "pr_reviewer.persistent_comment": False,
        "pr_reviewer.publish_error_details": False,
    }
    snapshot = snapshot_settings(settings_values)
    try:
        for key, value in settings_values.items():
            get_settings().set(key, value)
        with (
            patch("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", new_callable=AsyncMock),
            patch("pr_agent.commands.pr_reviewer.fit_related_tickets_to_prompt_budget",
                  return_value=({}, reviewer.token_handler)),
            patch("pr_agent.algo.pr_processing.route_primary_model", return_value=None),
            patch("pr_agent.algo.pr_processing._get_all_deployments", return_value=[None, None]),
            patch("pr_agent.commands.pr_reviewer.get_pr_diff", side_effect=[
                ("diff", [] if primary_is_single else ["b.py"]), ("diff", ["b.py"]),
            ]),
            patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
                  return_value=PackedPRDiffs(["chunk-a", "chunk-b", "chunk-c"], [], [])),
            patch.object(reviewer, "_prepare_pr_review", side_effect=lambda: _render_review(reviewer)) as render,
        ):
            # Exercise the real fallback chain and run()'s terminal publication path.
            export_token = hosted_markdown_export.set(hosted_export)
            artifact_token = hosted_markdown_artifact.set(None)
            try:
                await reviewer.run()
                captured_export = hosted_markdown_artifact.get()
            finally:
                hosted_markdown_artifact.reset(artifact_token)
                hosted_markdown_export.reset(export_token)
    finally:
        restore_settings(snapshot)

    calls = [call.args for call in reviewer._get_prediction.await_args_list]
    if primary_is_single:
        assert calls == [("primary",), ("fallback", "chunk-a"), ("fallback", "chunk-b"), ("fallback", "chunk-c")]
    else:
        assert calls[:3] == [("primary", "chunk-a"), ("primary", "chunk-b"), ("primary", "chunk-c")]
        retried = ["chunk-b"] if has_successful_chunks else ["chunk-a", "chunk-b", "chunk-c"]
        assert calls[3:] == [("fallback", chunk) for chunk in retried]
    if hosted_export:
        reviewer.git_provider.publish_comment.assert_not_called()
        assert captured_export is None
    else:
        published = reviewer.git_provider.publish_comment.call_args.args[0]
    if not has_successful_chunks:
        render.assert_not_called()
        assert reviewer._structured_review_data is None
        assert published == "Failed to review PR"
        assert reviewer.prediction_data is None
        return

    details = get_run_details()
    assert details.models_used == (["fallback"] if primary_is_single else ["primary"])
    assert details.fallback_used is primary_is_single
    if primary_is_single:
        assert "Model: fallback (fallback)" in published
    assert reviewer._chunked_primary_model == "primary"
    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 1
    assert reviewer.remaining_files_list == []
    assert reviewer._structured_review_data["coverage"] == {
        "total_chunks": 3,
        "failed_chunks": 1,
        "files_not_reviewed": [],
        "files_partially_reviewed": [],
    }
    if not hosted_export:
        assert "1 chunk(s) failed and are not covered by this review." in published
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]

    previous_state = reconcile_review_findings(
        None, [{"path": "b.py", "body": "old finding", "line_start": 3, "line_end": 4}],
        allow_resolution=False,
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous_state, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"
    reviewer._prepare_review_finding_state(reviewer.prediction_data)
    assert reviewer._review_state_result.state["last_run"]["complete"] is False
    assert reviewer._review_state_result.state["findings"][0]["state"] != "RESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("propagate_command_errors", [True, False])
async def test_exhausted_fallbacks_propagate_partial_review_failure_when_configured(
    chunking_enabled, propagate_command_errors,
):
    reviewer = _make_reviewer()
    reviewer.vars = {}
    reviewer.git_provider.should_publish_review_as_thread.return_value = False
    reviewer.git_provider.supports_review_comment_identity.return_value = False
    chunk_c = CHUNK_A.replace("a.py", "c.py").replace("the index is never checked", "the value is never checked")
    reviewer._get_prediction = AsyncMock(side_effect=[
        CHUNK_A, "review: {}", chunk_c, TimeoutError("context limit exceeded"),
    ])
    settings_values = {
        "config.model": "primary",
        "config.fallback_models": ["fallback"],
        "config.publish_output": True,
        "config.is_auto_command": False,
        "config.propagate_command_errors": propagate_command_errors,
        "pr_reviewer.persistent_comment": False,
        "pr_reviewer.publish_error_details": False,
    }
    snapshot = snapshot_settings(settings_values)
    try:
        for key, value in settings_values.items():
            get_settings().set(key, value)
        with (
            patch("pr_agent.commands.pr_reviewer.extract_and_cache_pr_tickets", new_callable=AsyncMock),
            patch("pr_agent.commands.pr_reviewer.fit_related_tickets_to_prompt_budget",
                  return_value=({}, reviewer.token_handler)),
            patch("pr_agent.algo.pr_processing.route_primary_model", return_value=None),
            patch("pr_agent.algo.pr_processing._get_all_deployments", return_value=[None, None]),
            patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
            patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
                  return_value=PackedPRDiffs(["chunk-a", "chunk-b", "chunk-c"], [], [])),
            patch.object(reviewer, "_prepare_pr_review", side_effect=lambda: _render_review(reviewer)) as render,
        ):
            if propagate_command_errors:
                with pytest.raises(Exception, match="Failed to generate prediction with any model"):
                    await reviewer.run()
            else:
                await reviewer.run()
    finally:
        restore_settings(snapshot)

    published = reviewer.git_provider.publish_comment.call_args.args[0]
    assert "1 chunk(s) failed and are not covered by this review." in published
    assert reviewer.review_failed_chunk_count == 1
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["relevant_file"].strip() for issue in issues] == ["a.py", "c.py"]
    render.assert_called_once()


@pytest.mark.asyncio
async def test_cached_chunks_are_used_when_fallback_diff_fits(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff",
              side_effect=[("diff", ["b.py"]), ("full diff", [])]),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")

    await reviewer._prepare_chunked_prediction("model")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
        "chunk-a", "chunk-b", "chunk-b",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_fits", [True, False])
@pytest.mark.parametrize("filename", ["c.py", " c.py ", "c.py\n", r"c.py\n"])
async def test_larger_fallback_includes_omitted_files_without_repeating_successes(
    chunking_enabled, fallback_fits, filename,
):
    reviewer = _make_reviewer()
    reviewer.token_handler.prompt_tokens = 0
    reviewer.token_handler.count_tokens.side_effect = len
    get_settings().set("pr_reviewer.max_number_of_calls", 2)
    chunk_a = "## File: 'a.py'\n+first change\n"
    chunk_b = "## File: 'b.py'\n+second change\n"
    chunk_c = f"## File: {quote_diff_filename(filename)}\n+previously omitted change\n"
    reviewer._get_prediction = AsyncMock(side_effect=[CHUNK_A, RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", side_effect=[
            (chunk_a, ["b.py", filename]),
            (chunk_a + chunk_b + chunk_c, []),
        ]) as get_diff,
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs([chunk_a, chunk_b], [filename], [])) as get_multi,
        patch("pr_agent.algo.token_budget.get_max_tokens",
              return_value=10000 if fallback_fits else 1500 + len(chunk_b)),
    ):
        with pytest.raises(RuntimeError, match="model refused"):
            await reviewer._prepare_prediction("primary")
        await reviewer._prepare_prediction("fallback")

    assert get_diff.call_count == 2
    get_multi.assert_called_once()
    calls = reviewer._get_prediction.await_args_list
    assert len(calls) == 3
    assert calls[0].args == ("primary", chunk_a)
    assert calls[1].args == ("primary", chunk_b)
    fallback_diff = calls[2].args[1]
    assert calls[2].args[0] == "fallback"
    assert chunk_a not in fallback_diff
    assert chunk_b in fallback_diff
    assert (chunk_c in fallback_diff) is fallback_fits
    assert reviewer.remaining_files_list == ([] if fallback_fits else [filename])
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0


@pytest.mark.parametrize("remaining_index", [0, 1])
def test_fallback_distinguishes_literal_backslash_from_newline(remaining_index, chunking_enabled):
    reviewer = _make_reviewer()
    paths = ["a\n.py", r"a\n.py"]
    sections = [f"## File: {quote_diff_filename(path)}\n+change\n" for path in paths]
    selected = paths[remaining_index]
    completed = "## File: 'done.py'\n+reviewed\n"
    reviewer._chunked_patches_diff_list = [completed]
    reviewer._chunked_results = {0: CHUNK_A}
    reviewer._chunked_remaining_files_list = [selected]
    reviewer.patches_diff = "".join(sections)

    reviewer._include_newly_reviewable_files("primary")

    assert reviewer._chunked_patches_diff_list == [completed, sections[remaining_index]]
    assert reviewer._chunked_remaining_files_list == []
    assert reviewer._chunked_results == {0: CHUNK_A}


@pytest.mark.parametrize("filename", ["a.py", "a\n.py"])
def test_fallback_ignores_file_headers_inside_ai_metadata(filename, chunking_enabled):
    reviewer = _make_reviewer()
    header = f"## File: {quote_diff_filename(filename)}"
    actual = header + "\n+actual change\n"
    completed = "## File: 'done.py'\n+reviewed\n"
    file = FilePatchInfo("", "", "", "done.py")
    alternate_headers = [header, header.replace("## File:", "## file:"), header.replace(": ", ":")]
    file.ai_file_summary = {"long_summary": "Summary\n" + "\n".join(alternate_headers) + "\nNot the actual diff"}
    rendered = add_ai_summary_top_patch(file, completed)

    reviewer._chunked_patches_diff_list = [rendered]
    reviewer._chunked_results = {0: CHUNK_A}
    reviewer._chunked_remaining_files_list = [filename]
    reviewer.patches_diff = rendered + actual

    reviewer._include_newly_reviewable_files("primary")

    assert all(f"> {label}" in rendered.split("\n") for label in alternate_headers)
    assert reviewer._chunked_patches_diff_list == [rendered, actual]
    assert reviewer._chunked_remaining_files_list == []
    assert reviewer._chunked_results == {0: CHUNK_A}


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True])
async def test_a_smaller_fallback_splits_an_oversized_pending_chunk(chunking_enabled, filtered):
    reviewer = _make_reviewer()
    reviewer.git_provider.get_filtered_diff_file_names.return_value = (["pnpm-lock.yaml"] if filtered else [])
    a_part = "## File: 'a.py'\n+change a\n"
    b_part = "## File: 'blong.py'\n+long change b\n"
    combined_chunk = a_part + b_part
    chunk_c = "## File: 'c.py'\n+change c\n"
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"), CHUNK_B])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff",
              side_effect=[(combined_chunk, ["c.py", "blong.py"]), (combined_chunk, [])]),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs([combined_chunk, chunk_c], [], [])),
        patch("pr_agent.algo.token_budget.get_max_tokens",
              side_effect=lambda model, **_kwargs: 10000 if model == "primary" else 1540),
    ):
        with pytest.raises(RuntimeError, match="model refused"):
            await reviewer._prepare_prediction("primary")
        reviewer._get_prediction.side_effect = [CHUNK_A, CHUNK_A]
        await reviewer._prepare_prediction("fallback")

    assert reviewer.prediction_data["review"]["score"] == 40
    assert reviewer.review_chunk_count == 3
    assert reviewer.review_failed_chunk_count == 0
    calls = [call.args for call in reviewer._get_prediction.await_args_list]
    assert [model for model, _ in calls] == ["primary", "primary", "fallback", "fallback"]
    assert all(("pnpm-lock.yaml" in chunk) is filtered for _, chunk in calls)
    assert combined_chunk in calls[0][1]
    assert chunk_c in calls[1][1]
    assert a_part in calls[2][1]
    assert b_part in calls[3][1]


@pytest.mark.asyncio
async def test_a_review_where_every_chunk_failed_raises_so_a_fallback_model_is_tried(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("model refused"),
                                                      RuntimeError("model refused again")])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        pytest.raises(RuntimeError, match="model refused"),
    ):
        await reviewer._prepare_prediction("model")


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_predictions", [
    ["not yaml at all", "nor is this"],
    ["review: {}", "review: {}"],
])
async def test_chunks_without_nonempty_reviews_fail_the_model_attempt(chunking_enabled,
                                                                      chunk_predictions):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=chunk_predictions)

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        pytest.raises(ValueError, match="non-empty review"),
    ):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 2
    assert reviewer.prediction_data is None


@pytest.mark.asyncio
async def test_invalid_chunk_emits_one_schema_warning_before_rendering(chunking_enabled):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[
        "review:\n  score: 101\n  key_issues_to_review: []",
        CHUNK_B,
    ])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
        patch("pr_agent.commands.pr_reviewer.get_logger") as get_logger,
    ):
        await reviewer._prepare_prediction("model")
        reviewer._prepare_pr_review()

    warnings = get_logger.return_value.warning.call_args_list
    schema_warnings = [call for call in warnings if call.args == ("Review output failed schema validation",)]
    assert len(schema_warnings) == 1
    assert schema_warnings[0].kwargs["artifact"] == {"field": "review.score", "value": 101}
    # schema validation stays warn-only (#3372): the chunk is merged, not retried
    assert reviewer._get_prediction.await_count == 2
    assert reviewer.review_chunk_count == 2
    assert reviewer.review_failed_chunk_count == 0
    assert reviewer.prediction_data["review"]["score"] == 40


def _render_review(reviewer):
    reviewer.prediction = "review:\n  summary: test"
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = False
    reviewer.set_review_labels = MagicMock()

    with (
        patch("pr_agent.commands.pr_reviewer.load_yaml", return_value={"review": {"summary": "test"}}),
        patch("pr_agent.commands.pr_reviewer.github_action_output"),
        patch("pr_agent.commands.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return PRReviewer._prepare_pr_review(reviewer)


def test_a_chunked_review_says_how_many_chunks_it_was_built_from():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 3

    review = _render_review(reviewer)

    assert review.startswith("original review")
    assert "ℹ️ **Chunked review:**" in review
    assert "reviewed in 3 chunks" in review
    assert "failed" not in review


def test_a_chunked_review_reports_the_chunks_that_failed():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 3
    reviewer.review_failed_chunk_count = 1

    review = _render_review(reviewer)

    assert "1 chunk(s) failed and are not covered by this review." in review


def test_a_single_call_review_says_nothing_about_chunks():
    review = _render_review(_make_reviewer())

    assert review == "original review"


def test_the_chunk_note_comes_before_the_review_coverage_footer():
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 2
    reviewer.remaining_files_list = ["left_out.py"]
    snapshot = snapshot_settings(("pr_reviewer.enable_review_coverage_footer",))
    try:
        get_settings().set("pr_reviewer.enable_review_coverage_footer", True)
        review = _render_review(reviewer)
    finally:
        restore_settings(snapshot)

    assert review.index("Chunked review:") < review.index("⚠️ **Review coverage:**")
    assert "- `left_out.py`" in review


def _pack_partial_review(token_handler):
    patch_text = "## File: 'a.py'\n+" + "visible change\n" * 8 + "+UNSEEN_TAIL_SENTINEL\n"
    file_dict = {
        "a.py": {"patch": patch_text, "tokens": len(patch_text)},
        "b.py": {"patch": "## File: 'b.py'\n+other change\n", "tokens": 28},
    }
    with (
        patch("pr_agent.algo.pr_processing.clip_tokens", side_effect=lambda text, *a, **k: text[:55]),
        patch.dict(get_settings().config, {"large_patch_policy": "clip"}),
    ):
        coverage = _pack_pr_multi_diffs(file_dict, token_handler, 3, False, 60, return_coverage=True)
    assert len(coverage.chunks) == 2
    assert "UNSEEN_TAIL_SENTINEL" not in "".join(coverage.chunks)
    assert coverage.remaining_files_list == []
    assert coverage.partial_files_list == ["a.py"]
    return coverage


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_outcome", ["success", "retry", "failed"])
async def test_clipped_prompt_keeps_prior_findings_active(chunking_enabled, chunk_outcome):
    reviewer = _make_reviewer()
    coverage = _pack_partial_review(reviewer.token_handler)
    predictions = {
        "success": [CHUNK_B, CHUNK_B],
        "retry": [CHUNK_B, RuntimeError("model unavailable"), CHUNK_B],
        "failed": [RuntimeError("model unavailable"), CHUNK_B],
    }
    reviewer._get_prediction = AsyncMock(side_effect=predictions[chunk_outcome])
    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("single diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs", return_value=coverage) as pack,
    ):
        if chunk_outcome != "success":
            with pytest.raises(RuntimeError, match="model unavailable"):
                await reviewer._prepare_prediction("primary")
            if chunk_outcome == "retry":
                await reviewer._prepare_prediction("fallback")
            else:
                assert reviewer._merge_cached_review_chunks()
        else:
            await reviewer._prepare_prediction("primary")
    pack.assert_called_once()
    assert all("UNSEEN_TAIL_SENTINEL" not in call.args[1] for call in reviewer._get_prediction.await_args_list)
    assert reviewer.remaining_files_list == []
    assert reviewer.partial_files_list == ["a.py"]
    assert reviewer.review_failed_chunk_count == (1 if chunk_outcome == "failed" else 0)
    if chunk_outcome == "retry":
        assert [call.args[1] for call in reviewer._get_prediction.await_args_list] == [
            *coverage.chunks, coverage.chunks[1],
        ]

    previous = reconcile_review_findings(
        None, [{"path": "a.py", "body": "unseen old finding", "line_start": 100, "line_end": 101}],
        allow_resolution=False, head_sha="head-1",
    ).state
    reviewer._review_finding_state_enabled = lambda: True
    reviewer._load_review_finding_state = lambda: ParsedReviewState(previous, present=True, valid=True)
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"
    reviewer._prepare_review_finding_state(reviewer.prediction_data)
    state = reviewer._review_state_result.state
    assert state["findings"][0]["state"] == "ACTIVE"
    assert state["last_run"]["complete"] is False
    assert state["last_run"]["kind"] == "partial"
    assert state["last_run"]["excluded_files"] == ["a.py"]
    with patch.dict(get_settings().pr_reviewer, {"enable_review_coverage_footer": True}):
        rendered = _render_review(reviewer)
    assert "had patches clipped before analysis" in rendered
    assert "partially analyzed" not in rendered
    if chunk_outcome == "failed":
        assert "1 chunk(s) failed and are not covered" in rendered
    assert "- `a.py`" in rendered
    assert "not included in this review" not in rendered


@pytest.mark.parametrize("invalid_marker", [False, True])
def test_review_state_records_union_of_incomplete_files(invalid_marker):
    reviewer = _make_reviewer()
    reviewer.remaining_files_list = ["omitted.py", "same.py"]
    reviewer.partial_files_list = ["same.py", "clipped.py"]
    reviewer.prediction = CHUNK_B
    reviewer._review_finding_state_enabled = lambda: True

    def load_state():
        if invalid_marker:
            reviewer._review_state_blocked = True
            reviewer._review_state_block_reason = "invalid_marker"
        return ParsedReviewState(None, present=invalid_marker, valid=not invalid_marker)

    reviewer._load_review_finding_state = load_state
    reviewer._review_head_sha = lambda: "head-2"
    reviewer._review_run_id = lambda: "run-2"
    reviewer._prepare_review_finding_state(PRReviewer._load_review_yaml(CHUNK_A))
    assert reviewer._review_state_result.state["last_run"]["excluded_files"] == [
        "clipped.py", "omitted.py", "same.py",
    ]
    assert reviewer._review_state_result.state["last_run"]["complete"] is False


@pytest.mark.parametrize("filename, safe_span", [
    ("app`[@org/team](https://example.invalid).py", "``app`[@org/team](https://example.invalid).py``"),
    ("line\n@org/team.py", r"`line\n@org/team.py`"),
    ("carriage\r# heading.py", r"`carriage\r# heading.py`"),
    ("a``b.py", "```a``b.py```"),
    ("`edge`", "`` `edge` ``"),
    (" edge ", "`  edge  `"),
])
@pytest.mark.parametrize("category", ["remaining_files_list", "partial_files_list"])
def test_review_coverage_renders_filenames_as_safe_code_spans(filename, safe_span, category):
    reviewer = _make_reviewer()
    setattr(reviewer, category, [filename])
    with patch.dict(get_settings().pr_reviewer, {"enable_review_coverage_footer": True}):
        rendered = _render_review(reviewer)
    assert rendered.splitlines()[-1] == "- " + safe_span
    assert "\r" not in rendered
    assert "\n@org/team" not in rendered
    assert "\n# heading" not in rendered


@pytest.mark.parametrize("enabled", [False, True])
def test_review_coverage_caps_each_category_and_preserves_failed_chunk_note(enabled):
    reviewer = _make_reviewer()
    reviewer.review_chunk_count = 3
    reviewer.review_failed_chunk_count = 1
    reviewer.remaining_files_list = [f"omitted-{i}.py" for i in range(52)]
    reviewer.partial_files_list = [f"clipped-{i}.py" for i in range(53)]
    with patch.dict(get_settings().pr_reviewer, {"enable_review_coverage_footer": enabled}):
        rendered = _render_review(reviewer)
    assert "1 chunk(s) failed" in rendered
    if not enabled:
        assert "Review coverage" not in rendered
        assert "omitted-" not in rendered and "clipped-" not in rendered
        return
    assert (
        rendered.index("Chunked review")
        < rendered.index("no source changes analyzed")
        < rendered.index("had patches clipped before analysis")
    )
    for prefix in ("omitted", "clipped"):
        assert f"- `{prefix}-49.py`" in rendered
        assert f"{prefix}-50.py" not in rendered
    assert "... and 2 more" in rendered
    assert "... and 3 more" in rendered


def _chunk_with_confidences(*confidences):
    issues = "".join(
        f"    - relevant_file: |\n        a.py\n      issue_header: |\n        Possible Issue\n"
        f"      issue_content: |\n        problem {confidence}\n      start_line: 3\n      end_line: 4\n"
        f"      confidence: {confidence}\n"
        for confidence in confidences
    )
    return f"review:\n  score: 50\n  key_issues_to_review:\n{issues}  security_concerns: |\n    No\n"


@pytest.mark.asyncio
async def test_confidence_filter_runs_before_the_findings_cap_across_chunks(chunking_enabled, monkeypatch):
    # three 3s in the first chunk would fill num_max_findings and push out the 9 of the second
    get_settings().set("pr_reviewer.num_max_findings", 3)
    monkeypatch.setitem(get_settings().pr_reviewer, "min_finding_confidence", 7)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(
        side_effect=[_chunk_with_confidences(3, 3, 3), _chunk_with_confidences(9)])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
    ):
        await reviewer._prepare_prediction("model")

    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [issue["confidence"] for issue in issues] == [9]
    assert reviewer.low_confidence_filtered_count == 3


@pytest.mark.asyncio
async def test_remerging_cached_chunks_keeps_the_low_confidence_count(chunking_enabled, monkeypatch):
    # a fallback attempt reuses the cached chunks, so the count must not depend on the first merge
    monkeypatch.setitem(get_settings().pr_reviewer, "min_finding_confidence", 7)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(
        side_effect=[_chunk_with_confidences(3, 3), _chunk_with_confidences(9)])

    with (
        patch("pr_agent.commands.pr_reviewer.get_pr_diff", return_value=("diff", ["b.py"])),
        patch("pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
              return_value=PackedPRDiffs(["chunk-a", "chunk-b"], [], [])),
    ):
        await reviewer._prepare_prediction("model")
    reviewer.low_confidence_filtered_count = 0  # what the next attempt's reset does

    assert reviewer._merge_cached_review_chunks()

    assert reviewer.low_confidence_filtered_count == 2
    assert [issue["confidence"] for issue in reviewer.prediction_data["review"]["key_issues_to_review"]] == [9]


@pytest.mark.asyncio
async def test_chunked_review_drops_prepared_diff_and_renders_filtered_prompts_lazily(
    chunking_enabled, monkeypatch,
):
    import gc
    import weakref

    reviewer = _make_reviewer()
    reviewer.git_provider.get_filtered_diff_file_names.return_value = ["yarn.lock"]
    prepared_ref = None
    observed_prompts = []

    async def fake_get_prediction(_model, prompt):
        gc.collect()
        assert prepared_ref is not None and prepared_ref() is None
        observed_prompts.append(prompt)
        return CHUNK_A if len(observed_prompts) == 1 else CHUNK_B

    reviewer._get_prediction = AsyncMock(side_effect=fake_get_prediction)

    async def fake_prepare_diff(*_args, **_kwargs):
        nonlocal prepared_ref
        prepared = PreparedPRDiff(
            "chunk-a",
            ["b.py"],
            file_dict={"a.py": {"patch": "chunk-a", "tokens": 7}, "b.py": {"patch": "chunk-b", "tokens": 7}},
            model="model",
            add_line_numbers_to_hunks=True,
            token_handler=reviewer.token_handler,
        )
        prepared_ref = weakref.ref(prepared)
        return prepared

    # Avoid MagicMock here: MagicMock stores call_args, which would keep prepared_diff alive.
    monkeypatch.setattr("pr_agent.commands.pr_reviewer.run_diff_preparation", fake_prepare_diff)
    monkeypatch.setattr(
        "pr_agent.commands.pr_reviewer.get_pr_multi_diffs",
        lambda *_args, **_kwargs: PackedPRDiffs(["chunk-a", "chunk-b"], [], []),
    )

    await reviewer._prepare_prediction("model")

    assert len(observed_prompts) == 2
    assert all("yarn.lock" in prompt for prompt in observed_prompts)
