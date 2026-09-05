from unittest.mock import MagicMock, patch

from pr_agent.algo import inline_comment_dedup as dedup
from pr_agent.git_providers.gitlab_provider import GitLabProvider


class _FakeDiff:
    base_commit_sha = "base"
    start_commit_sha = "start"
    head_commit_sha = "head"


class _FakeTargetFile:
    filename = "a.py"
    old_filename = "a.py"
    head_file = "line1\nline2\nline3\n"


def _suggestion(**overrides):
    suggestion = {
        'body': "**Suggestion:** fix it\n```suggestion\nx = 2\n```",
        'relevant_file': 'a.py',
        'relevant_lines_start': 2,
        'relevant_lines_end': 2,
        'existing_code': 'x = 1',
        'improved_code': 'x = 2',
        'suggestion_content': 'fix it',
        'label': 'possible issue',
        'score': 7,
    }
    suggestion.update(overrides)
    return suggestion


def _gl_provider():
    """A GitLabProvider whose mr.draft_notes fake behaves like the real GitLab API: create()
    queues a pending draft, list() reflects whatever is currently pending, and bulk_publish()
    clears them - so tests exercise the same create -> list -> bulk_publish flow the real code
    depends on, instead of asserting on call counts alone."""
    p = GitLabProvider.__new__(GitLabProvider)
    p.id_mr = 1
    p.mr = MagicMock()
    p.mr.discussions.list.return_value = []
    p.mr.notes.list.return_value = []
    p.get_diff_files = MagicMock(return_value=[_FakeTargetFile()])
    p.get_relevant_diff = MagicMock(return_value=_FakeDiff())
    p.get_line_link = MagicMock(return_value="http://link")

    pending_drafts = []

    def _create(payload):
        note = MagicMock()
        note.note = payload.get('note')
        pending_drafts.append(note)
        return note

    def _list(get_all=True):
        return list(pending_drafts)

    def _bulk_publish():
        pending_drafts.clear()

    p.mr.draft_notes.create.side_effect = _create
    p.mr.draft_notes.list.side_effect = _list
    p.mr.draft_notes.bulk_publish.side_effect = _bulk_publish
    return p


def _settings(as_review=False, persistent_inline_comments=False):
    values = {
        "gitlab.publish_code_suggestions_as_review": as_review,
        "config.persistent_inline_comments": persistent_inline_comments,
    }

    def _get(key, default=None):
        return values.get(key, default)

    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _get
    return gs


def test_flag_off_posts_live_discussions_and_skips_bulk_publish():
    p = _gl_provider()
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    assert p.mr.discussions.create.call_count == 1
    p.mr.draft_notes.create.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_flag_on_queues_draft_notes_and_bulk_publishes_once():
    p = _gl_provider()
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion(), _suggestion()]) is True
    finally:
        gs.stop()

    assert p.mr.draft_notes.create.call_count == 2
    for call in p.mr.draft_notes.create.call_args_list:
        assert 'note' in call.args[0]
        assert 'position' in call.args[0]
    p.mr.discussions.create.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_called_once()
    assert p.mr.draft_notes.list(get_all=True) == []  # bulk_publish cleared the queue


def test_flag_on_fallback_uses_draft_note_not_live_note():
    p = _gl_provider()
    calls = []
    original_create = p.mr.draft_notes.create.side_effect

    def _create_first_call_rejected(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("position rejected")
        return original_create(payload)

    p.mr.draft_notes.create.side_effect = _create_first_call_rejected
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    # first call: primary attempt (raises); second call: fallback general draft note
    assert len(calls) == 2
    assert 'note' in calls[1]
    p.mr.notes.create.assert_not_called()
    p.mr.discussions.create.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_called_once()


def test_draft_totally_unavailable_falls_back_to_a_live_comment_not_a_dropped_suggestion():
    # Both draft attempts (primary anchored + general-note fallback) fail outright, e.g. the
    # draft-notes endpoint is unsupported/erroring for this MR. The suggestion must still be
    # posted, just live instead of batched - not silently dropped.
    p = _gl_provider()
    p.mr.draft_notes.create.side_effect = RuntimeError("draft notes unavailable")
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    assert p.mr.discussions.create.call_count == 1
    # nothing ever made it into drafts, so there's nothing to bulk-publish
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_bulk_publish_failure_is_caught_and_does_not_propagate():
    p = _gl_provider()
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("network error")
    gs = _settings(as_review=True)
    try:
        # must not raise, and must still report success for the individually-queued suggestions
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.bulk_publish.assert_called_once()


def test_empty_suggestions_does_not_bulk_publish_unrelated_pending_drafts():
    # Regression: bulk_publish() must not fire when nothing is pending, since it would
    # otherwise publish any unrelated drafts already on the MR for this user.
    p = _gl_provider()
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_all_suggestions_failing_to_queue_does_not_bulk_publish():
    p = _gl_provider()
    # file lookup will fail for every suggestion -> zero drafts actually queued
    p.get_diff_files = MagicMock(return_value=[])
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_bulk_publish_still_fires_for_stuck_drafts_even_if_this_run_dedupes_everything():
    # Regression for the fix above: gating bulk_publish on "did *this* call create a draft" would
    # mean a run where every suggestion is skipped by persistent-inline-comment dedup (because its
    # marker is already on a still-pending draft from an earlier run whose bulk_publish failed)
    # would never retry publishing that stuck draft. Gating on the MR's actual pending drafts
    # instead means it's still retried.
    p = _gl_provider()
    suggestion = _suggestion()
    range_ = suggestion['relevant_lines_end'] - suggestion['relevant_lines_start']
    posted_body = suggestion['body'].replace('```suggestion', f'```suggestion:-0+{range_}')
    anchor_line = suggestion['relevant_lines_start'] + 1  # target_line_no for an 'addition' edit
    seen_fp = dedup.body_fingerprint(suggestion['relevant_file'], anchor_line, posted_body)
    stuck_draft = MagicMock()
    stuck_draft.note = f"stuck from a previous run\n\n<!-- pr-agent-dedup: {seen_fp} -->"
    p.mr.draft_notes.list.side_effect = None
    p.mr.draft_notes.list.return_value = [stuck_draft]

    gs = _settings(as_review=True, persistent_inline_comments=True)
    try:
        assert p.publish_code_suggestions([suggestion]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_not_called()  # skipped as a duplicate of the stuck draft
    p.mr.draft_notes.bulk_publish.assert_called_once()  # but still retried
