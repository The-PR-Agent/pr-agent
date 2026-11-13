"""Unit tests for gitlab.auto_resolve_fixed_inline_threads."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gitlab import Gitlab, GitlabError

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    _eligible_own_inline_thread,
    _flagged_line_removed,
    _removed_lines_from_patch,
)

from ._settings_helpers import restore_settings, snapshot_settings

BOT_ID = 71
OLD_SHA = "a" * 40
HEAD_SHA = "b" * 40
START_SHA = "s" * 40
AGENT_BODY = "**Suggestion:** drop the key <!-- pr-agent-dedup: aaaa1111bbbb -->"

PATCH = """\
@@ -1,3 +1,4 @@
 ctx
-old
+new one
+new two
 ctx
@@ -10,2 +10,3 @@
 ctx
+added
 ctx
"""

INSERT_ONLY_PATCH = """\
@@ -1,2 +1,3 @@
 ctx
+inserted
 ctx
"""

NO_NEWLINE_PATCH = """\
@@ -1,3 +1,3 @@
 ctx
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

DOUBLE_DASH_PATCH = """\
@@ -1,3 +1,3 @@
 ctx
---flag
 ctx
"""


def _discussion(body=AGENT_BODY, path="app.py", line=4, head_sha=OLD_SHA,
                resolved=False, author_id=BOT_ID):
    note = {
        "resolved": resolved,
        "resolvable": True,
        "body": body,
        "author": {"id": author_id},
        "position": {
            "position_type": "text",
            "new_path": path,
            "new_line": line,
            "head_sha": head_sha,
        },
    }
    return SimpleNamespace(id="d1", attributes={"notes": [note]},
                           resolved=False, save=MagicMock())


def _provider(discussions, diffs) -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "owner/repo"
    provider.id_mr = 7
    provider.mr = MagicMock()
    provider.mr.diff_refs = {"head_sha": HEAD_SHA, "start_sha": START_SHA}
    provider.mr.discussions.list.return_value = discussions
    provider._get_own_user_id = MagicMock(return_value=BOT_ID)
    provider._get_merge_request = MagicMock(
        side_effect=lambda: SimpleNamespace(diff_refs=dict(provider.mr.diff_refs))
    )
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.repository_compare.return_value = {"diffs": diffs}
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": OLD_SHA}
    return provider


@pytest.fixture(autouse=True)
def flag():
    snapshot = snapshot_settings(["gitlab.auto_resolve_fixed_inline_threads"])
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", True)
    yield
    restore_settings(snapshot)


def test_removed_lines_from_patch() -> None:
    assert _removed_lines_from_patch(PATCH) == {2}
    assert _removed_lines_from_patch(INSERT_ONLY_PATCH) == set()


def test_no_newline_marker_is_not_a_file_line() -> None:
    # "\ No newline at end of file" must not advance the base line counter.
    assert _removed_lines_from_patch(NO_NEWLINE_PATCH) == {2}


def test_double_dash_content_line_is_a_removal() -> None:
    # A removed source line "--flag" is encoded as "---flag" inside the hunk.
    assert _removed_lines_from_patch(DOUBLE_DASH_PATCH) == {2}


def test_thread_on_removed_line_resolves() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    assert provider.reconcile_code_suggestion_threads() == 1
    assert discussion.resolved is True
    discussion.save.assert_called_once()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\f"],
                         ids=["line-separator", "paragraph-separator", "form-feed"])
def test_fixed_thread_resolution_uses_lf_line_boundaries(separator) -> None:
    patch = (
        "@@ -1,3 +1,2 @@\n"
        f' value = "before{separator}after"\n'
        "-obsolete = True\n"
        " kept = True\n"
    )
    removed_thread = _discussion(line=2)
    untouched_thread = _discussion(line=3)
    provider = _provider([removed_thread, untouched_thread], [{"old_path": "app.py", "diff": patch}])

    assert provider.reconcile_code_suggestion_threads() == 1
    assert removed_thread.resolved is True
    removed_thread.save.assert_called_once()
    assert untouched_thread.resolved is False
    untouched_thread.save.assert_not_called()


def test_compare_uses_straight_diff() -> None:
    # The default merge-base comparison would report lines dropped by a rebase
    # as removed; the sweep must compare the recorded head to the current head
    # directly.
    discussion = _discussion(line=2)
    diffs = [{"old_path": "app.py", "diff": PATCH}]
    provider = _provider([discussion], diffs)
    client = Gitlab("https://gitlab.example.com", private_token="test-token")
    client.http_get = MagicMock(side_effect=lambda path, **kwargs:
                               {"diffs": diffs} if path.endswith("/repository/compare") else {"id": OLD_SHA})
    provider.gl = client

    assert provider.reconcile_code_suggestion_threads() == 1
    assert discussion.resolved is True
    assert client.http_get.call_count == 2
    client.http_get.assert_any_call(
        "/projects/owner%2Frepo/repository/merge_base", query_data={"refs[]": [OLD_SHA, HEAD_SHA]}
    )
    client.http_get.assert_any_call(
        "/projects/owner%2Frepo/repository/compare", query_data={"from": OLD_SHA, "to": HEAD_SHA}, straight=True
    )


def test_thread_on_untouched_line_stays_open() -> None:
    discussion = _discussion(line=3)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    assert discussion.resolved is False
    discussion.save.assert_not_called()


def test_insertion_at_flagged_position_does_not_resolve() -> None:
    # A line inserted where the comment used to point must not resolve the
    # thread: coordinates belong to the comment's head, not the current one.
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": INSERT_ONLY_PATCH}])
    provider.reconcile_code_suggestion_threads()
    assert discussion.resolved is False


def test_thread_on_current_head_is_not_compared() -> None:
    discussion = _discussion(line=2, head_sha=HEAD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()
    assert discussion.resolved is False


def test_ineligible_threads_skip_compare() -> None:
    # Human and already-resolved threads must not cost a repository_compare call.
    foreign = _discussion(line=2, body="a human comment")
    other_author = _discussion(line=2, author_id=999)
    resolved_thread = _discussion(line=2, resolved=True)
    provider = _provider([foreign, other_author, resolved_thread],
                         [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()
    for d in (foreign, other_author, resolved_thread):
        d.save.assert_not_called()


def test_object_shaped_compare_response_is_handled() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_compare.return_value = SimpleNamespace(
        diffs=[SimpleNamespace(new_path="app.py", old_path="app.py", diff=PATCH)])
    provider.reconcile_code_suggestion_threads()
    assert discussion.resolved is True


def test_disabled_flag_is_noop() -> None:
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", False)
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    provider.mr.discussions.list.assert_not_called()
    assert discussion.resolved is False


@pytest.mark.parametrize("configured", ["false", "FALSE", "0"])
def test_string_false_disables_fixed_thread_resolution(configured) -> None:
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", configured)
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    provider.mr.discussions.list.assert_not_called()
    assert discussion.resolved is False


def test_non_agent_and_other_author_threads_stay_open() -> None:
    foreign = _discussion(line=2, body="a human comment")
    other_author = _discussion(line=2, author_id=999)
    provider = _provider([foreign, other_author], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    assert foreign.resolved is False
    assert other_author.resolved is False


def test_thread_eligibility_guards() -> None:
    assert _eligible_own_inline_thread(_discussion(resolved=True), BOT_ID) is None
    assert _eligible_own_inline_thread(_discussion(body="human"), BOT_ID) is None
    position = _eligible_own_inline_thread(_discussion(line=2), BOT_ID)
    assert position is not None
    assert _flagged_line_removed(position, {"app.py": {2}})
    assert not _flagged_line_removed(position, {"app.py": {3}})


def test_deletion_anchored_thread_stays_open() -> None:
    # old_line is a coordinate in the MR base, not the comment head: a head-side
    # removal at the same number must not resolve a deletion-anchored thread.
    position = {"position_type": "text", "new_path": "app.py", "old_path": "app.py",
                "old_line": 2, "head_sha": OLD_SHA}
    assert not _flagged_line_removed(position, {"app.py": {2}})


def test_repeat_sweep_costs_no_api_calls() -> None:
    # PRCodeSuggestions.run() and publish_code_suggestions share one provider;
    # the second sweep must not list discussions again.
    discussion = _discussion(line=3)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.reconcile_code_suggestion_threads()
    provider.reconcile_code_suggestion_threads()
    provider.mr.discussions.list.assert_called_once()


def test_fixed_thread_sweep_retries_after_head_advances_but_not_at_same_head() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": INSERT_ONLY_PATCH}])

    # Head B has not removed the flagged line, so the thread stays open.
    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    assert provider.mr.discussions.list.call_count == 1

    # Another command sees head C; its straight diff now removes the line.
    provider.mr.diff_refs["head_sha"] = "c" * 40
    provider.gl.projects.get.return_value.repository_compare.return_value = {
        "diffs": [{"old_path": "app.py", "diff": PATCH}],
    }
    assert provider.reconcile_code_suggestion_threads() == 1
    assert discussion.resolved is True
    discussion.save.assert_called_once()
    assert provider.mr.discussions.list.call_count == 2

    # C → C does not scan or resolve the same discussion twice.
    assert provider.reconcile_code_suggestion_threads() == 0
    assert provider.mr.discussions.list.call_count == 2


def test_fixed_thread_cleanup_does_not_resolve_after_head_changes_during_compare() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider._get_merge_request = MagicMock(return_value=SimpleNamespace(
        diff_refs={"head_sha": "c" * 40, "start_sha": START_SHA}
    ))

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    assert getattr(provider, "_fixed_threads_swept_revision", None) != (HEAD_SHA, START_SHA)


def test_fixed_cleanup_rejects_target_update_without_head_change() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider._get_merge_request = MagicMock(return_value=SimpleNamespace(
        diff_refs={"head_sha": HEAD_SHA, "start_sha": "new-target"}
    ))

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "missing_diffs", "collapsed", "too_large"])
def test_incomplete_compare_retries_at_same_head(failure) -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    compare = provider.gl.projects.get.return_value.repository_compare
    incomplete = {
        "timeout": {"compare_timeout": True, "diffs": []},
        "missing_diffs": {},
        "collapsed": {"diffs": [{"old_path": "app.py", "diff": PATCH, "collapsed": True}]},
        "too_large": {"diffs": [{"old_path": "app.py", "diff": PATCH, "too_large": True}]},
    }[failure]
    compare.side_effect = [incomplete, {"diffs": [{"old_path": "app.py", "diff": PATCH}]}]

    assert provider.reconcile_code_suggestion_threads() == 0
    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()


def test_empty_compare_patch_on_discussion_file_retries_at_same_head() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [])
    compare = provider.gl.projects.get.return_value.repository_compare
    compare.side_effect = [
        {"diffs": [{"old_path": "app.py", "diff": ""}]},
        {"diffs": [{"old_path": "app.py", "diff": PATCH}]},
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    assert getattr(provider, "_fixed_threads_swept_revision", None) is None
    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()
    assert compare.call_count == 2


def test_unrelated_empty_compare_patch_does_not_block_cleanup() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [
        {"old_path": "image.png", "diff": ""},
        {"old_path": "app.py", "diff": PATCH},
    ])

    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()
    assert provider.reconcile_code_suggestion_threads() == 0
    provider.gl.projects.get.return_value.repository_compare.assert_called_once()


@pytest.mark.parametrize("history", [
    [],
    [SimpleNamespace(id=1, head_commit_sha=OLD_SHA)],
    [SimpleNamespace(id=2, head_commit_sha=HEAD_SHA)],
])
def test_missing_diff_history_retries_at_same_head(history) -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.side_effect = [
        history,
        [SimpleNamespace(id=1, head_commit_sha=OLD_SHA),
         SimpleNamespace(id=2, head_commit_sha=HEAD_SHA)],
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()


def test_fixed_thread_cleanup_retries_after_failed_compare_at_same_head() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    compare = provider.gl.projects.get.return_value.repository_compare
    compare.side_effect = [
        GitlabError("temporary GitLab outage"),
        {"diffs": [{"old_path": "app.py", "diff": PATCH}]},
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()
    assert provider.mr.discussions.list.call_count == 2


def test_fixed_thread_cleanup_uses_verified_diff_head_after_mr_changes() -> None:
    # A fresh diff can be collected after the original MR object was read.
    discussion = _discussion(line=2, head_sha=HEAD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    new_head = "c" * 40
    provider.diff_files = [object()]
    provider._diff_head_sha = new_head
    provider._cleanup_diff_start_sha = START_SHA
    provider._get_merge_request = MagicMock(return_value=SimpleNamespace(
        diff_refs={"head_sha": new_head, "start_sha": START_SHA}
    ))
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": HEAD_SHA}

    assert provider.reconcile_code_suggestion_threads() == 1
    provider.gl.projects.get.return_value.repository_compare.assert_called_once_with(
        HEAD_SHA, new_head, straight=True
    )
    discussion.save.assert_called_once()


def test_fixed_thread_cleanup_does_not_compare_newer_discussion_backwards() -> None:
    discussion = _discussion(line=2, head_sha="c" * 40)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    # Head B is an ancestor of the discussion's head C, not vice versa.
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": HEAD_SHA}

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()


@pytest.mark.parametrize("malformed_head", [123, [OLD_SHA], {"sha": OLD_SHA}])
def test_malformed_discussion_head_skips_remote_comparisons(malformed_head) -> None:
    discussion = _discussion(line=2, head_sha=malformed_head)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    project = provider.gl.projects.get.return_value

    assert provider.reconcile_code_suggestion_threads() == 0
    project.repository_merge_base.assert_not_called()
    project.repository_compare.assert_not_called()
    discussion.save.assert_not_called()
    assert provider.reconcile_code_suggestion_threads() == 0  # Incomplete sweeps remain retryable.
    assert provider.mr.discussions.list.call_count == 2


def test_rebased_older_discussion_can_resolve_after_divergent_history() -> None:
    discussion = _discussion(line=2, head_sha=OLD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    # Pagination order is not a chronology guarantee; version IDs are.
    provider.mr.diffs.list.return_value = [
        SimpleNamespace(id=1, head_commit_sha=OLD_SHA),
        SimpleNamespace(id=2, head_commit_sha=HEAD_SHA),
    ]

    assert provider.reconcile_code_suggestion_threads() == 1
    provider.gl.projects.get.return_value.repository_compare.assert_called_once_with(
        OLD_SHA, HEAD_SHA, straight=True
    )
    discussion.save.assert_called_once()


def test_rebased_unchanged_line_is_not_resolved_after_divergent_history() -> None:
    discussion = _discussion(line=2, head_sha=OLD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": INSERT_ONLY_PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.return_value = [
        SimpleNamespace(id=2, head_commit_sha=HEAD_SHA),
        SimpleNamespace(id=1, head_commit_sha=OLD_SHA),
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    provider.gl.projects.get.return_value.repository_compare.assert_called_once_with(
        OLD_SHA, HEAD_SHA, straight=True
    )
    discussion.save.assert_not_called()


def test_divergent_newer_discussion_is_not_resolved_backwards() -> None:
    newer_head = "c" * 40
    discussion = _discussion(line=2, head_sha=newer_head)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.return_value = [
        SimpleNamespace(id=3, head_commit_sha=newer_head),
        SimpleNamespace(id=2, head_commit_sha=HEAD_SHA),
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()


def test_divergent_cleanup_retries_when_mr_version_history_was_unavailable() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.side_effect = [
        GitlabError("temporary history API outage"),
        [SimpleNamespace(id=2, head_commit_sha=HEAD_SHA),
         SimpleNamespace(id=1, head_commit_sha=OLD_SHA)],
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    assert provider.reconcile_code_suggestion_threads() == 1
    discussion.save.assert_called_once()
    assert provider.mr.discussions.list.call_count == 2


def test_incomplete_mr_version_history_never_authorizes_later_discussions() -> None:
    first = _discussion(head_sha="d" * 40, line=2)
    second = _discussion(head_sha=OLD_SHA, line=2)
    provider = _provider([first, second], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.return_value = [
        SimpleNamespace(id=2, head_commit_sha=HEAD_SHA),
        SimpleNamespace(id=1, head_commit_sha=OLD_SHA),
        SimpleNamespace(id=3, head_commit_sha=None),
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    first.save.assert_not_called()
    second.save.assert_not_called()
    provider.mr.diffs.list.assert_called_once_with(get_all=True, per_page=100)


def test_duplicate_mr_heads_cannot_authorize_divergent_cleanup() -> None:
    discussion = _discussion(line=2, head_sha=OLD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_merge_base.return_value = {"id": "0" * 40}
    provider.mr.diffs.list.return_value = [
        {"id": 3, "head_commit_sha": HEAD_SHA},
        {"id": 2, "head_commit_sha": OLD_SHA},
        {"id": 1, "head_commit_sha": HEAD_SHA},
    ]

    assert provider.reconcile_code_suggestion_threads() == 0
    discussion.save.assert_not_called()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()


def test_supports_code_suggestion_state() -> None:
    # PRCodeSuggestions.run() calls reconcile_code_suggestion_threads through
    # the generic provider contract when this reports support.
    provider = _provider([], [])
    assert provider.supports_code_suggestion_state() is True
    assert provider.reconcile_code_suggestion_threads() == 0
