from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.run_details import command_failed, init_run_details, record_command_failure
from pr_agent.algo.types import FilePatchInfo
from pr_agent.commands import pr_description as pr_description_module
from pr_agent.commands import pr_reviewer as pr_reviewer_module
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import IncrementalPR


class FakeGuard:
    instances = []

    def __init__(self, tool, git_provider, skip=False):
        self.tool = tool
        self.skip = skip
        self.recorded = False
        self.invalidated = False
        self.replaced_output = None
        FakeGuard.instances.append(self)

    def should_skip(self):
        return self.skip

    def record(self, *, replaced_output=True):
        self.recorded = True
        self.replaced_output = replaced_output

    def invalidate(self):
        self.invalidated = True


@pytest.fixture
def guards(monkeypatch):
    FakeGuard.instances = []
    monkeypatch.setitem(get_settings().pr_reviewer, "per_commit_review", False)
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", True)
    return FakeGuard.instances


def _guard_factory(skip):
    return lambda tool, provider: FakeGuard(tool, provider, skip=skip)


def _reviewer(incremental=False):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = IncrementalPR(incremental)
    return reviewer


def _review(reviewer, done=True, fail=False):
    async def run_review():
        init_run_details()
        reviewer._review_done = done
        if fail:
            record_command_failure()
    return run_review


@pytest.mark.parametrize("skip", [True, False])
async def test_review_runs_only_when_not_skipped(guards, skip):
    reviewer = _reviewer()
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(skip)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer)) as run_review:
        await reviewer.run()

    assert run_review.called is not skip
    assert guards[0].tool == "review"


@pytest.mark.parametrize("done, fail, recorded", [(True, False, True), (False, False, False), (True, True, False)])
async def test_review_is_recorded_only_when_it_succeeded(guards, done, fail, recorded):
    reviewer = _reviewer()
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(False)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer, done, fail)):
        await reviewer.run()

    assert guards[0].recorded is recorded


async def test_incremental_review_is_never_skipped_or_recorded(guards):
    reviewer = _reviewer(incremental=True)
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(True)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer)) as run_review:
        await reviewer.run()

    assert run_review.called
    assert guards == []


def _describer():
    describer = PRDescription.__new__(PRDescription)
    describer.git_provider = MagicMock()
    return describer


@pytest.mark.parametrize("skip", [True, False])
async def test_describe_runs_only_when_not_skipped(guards, skip):
    describer = _describer()
    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(skip)), \
            patch.object(describer, "_run_describe", return_value="") as run_describe:
        await describer.run()

    assert run_describe.called is not skip
    assert guards[0].tool == "describe"


@pytest.mark.parametrize("result, fail, recorded", [("", False, True), (None, False, False), ("", True, False)])
async def test_describe_is_recorded_only_when_it_succeeded(guards, result, fail, recorded):
    describer = _describer()

    async def run_describe():
        init_run_details()
        if fail:
            record_command_failure()
        return result

    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(False)), \
            patch.object(describer, "_run_describe", side_effect=run_describe):
        assert await describer.run() == result

    assert guards[0].recorded is recorded


async def test_review_builds_no_guard_when_the_setting_is_off(guards, monkeypatch):
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", False)
    reviewer = _reviewer()
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(True)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer)) as run_review:
        await reviewer.run()

    assert run_review.called
    assert guards == []


async def test_describe_builds_no_guard_when_the_setting_is_off(guards, monkeypatch):
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", False)
    describer = _describer()
    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(True)), \
            patch.object(describer, "_run_describe", return_value="") as run_describe:
        await describer.run()

    assert run_describe.called
    assert guards == []


@pytest.mark.parametrize(
    "mode, expected_recorded, expected_invalidated",
    [
        ("clean", True, False),
        ("state_size_cap", True, False),
        ("content_fetch_failed", False, True),
        ("failed_chunk", False, True),
        ("partial_fallback", False, True),
        ("propagated_partial_fallback", False, True),
        ("unpublished_partial_fallback", False, False),
        ("label_publish_failed", False, True),
        ("labels_updated_then_publish_failed", False, True),
        ("persistent_publish_failed", False, True),
        ("comment_publish_exception", False, True),
        ("non_authoritative", False, True),
        ("unverified_identity", False, True),
    ],
)
async def test_degraded_review_runs_are_not_recorded(
    guards, monkeypatch, mode, expected_recorded, expected_invalidated
):
    settings = get_settings()
    monkeypatch.setitem(settings.config, "publish_output", True)
    monkeypatch.setitem(
        settings.pr_reviewer,
        "publish_output_no_suggestions",
        mode not in {"unpublished_partial_fallback", "label_publish_failed"},
    )
    monkeypatch.setitem(settings.config, "is_auto_command", True)
    monkeypatch.setitem(
        settings.config,
        "propagate_command_errors",
        mode == "propagated_partial_fallback",
    )
    monkeypatch.setitem(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setitem(
        settings.pr_reviewer,
        "persistent_finding_state",
        mode in {"persistent_publish_failed", "unverified_identity"},
    )
    monkeypatch.setitem(settings.pr_reviewer, "require_estimate_effort_to_review", True)
    monkeypatch.setitem(
        settings.pr_reviewer,
        "enable_review_labels_effort",
        mode in {"label_publish_failed", "labels_updated_then_publish_failed"},
    )
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())

    reviewer = _reviewer()
    reviewer._review_commit = None
    reviewer.vars = {}
    reviewer.remaining_files_list = []
    reviewer.git_provider.get_files.return_value = ["x.py"]
    reviewer.git_provider.get_diff_files.return_value = [
        FilePatchInfo(
            "" if mode == "content_fetch_failed" else "a",
            "" if mode == "content_fetch_failed" else "b",
            "" if mode == "content_fetch_failed" else "@@ -1 +1 @@\n-a\n+b\n",
            "x.py",
            content_fetch_failed=mode == "content_fetch_failed",
        )
    ]
    reviewer.git_provider.should_publish_review_as_thread.return_value = False
    if mode in {"label_publish_failed", "labels_updated_then_publish_failed"}:
        reviewer.git_provider.is_supported.side_effect = lambda cap: cap == "get_labels"
        reviewer.git_provider.get_pr_labels.return_value = []
        reviewer.git_provider.publish_labels.return_value = mode != "label_publish_failed"
    if mode in {"labels_updated_then_publish_failed", "comment_publish_exception"}:
        reviewer.git_provider.publish_persistent_comment.side_effect = RuntimeError("comment publish failed")
    if mode == "persistent_publish_failed":
        reviewer.git_provider.publish_persistent_comment_full.return_value = None

    def prepare_review():
        if mode in {"label_publish_failed", "labels_updated_then_publish_failed"}:
            reviewer.set_review_labels({"review": {"estimated_effort_to_review_[1-5]": 2}})
        return "## PR Reviewer Guide 🔍\n\nNo major issues detected."

    reviewer._prepare_pr_review = prepare_review
    if mode in {"persistent_publish_failed", "unverified_identity"}:
        reviewer._review_finding_state_in_play = lambda: True
        reviewer._review_comment_authorship_available = lambda: mode == "persistent_publish_failed"
        reviewer._persistent_review_comment_exists = lambda: True

    async def fake_retry(fn, **_kwargs):
        reviewer.prediction = "review: {}"
        reviewer.review_failed_chunk_count = 1 if mode == "failed_chunk" else 0
        reviewer._review_state_blocked = mode in {"non_authoritative", "state_size_cap"}
        reviewer._review_state_block_reason = (
            "state_size"
            if mode == "state_size_cap"
            else "read_error"
            if mode == "non_authoritative"
            else None
        )
        if mode in {"partial_fallback", "propagated_partial_fallback", "unpublished_partial_fallback"}:
            raise RuntimeError("fallback models exhausted")

    def fake_merge_cached():
        reviewer.prediction = "review: {}"
        reviewer.review_failed_chunk_count = 0
        return True

    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)
    monkeypatch.setattr(reviewer, "_merge_cached_review_chunks", fake_merge_cached)

    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(False)):
        if mode == "propagated_partial_fallback":
            with pytest.raises(RuntimeError, match="fallback models exhausted"):
                await reviewer.run()
        else:
            await reviewer.run()

    assert command_failed() is (
        mode in {"labels_updated_then_publish_failed", "persistent_publish_failed", "comment_publish_exception"}
    )
    assert guards[0].recorded is expected_recorded
    assert guards[0].invalidated is expected_invalidated


@pytest.mark.parametrize(
    "mode, expected_recorded, expected_invalidated",
    [
        ("clean", True, False),
        ("content_fetch_failed", False, True),
        ("failed_chunk", False, True),
        ("label_read_failed", False, True),
        ("label_publish_failed", False, True),
        ("label_publish_exception", False, True),
        ("labels_updated_then_publish_failed", False, True),
        ("description_publish_failed", False, True),
        ("headers_only_fallback", False, True),
        ("header_only_extended", False, True),
        ("empty_pr_files_extended", False, True),
        ("nonempty_pr_files_extended", True, False),
        ("extend_uncovered_fallback", False, True),
        ("malformed_pr_files", False, True),
        ("dropped_file_entry", False, True),
        ("markers_missing_walkthrough", False, True),
        ("markers_summary_only_extend_fallback", True, False),
        ("markers_summary_only_unselected_sink", True, False),
        ("markers_summary_only_structured_output", False, True),
    ],
)
async def test_degraded_describe_runs_are_not_recorded(
    guards, monkeypatch, mode, expected_recorded, expected_invalidated
):
    settings = get_settings()
    monkeypatch.setitem(settings.config, "publish_output", True)
    monkeypatch.setitem(settings.config, "is_auto_command", True)
    if mode == "markers_summary_only_structured_output":
        from pr_agent.algo import output_sinks as output_sinks_module

        monkeypatch.setitem(output_sinks_module.OUTPUT_SINK_TYPES, "custom", MagicMock)
        monkeypatch.setitem(settings.push_outputs, "enable", True)
        monkeypatch.setitem(settings.push_outputs, "channels", ["webhook", "custom"])
        monkeypatch.setitem(settings.push_outputs, "webhook_url", "https://[")
    elif mode == "markers_summary_only_unselected_sink":
        monkeypatch.setitem(settings.push_outputs, "enable", True)
        monkeypatch.setitem(settings.push_outputs, "commands", ["review"])
        monkeypatch.setitem(settings.push_outputs, "channels", ["stdout"])
    monkeypatch.setitem(
        settings.config,
        "propagate_command_errors",
        mode
        in {
            "label_publish_exception",
            "labels_updated_then_publish_failed",
            "description_publish_failed",
        },
    )
    monkeypatch.setitem(
        settings.pr_description,
        "publish_labels",
        mode
        in {
            "label_read_failed",
            "label_publish_failed",
            "label_publish_exception",
            "labels_updated_then_publish_failed",
        },
    )
    monkeypatch.setitem(
        settings.pr_description,
        "enable_semantic_files_types",
        mode
        in {
            "headers_only_fallback",
            "header_only_extended",
            "empty_pr_files_extended",
            "nonempty_pr_files_extended",
            "extend_uncovered_fallback",
            "malformed_pr_files",
            "dropped_file_entry",
            "markers_missing_walkthrough",
            "markers_summary_only_extend_fallback",
            "markers_summary_only_unselected_sink",
            "markers_summary_only_structured_output",
        },
    )
    monkeypatch.setitem(settings.pr_description, "enable_large_pr_handling", True)
    monkeypatch.setitem(settings.pr_description, "async_ai_calls", False)
    monkeypatch.setitem(
        settings.pr_description,
        "use_description_markers",
        mode
        in {
            "markers_missing_walkthrough",
            "markers_summary_only_extend_fallback",
            "markers_summary_only_unselected_sink",
            "markers_summary_only_structured_output",
        },
    )
    monkeypatch.setitem(settings.pr_description, "publish_description_as_comment", False)
    monkeypatch.setitem(settings.pr_description, "generate_ai_title", False)
    monkeypatch.setattr(pr_description_module, "extract_and_cache_pr_tickets", AsyncMock())

    describer = _describer()
    describer.pr_id = "1"
    describer.vars = {"title": "Existing PR title"}
    describer.keys_fix = ["filename:", "language:", "changes_summary:", "changes_title:", "description:"]
    describer.file_label_dict = {}
    if mode in {
        "markers_summary_only_extend_fallback",
        "markers_summary_only_unselected_sink",
        "markers_summary_only_structured_output",
    }:
        describer.user_description = "pr_agent:summary"
    elif mode == "markers_missing_walkthrough":
        describer.user_description = "pr_agent:summary\npr_agent:walkthrough"
    else:
        describer.user_description = ""
    describer.token_handler = MagicMock(prompt_tokens=10)
    describer.git_provider.get_line_link.return_value = "https://example/x.py"
    diff_files = [
        FilePatchInfo(
            "" if mode == "content_fetch_failed" else "a",
            "" if mode == "content_fetch_failed" else "b",
            "" if mode == "content_fetch_failed" else "@@ -1 +1 @@\n-a\n+b\n",
            "x.py",
            content_fetch_failed=mode == "content_fetch_failed",
        )
    ]
    if mode in {
        "extend_uncovered_fallback",
        "nonempty_pr_files_extended",
        "malformed_pr_files",
        "dropped_file_entry",
        "markers_summary_only_extend_fallback",
        "markers_summary_only_unselected_sink",
        "markers_summary_only_structured_output",
    }:
        diff_files.append(FilePatchInfo("a", "b", "@@ -1 +1 @@\n-a\n+b\n", "y.py"))
    describer.git_provider.get_diff_files.return_value = diff_files
    describer.git_provider.is_supported.side_effect = (
        lambda cap: cap == "gfm_markdown"
        or (
            cap == "get_labels"
            and mode
            in {
                "label_read_failed",
                "label_publish_failed",
                "label_publish_exception",
                "labels_updated_then_publish_failed",
            }
        )
    )
    if mode == "label_read_failed":
        describer.git_provider.get_pr_labels.return_value = None
    elif mode == "label_publish_failed":
        describer.git_provider.get_pr_labels.return_value = []
        describer.git_provider.publish_labels.return_value = False
    elif mode == "label_publish_exception":
        describer.git_provider.get_pr_labels.return_value = []
        describer.git_provider.publish_labels.side_effect = RuntimeError("partial label publish failed")
    elif mode == "labels_updated_then_publish_failed":
        describer.git_provider.get_pr_labels.return_value = []
        describer.git_provider.publish_labels.return_value = True
        describer.git_provider.publish_description.side_effect = RuntimeError("description publish failed")
    elif mode == "description_publish_failed":
        describer.git_provider.publish_description.side_effect = RuntimeError("description publish failed")

    header_yaml = "title: Test\ntype:\n  - Bug fix\ndescription: Summary\n"

    async def fake_retry(fn, *_args, **_kwargs):
        if mode in {
            "header_only_extended",
            "empty_pr_files_extended",
            "nonempty_pr_files_extended",
            "malformed_pr_files",
            "dropped_file_entry",
        }:
            if mode == "header_only_extended":
                pr_files_block = ""
            elif mode == "empty_pr_files_extended":
                pr_files_block = "pr_files: []\n"
            elif mode == "nonempty_pr_files_extended":
                pr_files_block = (
                    "pr_files:\n"
                    "  - filename: x.py\n"
                    "    changes_summary: Summary\n"
                    "    changes_title: Title\n"
                    "    label: bug\n"
                )
            elif mode == "malformed_pr_files":
                pr_files_block = "pr_files: broken\n"
            else:
                pr_files_block = (
                    "pr_files:\n"
                    "  - filename: x.py\n"
                    "    changes_summary: Summary\n"
                    "    changes_title: Title\n"
                    "    label: bug\n"
                    "  - filename: y.py\n"
                    "    changes_title: Truncated entry missing summary\n"
                    "    label: bug\n"
                )
            describer._get_prediction = AsyncMock(return_value=header_yaml + pr_files_block)
            with (
                patch("pr_agent.commands.pr_description.get_pr_diff", return_value="@@ -1 +1 @@\n-a\n+b\n"),
                patch(
                    "pr_agent.commands.pr_description.fit_related_tickets_to_prompt_budget",
                    side_effect=lambda _pr, raw_vars, *_a, **_kw: (
                        raw_vars,
                        describer.token_handler,
                    ),
                ),
                patch("pr_agent.commands.pr_description.set_custom_labels"),
            ):
                await fn("gpt-4o")
            return
        if mode in {
            "headers_only_fallback",
            "extend_uncovered_fallback",
            "markers_summary_only_extend_fallback",
            "markers_summary_only_unselected_sink",
            "markers_summary_only_structured_output",
        }:
            file_yaml = (
                "pr_files:\n"
                "  - filename: x.py\n"
                "    changes_summary: Summary\n"
                "    changes_title: Title\n"
                "    label: bug\n"
            )
            describer._get_prediction = AsyncMock(
                side_effect=lambda *_a, **_kw: (
                    header_yaml
                    if _kw.get("prompt") == "pr_description_only_description_prompts"
                    else file_yaml
                )
            )
            if mode == "headers_only_fallback":
                describer.extend_uncovered_files = AsyncMock(return_value="!!omap\n  - x: y")
            else:
                monkeypatch.setattr(
                    pr_description_module.yaml,
                    "dump",
                    MagicMock(side_effect=RuntimeError("yaml dump failed")),
                )
            with (
                patch("pr_agent.commands.pr_description.get_pr_diff", return_value=""),
                patch(
                    "pr_agent.commands.pr_description.get_pr_diff_multiple_patchs",
                    return_value=(["@@ -1 +1 @@\n-a\n+b\n"], [10], [], [], {}, [["x.py"]]),
                ),
                patch(
                    "pr_agent.commands.pr_description.fit_related_tickets_to_prompt_budget",
                    side_effect=lambda _pr, raw_vars, *_a, **_kw: (
                        raw_vars,
                        describer.token_handler,
                    ),
                ),
                patch("pr_agent.commands.pr_description.set_custom_labels"),
            ):
                await fn("gpt-4o")
            return
        describer._description_degraded = False
        describer.prediction = header_yaml
        describer.description_total_chunk_count = 2
        describer.description_failed_chunk_count = 1 if mode == "failed_chunk" else 0
        describer.description_failed_files = ["y.py"] if mode == "failed_chunk" else []

    monkeypatch.setattr(pr_description_module, "retry_with_fallback_models", fake_retry)

    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(False)):
        if mode in {
            "label_publish_exception",
            "labels_updated_then_publish_failed",
            "description_publish_failed",
        }:
            with pytest.raises(RuntimeError, match="publish failed"):
                await describer.run()
        else:
            assert await describer.run() == ""

    assert command_failed() is (
        mode
        in {
            "label_publish_exception",
            "labels_updated_then_publish_failed",
            "description_publish_failed",
        }
    )
    assert describer.git_provider.publish_description.called is (mode != "label_publish_exception")
    assert guards[0].recorded is expected_recorded
    assert guards[0].invalidated is expected_invalidated
