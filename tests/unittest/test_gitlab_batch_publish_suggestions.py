from unittest.mock import MagicMock, patch

import pytest

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
    published_notes = []

    def _publish(payload):
        note = MagicMock()
        note.body = payload['body']
        published_notes.append(note)
        return note

    p.mr.notes.list.side_effect = lambda get_all=True: list(published_notes)
    p.mr.notes.create.side_effect = _publish
    p.mr.discussions.create.side_effect = _publish

    def _create(payload):
        note = MagicMock()
        note.note = payload.get('note')
        pending_drafts.append(note)
        return note

    def _list(get_all=True):
        return list(pending_drafts)

    def _bulk_publish():
        for draft in pending_drafts:
            _publish({'body': draft.note})
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
        # Queued drafts are still invisible to the reviewer; the caller must retry.
        assert p.publish_code_suggestions([_suggestion()]) is False
    finally:
        gs.stop()

    p.mr.draft_notes.bulk_publish.assert_called_once()


def test_empty_suggestions_does_not_bulk_publish_unrelated_pending_drafts():
    # An empty input is a no-op even when the user has an unrelated draft review.
    p = _gl_provider()
    p.mr.draft_notes.create({'note': 'unrelated manual draft'})
    p.mr.draft_notes.create.reset_mock()
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
        assert p.publish_code_suggestions([_suggestion()]) is False
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


@pytest.fixture
def publication_settings(monkeypatch):
    def configure(as_review=False, persistent=False):
        monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.get_settings", lambda: {
            "gitlab.publish_code_suggestions_as_review": as_review,
            "config.persistent_inline_comments": persistent,
        })
    return configure


@pytest.mark.parametrize("as_review", [False, True])
def test_total_creation_failure_reports_failure(publication_settings, as_review):
    publication_settings(as_review=as_review)
    p = _gl_provider()
    p.mr.draft_notes.create.side_effect = RuntimeError("draft endpoint unavailable")
    p.mr.discussions.create.side_effect = RuntimeError("discussion rejected")
    p.mr.notes.create.side_effect = RuntimeError("fallback rejected")

    assert p.publish_code_suggestions([_suggestion(), _suggestion()]) is False
    assert p.mr.discussions.create.call_count == 2
    assert p.mr.notes.create.call_count == 2
    p.mr.draft_notes.bulk_publish.assert_not_called()


@pytest.mark.parametrize("failure_first", [False, True])
def test_partial_live_publication_keeps_processing_the_batch(publication_settings, failure_first):
    publication_settings()
    p = _gl_provider()
    p.mr.discussions.create.side_effect = (
        [RuntimeError("rejected"), MagicMock()] if failure_first else [MagicMock(), RuntimeError("rejected")])
    p.mr.notes.create.side_effect = RuntimeError("fallback rejected")

    assert p.publish_code_suggestions([_suggestion(), _suggestion()]) is True
    assert p.mr.discussions.create.call_count == 2
    p.mr.notes.create.assert_called_once()


def test_live_general_note_fallback_counts_as_published(publication_settings):
    publication_settings()
    p = _gl_provider()
    p.mr.discussions.create.side_effect = RuntimeError("position rejected")

    assert p.publish_code_suggestions([_suggestion()]) is True
    assert len(p.mr.notes.list()) == 1


@pytest.mark.parametrize("suggestion", [{}, _suggestion(relevant_file="missing.py")])
def test_invalid_suggestions_do_not_report_success(publication_settings, suggestion):
    publication_settings()
    p = _gl_provider()

    assert p.publish_code_suggestions([suggestion]) is False
    p.mr.discussions.create.assert_not_called()


@pytest.mark.parametrize("as_review", [False, True])
def test_published_duplicate_is_a_successful_noop_across_runs(publication_settings, as_review):
    publication_settings(as_review=as_review, persistent=True)
    p = _gl_provider()
    assert p.publish_code_suggestions([_suggestion()]) is True
    # A new provider must rediscover the public marker, not rely on its in-memory store.
    next_run = _gl_provider()
    next_run.mr = p.mr
    creates = p.mr.discussions.create.call_count + p.mr.draft_notes.create.call_count
    publishes = p.mr.draft_notes.bulk_publish.call_count

    assert next_run.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.discussions.create.call_count + p.mr.draft_notes.create.call_count == creates
    assert p.mr.draft_notes.bulk_publish.call_count == publishes


@pytest.mark.parametrize("persistent", [False, True])
def test_mixed_live_and_pending_draft_reports_failure_then_retries_without_duplicates(publication_settings, persistent):
    publication_settings(as_review=True, persistent=persistent)
    p = _gl_provider()
    create_draft = p.mr.draft_notes.create.side_effect
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect

    def selectively_create(payload):
        if 'live fallback' in payload['note']:
            raise RuntimeError("draft rejected")
        return create_draft(payload)

    p.mr.draft_notes.create.side_effect = selectively_create
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("publish unavailable")
    live = _suggestion(body="live fallback", improved_code="live fallback")
    draft = _suggestion()

    assert p.publish_code_suggestions([live, draft]) is False
    assert len(p.mr.notes.list()) == 1
    assert len(p.mr.draft_notes.list()) == 1

    # Mirror the caller's individual retries: even the already-live suggestion cannot
    # hide a failed publish of the draft left over from the batch.
    assert p.publish_code_suggestions([live]) is False
    p.mr.draft_notes.bulk_publish.side_effect = publish_drafts
    assert p.publish_code_suggestions([draft]) is True
    assert len(p.mr.notes.list()) == 2
    assert p.mr.draft_notes.list() == []
    p.mr.discussions.create.assert_called_once()
    assert p.mr.draft_notes.create.call_count == 3  # two rejected fallbacks + one draft
    assert p.publish_code_suggestions([live, draft]) is True
    assert len(p.mr.notes.list()) == (2 if persistent else 4)


@pytest.mark.parametrize("notes_available", [True, False])
@pytest.mark.parametrize("human_resolved", [True, False])
def test_publication_verification_distinguishes_superseded_and_human_resolved_notes(
    monkeypatch, notes_available, human_resolved
):
    settings = {
        "GITLAB.RESOLVE_OUTDATED_INLINE_THREADS": True,
        "gitlab.publish_code_suggestions_as_review": True,
        "config.persistent_inline_comments": True,
    }
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.get_settings", lambda: settings)
    p = _gl_provider()
    body = _suggestion()['body'].replace("```suggestion", "```suggestion:-0+0")
    old_body = dedup.body_with_markers(
        body, dedup.body_fingerprint("a.py", 3, body), dedup.code_fingerprint("a.py", 3, body))
    old_public = p.mr.notes.create({'body': old_body})
    old_public.id = 10
    old_note = {
        'id': 10, 'body': old_body, 'author': {'id': 7}, 'resolved': human_resolved, 'resolvable': True,
        'position': {'position_type': 'text', 'head_sha': 'old-head', 'new_line': 2},
    }
    thread = MagicMock()
    thread.id = "old-thread"
    thread.attributes = {'notes': [old_note]}
    thread.save.side_effect = lambda: old_note.update(resolved=True)
    p.mr.discussions.list.return_value = [thread]
    p.mr.diff_refs = {'head_sha': 'head'}
    p._get_own_user_id = lambda: 7
    public_notes = p.mr.notes.list.side_effect
    pending = p.mr.draft_notes.list.side_effect
    publish = p.mr.draft_notes.bulk_publish.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    if not notes_available:
        p.mr.notes.list.side_effect = RuntimeError("cannot list public notes")

    assert p.publish_code_suggestions([_suggestion()]) is human_resolved
    if human_resolved:
        # Human-resolved comments intentionally remain suppressive, including on an older head.
        thread.save.assert_not_called()
        p.mr.draft_notes.create.assert_not_called()
        return

    thread.save.assert_called_once()
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list drafts")
    assert p.publish_code_suggestions([_suggestion()]) is False
    assert len(pending()) == 1

    publish()
    replacement = public_notes()[-1]
    replacement.id = 11
    new_thread = MagicMock()
    new_thread.attributes = {'notes': [{'id': 11, 'body': replacement.body}]}
    p.mr.discussions.list.return_value.append(new_thread)
    assert p.publish_code_suggestions([_suggestion()]) is True
    p.mr.draft_notes.create.assert_called_once()
    assert len(pending()) == 0


def test_draft_listing_failure_does_not_claim_queued_drafts_are_public(publication_settings):
    publication_settings(as_review=True)
    p = _gl_provider()
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    assert p.publish_code_suggestions([_suggestion()]) is False
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_unavailable_draft_endpoint_preserves_successful_live_fallback(publication_settings):
    publication_settings(as_review=True)
    p = _gl_provider()
    p.mr.draft_notes.create.side_effect = RuntimeError("unsupported")
    p.mr.draft_notes.list.side_effect = RuntimeError("unsupported")

    assert p.publish_code_suggestions([_suggestion()]) is True
    assert len(p.mr.notes.list()) == 1


def test_listing_failure_on_duplicate_pending_draft_still_reports_failure(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([_suggestion()]) is False
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    assert p.publish_code_suggestions([_suggestion()]) is False
    p.mr.draft_notes.create.assert_called_once()


def test_missing_diff_reports_failure_and_continues(publication_settings):
    publication_settings()
    p = _gl_provider()
    p.get_relevant_diff.side_effect = [None, _FakeDiff()]

    assert p.publish_code_suggestions([_suggestion(), _suggestion()]) is True
    p.mr.discussions.create.assert_called_once()
    assert p.get_relevant_diff.call_count == 2


def test_wrapped_original_suggestion_preserves_general_note_fallback(publication_settings):
    publication_settings()
    p = _gl_provider()
    p.mr.discussions.create.side_effect = RuntimeError("position rejected")

    assert p.publish_code_suggestions([_suggestion(original_suggestion=_suggestion())]) is True
    assert 'fix it' in p.mr.notes.list()[0].body


@pytest.mark.parametrize("as_draft", [False, True])
@pytest.mark.parametrize("edit_type", ["addition", "deletion", "context"])
def test_send_inline_comment_keeps_boolean_creation_contract(publication_settings, as_draft, edit_type):
    publication_settings(persistent=True)
    p = _gl_provider()
    args = ("body", edit_type, True, "a.py", "line2", 3, _FakeTargetFile(), 3, _suggestion())

    assert p.send_inline_comment(*args, as_draft=as_draft) is True
    assert p.send_inline_comment(*args, as_draft=as_draft) is False  # duplicate, not a new creation


def test_send_inline_comment_without_position_keeps_false_result(publication_settings):
    publication_settings()
    p = _gl_provider()

    assert p.send_inline_comment("body", "addition", False, "a.py", "line2", -1, _FakeTargetFile(), 3) is False
    p.get_relevant_diff.assert_not_called()
    p.mr.discussions.create.assert_not_called()


@pytest.mark.parametrize("fresh_provider", [False, True])
def test_public_duplicate_succeeds_when_draft_endpoint_is_unavailable(publication_settings, fresh_provider):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    p.mr.draft_notes.create.side_effect = RuntimeError("unsupported")
    p.mr.draft_notes.list.side_effect = RuntimeError("unsupported")
    assert p.publish_code_suggestions([_suggestion()]) is True
    retry = _gl_provider() if fresh_provider else p
    retry.mr = p.mr

    assert retry.publish_code_suggestions([_suggestion()]) is True
    assert len(p.mr.notes.list()) == 1
    p.mr.discussions.create.assert_called_once()
    p.mr.draft_notes.bulk_publish.assert_not_called()


@pytest.mark.parametrize("notes_error", [False, True])
@pytest.mark.parametrize("as_review", [False, True])
def test_public_duplicate_in_discussion_preserves_success_on_draft_endpoint_error(
        publication_settings, notes_error, as_review):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    assert p.publish_code_suggestions([_suggestion()]) is True
    discussion = MagicMock()
    discussion.attributes = {"notes": [None, {}, {"body": p.mr.notes.list()[0].body}]}
    unrelated = MagicMock()
    unrelated.attributes = {"notes": [{"body": "unrelated comment"}]}
    p.mr.notes.list.side_effect = RuntimeError("cannot list notes") if notes_error else lambda get_all=True: []
    p.mr.discussions.list.return_value = [unrelated, discussion]
    p.mr.draft_notes.list.side_effect = RuntimeError("unsupported")
    publication_settings(as_review=as_review, persistent=True)

    assert p.publish_code_suggestions([_suggestion()]) is True
    p.mr.draft_notes.create.assert_called_once()


@pytest.mark.parametrize("as_review", [False, True])
def test_unverifiable_duplicate_does_not_claim_publication(publication_settings, as_review):
    publication_settings(as_review=as_review, persistent=True)
    p = _gl_provider()
    assert p.publish_code_suggestions([_suggestion()]) is True
    list_notes = p.mr.notes.list.side_effect
    p.mr.notes.list.side_effect = RuntimeError("cannot verify")
    p.mr.discussions.list.side_effect = RuntimeError("cannot verify discussions")
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    assert p.publish_code_suggestions([_suggestion()]) is False
    assert p.mr.draft_notes.create.call_count == int(as_review)
    p.mr.notes.list.side_effect = list_notes
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.create.call_count == int(as_review)
    if not as_review:
        assert p.publish_code_suggestions([_suggestion(body="independent")]) is True
        assert len(p.mr.notes.list()) == 2


def test_pending_duplicate_is_not_successful_after_disabling_review_mode(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([_suggestion()]) is False
    publication_settings(persistent=True)

    assert p.publish_code_suggestions([_suggestion()]) is False
    assert len(p.mr.draft_notes.list()) == 1
    p.mr.draft_notes.create.assert_called_once()
    p.mr.discussions.create.assert_not_called()


def test_concurrently_published_drafts_do_not_report_failure(publication_settings):
    publication_settings(as_review=True)
    p = _gl_provider()
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect

    def list_after_concurrent_publication(get_all=True):
        publish_drafts()
        return []

    p.mr.draft_notes.list.side_effect = list_after_concurrent_publication

    assert p.publish_code_suggestions([_suggestion()]) is True
    assert len(p.mr.notes.list()) == 1
    p.mr.draft_notes.bulk_publish.assert_not_called()


@pytest.mark.parametrize("repeated_key", [False, True])
@pytest.mark.parametrize("first_retry_fails", [False, True])
def test_failed_batch_retries_without_duplicates_when_persistence_is_disabled(
        publication_settings, repeated_key, first_retry_fails):
    publication_settings(as_review=True)
    p = _gl_provider()
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("temporary publication failure")
    suggestions = [_suggestion(), _suggestion() if repeated_key else _suggestion(body="second", improved_code="second")]

    assert p.publish_code_suggestions(suggestions) is False
    for index, suggestion in enumerate(suggestions):
        if not first_retry_fails or index:
            p.mr.draft_notes.bulk_publish.side_effect = publish_drafts
        assert p.publish_code_suggestions([suggestion]) is (not first_retry_fails or bool(index))
    assert len(p.mr.notes.list()) == 2
    assert p.mr.draft_notes.create.call_count == 2

    # The retry identities are consumed: persistence remains opt-in for later runs.
    assert p.publish_code_suggestions(suggestions) is True
    assert len(p.mr.notes.list()) == 4


def test_pending_batch_is_not_hidden_by_live_retry_when_draft_listing_fails(publication_settings):
    publication_settings(as_review=True)
    p = _gl_provider()
    create_draft = p.mr.draft_notes.create.side_effect

    def selectively_create(payload):
        if "live" in payload['note']:
            raise RuntimeError("draft rejected")
        return create_draft(payload)

    live = _suggestion(body="live", improved_code="live", suggestion_content="live")
    p.mr.draft_notes.create.side_effect = selectively_create
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([live, _suggestion()]) is False
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list pending drafts")

    assert p.publish_code_suggestions([live]) is False
    assert len(p.mr.notes.list()) == 1


def test_public_duplicate_is_found_among_unrelated_notes(publication_settings):
    publication_settings(persistent=True)
    p = _gl_provider()
    p.mr.notes.create({'body': 'unrelated comment'})

    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert len(p.mr.notes.list()) == 2
    p.mr.discussions.create.assert_called_once()


@pytest.mark.parametrize("listing_fails_after_dedup", [False, True])
def test_rediscovered_pending_draft_blocks_live_retry_when_listing_becomes_unavailable(
        publication_settings, listing_fails_after_dedup):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    draft = _suggestion()
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([draft]) is False
    retry = _gl_provider()
    retry.mr = p.mr
    create_draft = retry.mr.draft_notes.create.side_effect

    def selectively_create(payload):
        if "live" in payload['note']:
            raise RuntimeError("draft rejected")
        return create_draft(payload)

    live = _suggestion(body="live", improved_code="live", suggestion_content="live")
    retry.mr.draft_notes.create.side_effect = selectively_create
    pending = retry.mr.draft_notes.list.side_effect
    if listing_fails_after_dedup:
        calls = 0

        def list_once(get_all=True):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("cannot list")
            return pending()

        retry.mr.draft_notes.list.side_effect = list_once
    assert retry.publish_code_suggestions([live, draft]) is False
    retry.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    assert retry.publish_code_suggestions([live]) is False
    assert retry.publish_code_suggestions([draft]) is False
    assert len(pending()) == 1
    assert len(retry.mr.notes.list()) == 1


def test_lost_bulk_publish_response_is_verified_through_public_markers(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect
    suggestions = [_suggestion(), _suggestion(body="second", improved_code="second")]

    def publish_then_timeout():
        publish_drafts()
        raise RuntimeError("response timed out after publication")

    p.mr.draft_notes.bulk_publish.side_effect = publish_then_timeout
    assert p.publish_code_suggestions(suggestions) is False
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    for suggestion in suggestions:
        assert p.publish_code_suggestions([suggestion]) is True
    assert len(p.mr.notes.list()) == 2
    assert p.mr.draft_notes.create.call_count == 2


def test_verifying_one_public_draft_does_not_hide_another_pending_draft(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    suggestions = [_suggestion(), _suggestion(body="second", improved_code="second")]
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions(suggestions) is False
    pending = p.mr.draft_notes.list.side_effect
    # One draft becomes public, but the other remains private and retryable.
    p.mr.notes.create({'body': pending()[0].note})
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    for suggestion in suggestions:
        assert p.publish_code_suggestions([suggestion]) is False
    assert len(p.mr.notes.list()) == 1
    assert p.mr.draft_notes.create.call_count == 2


def test_new_draft_is_not_hidden_by_an_older_public_snapshot(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    first = _suggestion(body="first", improved_code="first", suggestion_content="first")
    second = _suggestion(body="second", improved_code="second", suggestion_content="second")
    create_draft = p.mr.draft_notes.create.side_effect
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect
    pending = p.mr.draft_notes.list.side_effect

    def reject_first(payload):
        if "first" in payload['note']:
            raise RuntimeError("first rejected")
        return create_draft(payload)

    def publish_then_timeout():
        publish_drafts()
        raise RuntimeError("lost response")

    p.mr.draft_notes.create.side_effect = reject_first
    p.mr.discussions.create.side_effect = RuntimeError("live rejected")
    p.mr.notes.create.side_effect = RuntimeError("fallback rejected")
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([first, second]) is False
    p.mr.draft_notes.bulk_publish.side_effect = publish_then_timeout
    assert p.publish_code_suggestions([second]) is False
    assert len(p.mr.notes.list()) == 1
    p.mr.draft_notes.create.side_effect = create_draft
    p.mr.draft_notes.list.side_effect = RuntimeError("cannot list")

    assert p.publish_code_suggestions([first]) is False
    assert len(pending()) == 1
    assert len(p.mr.notes.list()) == 1


def test_code_duplicate_with_new_wording_can_verify_publication_after_listing_failure(publication_settings):
    publication_settings(as_review=True, persistent=True)
    p = _gl_provider()
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([_suggestion()]) is False
    retry = _gl_provider()
    retry.mr = p.mr
    # Load the old pending marker, then lose the draft endpoint for subsequent calls.
    dedup.get_inline_comment_store(retry).load()
    retry.mr.draft_notes.list.side_effect = RuntimeError("cannot list")
    reworded = _suggestion(body=_suggestion()['body'].replace("fix it", "another explanation"))
    assert retry.publish_code_suggestions([reworded]) is False
    publish_drafts()

    assert retry.publish_code_suggestions([reworded]) is True
    assert len(retry.mr.notes.list()) == 1
    retry.mr.draft_notes.create.assert_called_once()


@pytest.mark.parametrize("failed_method", ["get_diff_files", "get_relevant_diff"])
def test_initial_exception_does_not_leave_a_retry_identity_for_later_runs(publication_settings, failed_method):
    publication_settings(as_review=True)
    p = _gl_provider()
    method = getattr(p, failed_method)
    method.side_effect = RuntimeError("transient diff API failure")
    assert p.publish_code_suggestions([_suggestion()]) is False
    method.side_effect = None
    publish_drafts = p.mr.draft_notes.bulk_publish.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([_suggestion()]) is False
    assert len(p.mr.draft_notes.list()) == 1
    p.mr.draft_notes.bulk_publish.side_effect = publish_drafts

    # The caller's retry was consumed despite its failure; a later run may repost.
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.create.call_count == 2
    assert len(p.mr.notes.list()) == 2


@pytest.mark.parametrize("still_pending", [True, False])
def test_live_mode_refreshes_markerless_drafts_without_hiding_pending_work(publication_settings, still_pending):
    publication_settings(as_review=True)
    p = _gl_provider()
    publish = p.mr.draft_notes.bulk_publish.side_effect
    pending = p.mr.draft_notes.list.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = RuntimeError("cannot publish")
    assert p.publish_code_suggestions([_suggestion()]) is False
    if not still_pending:
        publish()
    publication_settings(as_review=False)

    assert p.publish_code_suggestions([_suggestion()]) is (not still_pending)
    p.mr.draft_notes.create.assert_called_once()
    p.mr.discussions.create.assert_not_called()
    assert len(pending()) == int(still_pending)
    if still_pending:
        publish()
        p.mr.draft_notes.list.side_effect = RuntimeError("cannot refresh yet")
        assert p.publish_code_suggestions([_suggestion(body="new live suggestion")]) is False
        p.mr.draft_notes.list.side_effect = pending

    assert p.publish_code_suggestions([_suggestion(body="independent live suggestion")]) is True
    assert p._code_suggestion_drafts_pending is False
