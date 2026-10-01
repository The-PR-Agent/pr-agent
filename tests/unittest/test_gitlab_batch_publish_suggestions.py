import asyncio
import re
from unittest.mock import MagicMock, patch

import pytest
from gitlab import GitlabCreateError
from requests.exceptions import RequestException

from pr_agent.algo import inline_comment_dedup as dedup
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _FakeDiff:
    base_commit_sha = "base"
    start_commit_sha = "start"
    head_commit_sha = "head"


class _FakeTargetFile:
    filename = "a.py"
    old_filename = "a.py"
    head_file = "line1\nline2\nline3\n"
    patch = "@@ -1,2 +1,3 @@\n line1\n line2\n+line3\n"


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
    p.RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
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


def test_context_line_suggestion_sends_both_gitlab_line_numbers():
    p = _gl_provider()
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    position = p.mr.discussions.create.call_args.args[0]['position']
    assert position['old_line'] == 2
    assert position['new_line'] == 2


@pytest.mark.parametrize("start, expected", [
    (4, (3, 4)),  # context line whose text also appears as the added line 2
    (3, (2, 3)),  # blank context line
])
def test_anchor_is_positional_not_first_text_match(start, expected):
    class _RepeatingTargetFile(_FakeTargetFile):
        head_file = "a\nb\n\nb\n"
        patch = "@@ -1,3 +1,4 @@\n a\n+b\n \n b\n"

    p = _gl_provider()
    p.get_diff_files = MagicMock(return_value=[_RepeatingTargetFile()])
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion(relevant_lines_start=start, relevant_lines_end=start)]) is True
    finally:
        gs.stop()

    position = p.mr.discussions.create.call_args.args[0]['position']
    assert (position['old_line'], position['new_line']) == expected


def test_inverted_line_range_is_skipped_before_building_suggestion_body():
    p = _gl_provider()
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([
            _suggestion(relevant_lines_start=3, relevant_lines_end=2)
        ]) is False
    finally:
        gs.stop()

    p.mr.discussions.create.assert_not_called()


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
            raise GitlabCreateError("position rejected")
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
    p.mr.draft_notes.create.side_effect = GitlabCreateError("draft notes unavailable")
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
    p.mr.draft_notes.bulk_publish.side_effect = RequestException("network error")
    gs = _settings(as_review=True)
    try:
        # Queued drafts remain invisible until publication succeeds; request a retry.
        assert p.publish_code_suggestions([_suggestion()]) is False
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


@pytest.fixture(params=[False, True], ids=["no-persistence", "persistence"])
def publication_provider(request):
    provider = _gl_provider()
    settings = _settings(as_review=True, persistent_inline_comments=request.param)
    try:
        yield provider
    finally:
        settings.stop()


@pytest.mark.parametrize("failure", ["list", "bulk"])
def test_queued_draft_failure_retries_without_recreating_and_records_bodies(publication_provider, failure):
    p = publication_provider
    manager = p.mr.draft_notes
    original = getattr(manager, failure if failure == "list" else "bulk_publish").side_effect
    operation = manager.list if failure == "list" else manager.bulk_publish
    # Persistence loads drafts before creation; fail only once a new draft exists.
    def fail_after_queue(*args, **kwargs):
        if manager.create.called:
            raise RequestException("temporary publication failure")
        return original(*args, **kwargs)

    operation.side_effect = fail_after_queue
    assert p.publish_code_suggestions([_suggestion()]) is False
    assert manager.create.call_count == 1
    assert p.get_recent_inline_comment_bodies() == []
    assert p.publish_code_suggestions([]) is True
    assert manager.create.call_count == 1
    # A failed retry remains retryable; neither retry creates new suggestions.
    assert p.publish_code_suggestions([_suggestion(body="caller retry")]) is False
    operation.side_effect = original
    assert p.publish_code_suggestions([_suggestion(body="caller retry")]) is True
    assert manager.create.call_count == 1
    assert manager.list() == []
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]
    calls = manager.bulk_publish.call_count
    assert p.publish_code_suggestions([_suggestion(body="second caller retry")]) is True
    assert manager.bulk_publish.call_count == calls
    assert manager.create.call_count == 1


def test_live_fallback_survives_unavailable_draft_listing(publication_provider):
    p = publication_provider
    p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported")
    p.mr.draft_notes.list.side_effect = RequestException("drafts unsupported")
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.discussions.create.call_count == 1
    p.mr.draft_notes.bulk_publish.assert_not_called()
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


def test_mixed_live_and_queued_failure_still_requests_retry(publication_provider):
    p = publication_provider
    create = p.mr.draft_notes.create.side_effect

    def queue_or_fallback(payload):
        if "live fallback" in payload["note"]:
            raise GitlabCreateError("cannot draft this suggestion")
        return create(payload)

    p.mr.draft_notes.create.side_effect = queue_or_fallback
    p.mr.draft_notes.bulk_publish.side_effect = RequestException("cannot publish")
    assert p.publish_code_suggestions([
        _suggestion(body="live fallback", suggestion_content="live fallback"), _suggestion()
    ]) is False
    assert p.mr.discussions.create.call_count == 1
    assert len(p.mr.draft_notes.list()) == 1
    assert "live fallback" in p.get_recent_inline_comment_bodies()[0]
    assert all("fix it" not in body for body in p.get_recent_inline_comment_bodies())


@pytest.mark.parametrize("suggestions", [[], [_suggestion(relevant_file="absent.py")]])
def test_no_eligible_suggestion_does_not_publish_manual_drafts(publication_provider, suggestions):
    p = publication_provider
    p.mr.draft_notes.create({"note": "unrelated manual draft"})
    p.mr.draft_notes.create.reset_mock()
    assert p.publish_code_suggestions(suggestions) is (not suggestions)
    p.mr.draft_notes.bulk_publish.assert_not_called()
    assert p.mr.draft_notes.list()[0].note == "unrelated manual draft"
    assert p.get_recent_inline_comment_bodies() == []


@pytest.mark.parametrize("partial", [False, True])
def test_total_and_partial_live_failures(partial):
    p = _gl_provider()
    create = p.mr.discussions.create
    def fail_create(payload):
        raise GitlabCreateError("cannot create")
    create.side_effect = fail_create
    p.mr.notes.create.side_effect = GitlabCreateError("cannot fallback")
    suggestions = [_suggestion()]
    if partial:
        def fail_or_create(payload):
            if "working" in payload["body"]:
                return MagicMock()
            raise GitlabCreateError("cannot create")
        create.side_effect = fail_or_create
        suggestions.append(_suggestion(body="working"))
    settings = _settings()
    try:
        assert p.publish_code_suggestions(suggestions) is partial
    finally:
        settings.stop()


@pytest.mark.parametrize("as_review", [False, True])
def test_dedup_only_completed_run_succeeds_without_new_creation(as_review):
    p = _gl_provider()
    settings = _settings(as_review=as_review, persistent_inline_comments=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
        created = p.mr.draft_notes.create.call_count + p.mr.discussions.create.call_count
        assert p.publish_code_suggestions([_suggestion()]) is True
        assert p.mr.draft_notes.create.call_count + p.mr.discussions.create.call_count == created
    finally:
        settings.stop()


def test_initial_successful_bulk_records_real_queued_bodies(publication_provider):
    p = publication_provider
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.list() == []
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


def test_general_file_draft_is_tracked_for_list_failure(publication_provider):
    p = publication_provider
    create = p.mr.draft_notes.create.side_effect
    listing = p.mr.draft_notes.list.side_effect

    def general_file_only(payload):
        if "new_line" in payload["position"]:
            raise GitlabCreateError("anchor rejected")
        return create(payload)

    def fail_after_queue(*args, **kwargs):
        if p.mr.draft_notes.create.call_count >= 2:
            raise RequestException("cannot list")
        return listing(*args, **kwargs)

    p.mr.draft_notes.create.side_effect = general_file_only
    p.mr.draft_notes.list.side_effect = fail_after_queue
    assert p.publish_code_suggestions([_suggestion()]) is False
    p.mr.draft_notes.list.side_effect = listing
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.create.call_count == 2
    assert "Cannot implement directly" in p.get_recent_inline_comment_bodies()[0]


def test_empty_draft_body_is_not_recorded_as_a_published_finding(publication_provider):
    p = publication_provider
    # GitLab can return an empty existing draft body; a new persisted suggestion
    # itself would contain a dedup marker even if its input text were empty.
    p.mr.draft_notes.create({"note": ""})
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.list() == []
    assert len(p.get_recent_inline_comment_bodies()) == 1
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


@pytest.mark.parametrize("failure", ["bulk", "list", "drafts-unsupported"])
def test_improve_caller_retries_queued_batch_but_not_live_fallback(publication_provider, failure):
    p = publication_provider
    if failure == "drafts-unsupported":
        p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported")
        p.mr.draft_notes.list.side_effect = RequestException("cannot list")
    else:
        operation = p.mr.draft_notes.bulk_publish if failure == "bulk" else p.mr.draft_notes.list
        original = operation.side_effect
        failed = False

        def fail_once_after_queue(*args, **kwargs):
            nonlocal failed
            if p.mr.draft_notes.create.called and not failed:
                failed = True
                raise RequestException("transient failure")
            return original(*args, **kwargs)

        operation.side_effect = fail_once_after_queue
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = p
    tool.progress_response = None
    tool._validate_suggestion = lambda *args: (True, "", True)
    tool.dedent_code = lambda filename, line, code: code
    tool._validate_python_replacement_syntax = lambda *args: True
    asyncio.run(tool.push_inline_code_suggestions({"code_suggestions": [_suggestion()]},
                                                include_coverage_footer=False))
    assert tool._output_published is True
    p.mr.notes.create.assert_not_called()  # no duplicate summary on top of delivered comments
    assert p.get_recent_inline_comment_bodies()
    if failure == "drafts-unsupported":
        assert p.mr.discussions.create.call_count == 1
        p.mr.draft_notes.bulk_publish.assert_not_called()
    else:
        assert p.mr.draft_notes.create.call_count == 1
        assert p.mr.draft_notes.list() == []
