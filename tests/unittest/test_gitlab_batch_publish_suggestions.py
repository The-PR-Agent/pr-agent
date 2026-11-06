import asyncio
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from gitlab import GitlabCreateError
from requests.exceptions import RequestException
from starlette_context import request_cycle_context

import pr_agent.git_providers as providers
from pr_agent.algo import inline_comment_dedup as dedup
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    GitLabSuggestionPublicationError,
    IncompleteGitLabDiffError,
)
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
    p._get_merge_request = MagicMock(return_value=SimpleNamespace(
        sha="head",
        diff_refs={"base_sha": "base", "start_sha": "start", "head_sha": "head"},
    ))

    pending_drafts = []
    p._test_created_drafts = []

    def _create(payload):
        note = MagicMock()
        note.note = payload.get('note')
        note.position = payload.get('position')
        note.attributes = {"position": note.position}
        pending_drafts.append(note)
        p._test_created_drafts.append(note)
        note.delete.side_effect = lambda: pending_drafts.remove(note)
        note.publish.side_effect = lambda: pending_drafts.remove(note)
        return note

    def _list(get_all=True, **kwargs):
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


def _prepare_verified_command_reset(provider):
    # A command boundary must read a complete live MR before it can reuse the
    # provider. Keep the fake draft API attached to that refreshed MR.
    provider.mr.target_branch = "main"
    provider.mr.sha = "head"
    provider.mr.diff_refs = {"base_sha": "base", "start_sha": "start", "head_sha": "head"}
    provider._get_merge_request.return_value = provider.mr


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


@pytest.mark.parametrize("as_review", [False, True])
@pytest.mark.parametrize("start,old_line", [(1, None), (2, 2)])
def test_patch_only_suggestion_publishes_at_its_addition_or_context_line(as_review, start, old_line):
    target = _FakeTargetFile()
    target.head_file = ""
    target.patch = "@@ -1,2 +1,2 @@\n-old line1\n+line1\n line2\n\\ No newline at end of file\n"
    p = _gl_provider()
    p.get_diff_files.return_value = [target]
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = p
    tool.progress_response = None
    suggestion = _suggestion(relevant_lines_start=start, relevant_lines_end=start, existing_code=f'line{start}')
    gs = _settings(as_review=as_review)
    try:
        asyncio.run(tool.push_inline_code_suggestions({"code_suggestions": [suggestion]},
                                                     include_coverage_footer=False))
    finally:
        gs.stop()

    create = p.mr.draft_notes.create if as_review else p.mr.discussions.create
    create.assert_called_once()
    assert '```suggestion:-0+0' in create.call_args.args[0]['note' if as_review else 'body']
    position = create.call_args.args[0]['position']
    assert position['new_line'] == start
    assert position.get('old_line') == old_line
    p.get_relevant_diff.assert_called_once_with('a.py', f'line{start}')
    if as_review:
        p.mr.draft_notes.bulk_publish.assert_called_once()
        assert p.mr.draft_notes.list(get_all=True) == []


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
            raise GitlabCreateError("position rejected", response_code=400)
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
    p.mr.draft_notes.create.side_effect = GitlabCreateError("draft notes unavailable", response_code=404)
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    assert p.mr.discussions.create.call_count == 1
    # nothing ever made it into drafts, so there's nothing to bulk-publish
    p.mr.draft_notes.bulk_publish.assert_not_called()


def test_live_note_fallback_repeats_the_score_why_disclosure():
    p = _gl_provider()
    p.mr.discussions.create.side_effect = GitlabCreateError("position rejected", response_code=400)
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([
            _suggestion(score_why="Self-reflection unavailable; score not model-assigned")]) is True
    finally:
        gs.stop()

    fallback_body = p.mr.notes.create.call_args.args[0]['body']
    assert "importance: 7" in fallback_body
    assert "Why: Self-reflection unavailable; score not model-assigned" in fallback_body


def test_bulk_publish_failure_is_caught_and_does_not_propagate():
    p = _gl_provider()
    p.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
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


def test_stuck_draft_from_another_run_does_not_suppress_or_publish_new_suggestion():
    # A private draft is not a published deduplication result. Only this
    # provider's tracked draft may be published automatically.
    p = _gl_provider()
    suggestion = _suggestion()
    range_ = suggestion['relevant_lines_end'] - suggestion['relevant_lines_start']
    posted_body = suggestion['body'].replace('```suggestion', f'```suggestion:-0+{range_}')
    anchor_line = suggestion['relevant_lines_start'] + 1  # target_line_no for an 'addition' edit
    seen_fp = dedup.body_fingerprint(suggestion['relevant_file'], anchor_line, posted_body)
    stuck_draft = MagicMock()
    stuck_draft.note = f"stuck from a previous run\n\n<!-- pr-agent-dedup: {seen_fp} -->"
    stuck_draft.position = {"base_sha": "base", "start_sha": "start", "head_sha": "head"}
    original_create = p.mr.draft_notes.create.side_effect
    pending = []

    def create(payload):
        draft = original_create(payload)
        pending.append(draft)
        return draft

    p.mr.draft_notes.create.side_effect = create
    p.mr.draft_notes.list.side_effect = lambda **_kwargs: [stuck_draft, *pending]

    gs = _settings(as_review=True, persistent_inline_comments=True)
    try:
        assert p.publish_code_suggestions([suggestion]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_called_once()
    p._test_created_drafts[-1].publish.assert_called_once()
    stuck_draft.publish.assert_not_called()
    stuck_draft.delete.assert_not_called()
    p.mr.draft_notes.bulk_publish.assert_not_called()


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
            if failure == "bulk":
                raise GitlabCreateError("bulk rejected", response_code=400)
            raise RequestException("temporary publication failure")
        return original(*args, **kwargs)

    operation.side_effect = fail_after_queue
    suggestion = _suggestion()
    assert p.publish_code_suggestions([suggestion]) is False
    assert manager.create.call_count == 1
    assert p.get_recent_inline_comment_bodies() == []
    assert p.publish_code_suggestions([]) is True
    assert manager.create.call_count == 1
    # A failed retry remains retryable; neither retry creates new suggestions.
    assert p.publish_code_suggestions([suggestion]) is False
    operation.side_effect = original
    assert p.publish_code_suggestions([suggestion]) is True
    assert manager.create.call_count == 1
    assert manager.list() == []
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]
    calls = manager.bulk_publish.call_count
    assert p.publish_code_suggestions([suggestion]) is True
    assert manager.bulk_publish.call_count == calls
    assert manager.create.call_count == 1
    # A distinct batch on the same provider, e.g. /improve after /review, is published.
    new_batch = [_suggestion(body="**Suggestion:** other", relevant_lines_start=3, relevant_lines_end=3)]
    assert p.publish_code_suggestions(new_batch) is True
    assert manager.create.call_count == 2
    assert manager.list() == []


@pytest.mark.parametrize("retry_first", [False, True])
@pytest.mark.parametrize("include_old", [False, True])
def test_distinct_or_mixed_batch_publishes_after_failure(publication_provider, request, retry_first, include_old):
    p = publication_provider
    manager = p.mr.draft_notes
    original = manager.bulk_publish.side_effect
    manager.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
    old = _suggestion()
    assert p.publish_code_suggestions([old]) is False
    manager.bulk_publish.side_effect = original
    if retry_first:
        # Equal dictionaries, not only the original object, remain a retry.
        assert p.publish_code_suggestions([dict(old)]) is True
        assert manager.create.call_count == 1
    new = _suggestion(body="new command", improved_code="x = 3")
    assert p.publish_code_suggestions(([old] if include_old else []) + [new]) is True
    persistent = request.node.callspec.params["publication_provider"]
    assert manager.create.call_count == (2 if not include_old or persistent else 3)
    assert manager.list() == []
    assert any("new command" in body for body in p.get_recent_inline_comment_bodies())


def test_successful_batches_on_same_provider_create_each_new_suggestion(publication_provider):
    p = publication_provider
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.publish_code_suggestions([_suggestion(body="new command", improved_code="x = 3")]) is True
    assert p.mr.draft_notes.create.call_count == 2
    assert p.mr.draft_notes.list() == []
    assert len(p.get_recent_inline_comment_bodies()) == 2


@pytest.mark.parametrize("unqueued_first", [False, True])
@pytest.mark.parametrize("recovers", [False, True])
def test_failed_creation_in_failed_batch_is_created_on_retry(publication_provider, unqueued_first, recovers):
    p = publication_provider
    manager = p.mr.draft_notes
    create = manager.create.side_effect
    publish = manager.bulk_publish.side_effect
    reject = True

    def create_or_reject(payload):
        if reject and "unqueued" in payload["note"]:
            raise GitlabCreateError("cannot create draft", response_code=400)
        return create(payload)

    manager.create.side_effect = create_or_reject
    p.mr.discussions.create.side_effect = GitlabCreateError("cannot create live discussion", response_code=400)
    p.mr.notes.create.side_effect = GitlabCreateError("cannot create live note", response_code=400)
    manager.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
    queued = _suggestion(body="queued", improved_code="x = 3")
    unqueued = _suggestion(body="unqueued", suggestion_content="unqueued", improved_code="x = 4")
    assert p.publish_code_suggestions([queued, unqueued]) is False
    assert len(manager.list()) == 1
    assert p.get_recent_inline_comment_bodies() == []
    manager.bulk_publish.side_effect = publish
    reject = not recovers
    order = [unqueued, queued] if unqueued_first else [queued, unqueued]
    for suggestion in order:
        assert p.publish_code_suggestions([suggestion]) is (recovers or suggestion is queued)
    assert manager.list() == []
    bodies = p.get_recent_inline_comment_bodies()
    assert sum(body.startswith("queued") for body in bodies) == 1
    assert sum(body.startswith("unqueued") for body in bodies) == int(recovers)
    assert manager.bulk_publish.call_count == 1 + (2 if not unqueued_first and recovers else 1)


@pytest.mark.parametrize("unqueued_first", [False, True])
def test_retry_publication_failure_keeps_settled_inputs(publication_provider, unqueued_first):
    p = publication_provider
    manager = p.mr.draft_notes
    create = manager.create.side_effect
    publish = manager.bulk_publish.side_effect
    reject = True

    def create_or_reject(payload):
        if reject and "unqueued" in payload["note"]:
            raise GitlabCreateError("cannot create draft", response_code=400)
        return create(payload)

    manager.create.side_effect = create_or_reject
    p.mr.discussions.create.side_effect = GitlabCreateError("cannot create live discussion", response_code=400)
    p.mr.notes.create.side_effect = GitlabCreateError("cannot create live note", response_code=400)
    manager.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
    queued = _suggestion(body="queued", improved_code="x = 3")
    unqueued = _suggestion(body="unqueued", suggestion_content="unqueued", improved_code="x = 4")
    assert p.publish_code_suggestions([queued, unqueued]) is False
    reject = False
    order = [unqueued, queued] if unqueued_first else [queued, unqueued]
    for suggestion in order:
        assert p.publish_code_suggestions([suggestion]) is False
    assert len(manager.list()) == 2
    assert p.get_recent_inline_comment_bodies() == []
    calls = manager.create.call_count
    manager.bulk_publish.side_effect = publish
    for suggestion in order:
        assert p.publish_code_suggestions([suggestion]) is True
    assert manager.create.call_count == calls
    assert manager.list() == []
    assert len(p.get_recent_inline_comment_bodies()) == 2


@pytest.mark.parametrize("invalid", [None, {}, _suggestion(relevant_file="absent.py")])
def test_invalid_new_batch_after_successful_retry_does_not_report_success(publication_provider, invalid):
    p = publication_provider
    publish = p.mr.draft_notes.bulk_publish.side_effect
    p.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
    old = _suggestion()
    assert p.publish_code_suggestions([old]) is False
    p.mr.draft_notes.bulk_publish.side_effect = publish
    assert p.publish_code_suggestions([old]) is True
    calls = p.mr.draft_notes.bulk_publish.call_count
    assert p.publish_code_suggestions([invalid]) is False
    assert p.mr.draft_notes.bulk_publish.call_count == calls
    assert p.mr.draft_notes.create.call_count == 1


def test_live_fallback_survives_unavailable_draft_listing(publication_provider):
    p = publication_provider
    p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported", response_code=404)
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
            raise GitlabCreateError("cannot draft this suggestion", response_code=400)
        return create(payload)

    p.mr.draft_notes.create.side_effect = queue_or_fallback
    p.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
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
        raise GitlabCreateError("cannot create", response_code=400)
    create.side_effect = fail_create
    p.mr.notes.create.side_effect = GitlabCreateError("cannot fallback", response_code=400)
    suggestions = [_suggestion()]
    if partial:
        def fail_or_create(payload):
            if "working" in payload["body"]:
                return MagicMock()
            raise GitlabCreateError("cannot create", response_code=400)
        create.side_effect = fail_or_create
        suggestions.append(_suggestion(body="working"))
    settings = _settings()
    try:
        assert p.publish_code_suggestions(suggestions) is False
        assert p._gitlab_suggestion_publication_incomplete is partial
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
            raise GitlabCreateError("anchor rejected", response_code=400)
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
    position = {"base_sha": "base", "start_sha": "start", "head_sha": "head"}
    draft = p.mr.draft_notes.create({"note": "", "position": position})
    p._gitlab_pending_drafts = [draft]
    p._gitlab_pending_draft_refs = dict(position)
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.list() == []
    assert len(p.get_recent_inline_comment_bodies()) == 1
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


@pytest.mark.parametrize("failure", ["bulk", "list", "drafts-unsupported"])
@pytest.mark.parametrize("count", [1, 2])
def test_improve_caller_stops_unconfirmed_draft_batch_without_summary(publication_provider, failure, count):
    p = publication_provider
    if failure == "drafts-unsupported":
        p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported", response_code=404)
        p.mr.draft_notes.list.side_effect = RequestException("cannot list")
    else:
        operation = p.mr.draft_notes.bulk_publish if failure == "bulk" else p.mr.draft_notes.list
        original = operation.side_effect
        failed = False

        def fail_once_after_queue(*args, **kwargs):
            nonlocal failed
            if p.mr.draft_notes.create.called and not failed:
                failed = True
                raise GitlabCreateError("definitive publish rejection", response_code=400)
            return original(*args, **kwargs)

        operation.side_effect = fail_once_after_queue
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = p
    tool.progress_response = None
    tool._validate_suggestion = lambda *args: (True, "", True)
    tool.dedent_code = lambda filename, line, code: code
    tool._validate_python_replacement_syntax = lambda *args: True
    suggestions = [_suggestion()]
    if count == 2:
        suggestions.append(_suggestion(suggestion_content="second", improved_code="x = 3"))
    asyncio.run(tool.push_inline_code_suggestions({"code_suggestions": suggestions},
                                                include_coverage_footer=False))
    p.mr.notes.create.assert_not_called()  # no duplicate summary on top of delivered comments
    if failure == "drafts-unsupported":
        assert tool._output_published is True
        assert p.get_recent_inline_comment_bodies()
        assert p.mr.discussions.create.call_count == count
        p.mr.draft_notes.bulk_publish.assert_not_called()
    else:
        assert getattr(tool, "_output_published", False) is False
        assert p.get_recent_inline_comment_bodies() == []
        assert len(p.mr.draft_notes.list()) == count
        assert p.mr.draft_notes.create.call_count == count


@pytest.mark.parametrize("unqueued_first", [False, True])
@pytest.mark.parametrize("recovers", [False, True])
def test_improve_caller_preserves_unsettled_suggestion_for_manual_retry(
    publication_provider, unqueued_first, recovers
):
    p = publication_provider
    manager = p.mr.draft_notes
    create = manager.create.side_effect
    publish = manager.bulk_publish.side_effect
    reject = True
    failed = False

    def create_or_reject(payload):
        if reject and "unqueued" in payload["note"]:
            raise GitlabCreateError("cannot create draft", response_code=400)
        return create(payload)

    def fail_first_publish(*args, **kwargs):
        nonlocal failed, reject
        if not failed:
            failed = True
            reject = not recovers
            raise GitlabCreateError("bulk rejected", response_code=400)
        return publish(*args, **kwargs)

    manager.create.side_effect = create_or_reject
    manager.bulk_publish.side_effect = fail_first_publish
    p.mr.discussions.create.side_effect = GitlabCreateError("cannot create live discussion", response_code=400)
    p.mr.notes.create.side_effect = GitlabCreateError("cannot create live note", response_code=400)
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = p
    tool.progress_response = None
    tool._validate_suggestion = lambda *args: (True, "", True)
    tool.dedent_code = lambda filename, line, code: code
    tool._validate_python_replacement_syntax = lambda *args: True
    queued = _suggestion(body="queued", suggestion_content="queued", improved_code="x = 3")
    unqueued = _suggestion(body="unqueued", suggestion_content="unqueued", improved_code="x = 4")
    suggestions = [unqueued, queued] if unqueued_first else [queued, unqueued]
    asyncio.run(tool.push_inline_code_suggestions({"code_suggestions": suggestions},
                                                include_coverage_footer=False))
    assert getattr(tool, "_output_published", False) is False
    assert len(manager.list()) == 1
    assert p.get_recent_inline_comment_bodies() == []
    assert p.publish_code_suggestions([queued]) is True
    assert p.publish_code_suggestions([unqueued]) is recovers
    bodies = p.get_recent_inline_comment_bodies()
    assert sum(body.startswith("queued") for body in bodies) == 1
    assert sum(body.startswith("unqueued") for body in bodies) == int(recovers)
    p.mr.notes.create.assert_called()  # initial unqueued suggestion exhausted live fallback too


def test_cached_provider_publishes_new_batch_without_retrying_previous_failure(publication_provider):
    p = publication_provider
    settings = MagicMock()
    settings.config.git_provider = "gitlab"
    settings.get.return_value = None
    factory = MagicMock(return_value=p)
    publish = p.mr.draft_notes.bulk_publish.side_effect
    with patch.object(providers, "get_settings", return_value=settings), \
            patch.dict(providers._GIT_PROVIDERS, {"gitlab": factory}), \
            request_cycle_context({"settings": {"present": True}}):
        first = providers.get_git_provider_with_context("https://gitlab.test/mr/1")
        p.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError("bulk rejected", response_code=400)
        assert first.publish_code_suggestions([_suggestion()]) is False
        second = providers.get_git_provider_with_context("https://gitlab.test/mr/1")
        assert second is first
        factory.assert_called_once()
        p.mr.draft_notes.bulk_publish.side_effect = publish
        assert second.publish_code_suggestions([_suggestion(body="improve batch", improved_code="x = 3")]) is True
    assert p.mr.draft_notes.create.call_count == 2
    assert p.mr.draft_notes.list() == []
    assert any("improve batch" in body for body in p.get_recent_inline_comment_bodies())


@pytest.mark.parametrize("as_review", [False, True])
def test_mid_batch_revision_change_never_reports_full_publication(as_review):
    provider = _gl_provider()
    provider.publish_comment = MagicMock()
    provider._notice_policy_snapshot_required = False
    attempts = 0

    def versioned_diff(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            provider._gitlab_suggestion_version_changed = True
            return None
        return _FakeDiff()

    provider.get_relevant_diff.side_effect = versioned_diff
    settings = _settings(as_review=as_review)
    try:
        assert provider.publish_code_suggestions([_suggestion(), _suggestion()]) is False
    finally:
        settings.stop()

    assert provider._gitlab_suggestion_version_changed is True
    provider.publish_comment.assert_called_once()
    assert "original command" in provider.publish_comment.call_args.args[0]
    if as_review:
        provider.mr.draft_notes.bulk_publish.assert_not_called()
        provider._test_created_drafts[0].delete.assert_called_once()
    else:
        assert provider.mr.discussions.create.call_count == 1


def test_partial_gitlab_inline_publication_sets_an_incomplete_result():
    provider = _gl_provider()
    provider.send_inline_comment = MagicMock(side_effect=[True, False])
    settings = _settings(as_review=False)
    try:
        assert provider.publish_code_suggestions([_suggestion(), _suggestion()]) is False
    finally:
        settings.stop()

    assert provider._gitlab_suggestion_publication_incomplete is True
    assert provider.send_inline_comment.call_count == 2


@pytest.mark.parametrize("reason", ["stale", "partial"])
def test_improve_does_not_retry_or_summarize_incomplete_gitlab_publication(reason):
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    provider.publish_code_suggestions.return_value = False
    provider._gitlab_suggestion_version_changed = reason == "stale"
    provider._gitlab_suggestion_publication_incomplete = reason == "partial"

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    tool._output_published = False
    tool._validate_suggestion = MagicMock(return_value=(True, "", True))
    tool.dedent_code = MagicMock(side_effect=lambda _, __, text: text)
    tool._validate_python_replacement_syntax = MagicMock(return_value=True)
    tool.generate_summarized_suggestions = MagicMock(return_value="stale summary")

    asyncio.run(tool.push_inline_code_suggestions(
        {"code_suggestions": [_suggestion()]}, include_coverage_footer=False
    ))

    provider.publish_code_suggestions.assert_called_once()
    provider.publish_comment.assert_not_called()
    tool.generate_summarized_suggestions.assert_not_called()
    assert tool._output_published is False


def test_gitlab_command_reset_clears_stale_notice_deduplication():
    provider = _gl_provider()
    _prepare_verified_command_reset(provider)
    provider._gitlab_suggestion_version_notice_sent = True
    provider._gitlab_suggestion_version_changed = True
    provider._gitlab_suggestion_publication_incomplete = True

    provider.reset_diff_cache_for_command()

    assert provider._gitlab_suggestion_version_notice_sent is False
    assert provider._gitlab_suggestion_version_changed is False
    assert provider._gitlab_suggestion_publication_incomplete is False


def test_stale_draft_cleanup_releases_deleted_fingerprints_for_next_command():
    provider = _gl_provider()
    provider.publish_comment = MagicMock()
    original = _suggestion()
    next_suggestion = _suggestion(body="distinct later suggestion", relevant_lines_start=3,
                                  relevant_lines_end=3, improved_code="x = 3")
    calls = 0

    def invalidate_second_suggestion(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            provider._gitlab_suggestion_version_changed = True
            return None
        return _FakeDiff()

    provider.get_relevant_diff.side_effect = invalidate_second_suggestion
    settings = _settings(as_review=True, persistent_inline_comments=True)
    try:
        # The first draft is created, then the MR advances before the next suggestion.
        assert provider.publish_code_suggestions([original, next_suggestion]) is False
        draft = provider._test_created_drafts[0]
        fingerprints = dedup.marker_fingerprints(draft.note)
        assert fingerprints
        assert provider.mr.draft_notes.create.call_count == 1
        assert provider.mr.draft_notes.list() == []
        assert all(not provider._inline_comment_store.seen(fp) for fp in fingerprints)
        provider.get_relevant_diff.side_effect = lambda *_args: _FakeDiff()
        # The same provider must create a new draft, not dedupe a deleted one.
        assert provider.publish_code_suggestions([original]) is True
        assert provider.mr.draft_notes.create.call_count == 2
        assert provider.mr.draft_notes.list() == []
    finally:
        settings.stop()


def test_failed_stale_draft_deletion_blocks_next_command_until_cleanup_recovers():
    provider = _gl_provider()
    provider.publish_comment = MagicMock()
    created = provider.mr.draft_notes.create.side_effect
    first_draft = []

    def failing_draft(payload):
        draft = created(payload)
        first_draft.append((draft, draft.delete.side_effect))
        draft.delete.side_effect = RequestException("temporary deletion failure")
        return draft

    provider.mr.draft_notes.create.side_effect = failing_draft
    attempts = 0

    def stale_after_first(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            provider._gitlab_suggestion_version_changed = True
            return None
        return _FakeDiff()

    provider.get_relevant_diff.side_effect = stale_after_first
    settings = _settings(as_review=True, persistent_inline_comments=True)
    suggestion = _suggestion()
    second = _suggestion(body="second finding", improved_code="y = 2",
                         relevant_lines_start=3, relevant_lines_end=3)
    try:
        assert provider.publish_code_suggestions([suggestion, second]) is False
        assert len(provider._gitlab_stale_suggestion_drafts) == 1
        provider.get_relevant_diff.side_effect = lambda *_args: _FakeDiff()
        # No later command is allowed to bulk-publish the obsolete pending draft.
        assert provider.publish_code_suggestions([suggestion]) is False
        provider.mr.draft_notes.bulk_publish.assert_not_called()
        assert provider.mr.draft_notes.create.call_count == 1

        draft, original_delete = first_draft[0]
        draft.delete.side_effect = original_delete
        assert provider.publish_code_suggestions([suggestion]) is True
        assert provider._gitlab_stale_suggestion_drafts == []
        assert provider.mr.draft_notes.create.call_count == 2
    finally:
        settings.stop()


def test_all_gitlab_inline_failures_keep_existing_summary_recovery():
    provider = _gl_provider()
    provider.send_inline_comment = MagicMock(return_value=False)
    settings = _settings(as_review=False)
    try:
        assert provider.publish_code_suggestions([_suggestion(), _suggestion()]) is False
    finally:
        settings.stop()
    assert provider._gitlab_suggestion_version_changed is False
    assert provider._gitlab_suggestion_publication_incomplete is False
    assert provider.send_inline_comment.call_count == 2


def test_partial_gitlab_publication_suppresses_unverified_fallback_and_marks_failure():
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    provider.publish_code_suggestions.return_value = False
    provider._gitlab_suggestion_version_changed = False
    provider._gitlab_suggestion_publication_incomplete = True

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    tool._output_published = False
    tool._get_suggestions_coverage_footer = MagicMock(return_value="coverage")
    tool._validate_suggestion = MagicMock(side_effect=[
        (True, "", True),
        (False, "line outside reviewed diff", False),
    ])
    tool.dedent_code = MagicMock(side_effect=lambda _, __, body: body)
    tool._validate_python_replacement_syntax = MagicMock(return_value=True)
    tool.generate_summarized_suggestions = MagicMock()

    with patch("pr_agent.tools.pr_code_suggestions.record_command_failure") as record_failure:
        asyncio.run(tool.push_inline_code_suggestions(
            {"code_suggestions": [_suggestion(), _suggestion(relevant_file="outside.py")]},
            include_coverage_footer=True,
        ))

    provider.publish_code_suggestions.assert_called_once()
    record_failure.assert_called_once()
    provider.publish_comment.assert_not_called()
    tool.generate_summarized_suggestions.assert_not_called()
    assert tool._output_published is False


@pytest.mark.parametrize("policy_verified", [False, True])
def test_improve_reports_incomplete_publication_only_with_safe_notice_policy(policy_verified):
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    provider.publish_code_suggestions.return_value = False
    provider._gitlab_suggestion_version_changed = False
    provider._gitlab_suggestion_publication_incomplete = True
    provider._notice_policy_snapshot_required = not policy_verified

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    tool._output_published = False
    tool._validate_suggestion = MagicMock(return_value=(True, "", True))
    tool._validate_python_replacement_syntax = MagicMock(return_value=True)
    tool.dedent_code = MagicMock(side_effect=lambda _, __, code: code)

    with patch("pr_agent.tools.pr_code_suggestions.record_command_failure") as record_failure:
        asyncio.run(tool.push_inline_code_suggestions(
            {"code_suggestions": [_suggestion()]}, include_coverage_footer=False,
        ))

    record_failure.assert_called_once()
    assert provider.publish_tool_failure.call_count == int(policy_verified)
    provider.publish_comment.assert_not_called()


def test_standalone_gitlab_draft_creation_initializes_tracking():
    provider = _gl_provider()
    assert not hasattr(provider, "_gitlab_pending_drafts")
    assert provider._create_suggestion_note(
        True, "draft", {"position_type": "text"}, _FakeDiff(),
        _FakeTargetFile(), "a.py", _suggestion(), None, None, None,
    ) is True
    assert len(provider._gitlab_pending_drafts) == 1


@pytest.mark.parametrize("stale_call", [2, 3])
def test_improve_stops_retries_and_summary_if_revision_moves_mid_retry(stale_call):
    provider = MagicMock()
    provider.supports_code_suggestions_artifact.return_value = False
    provider._gitlab_suggestion_version_changed = False
    provider._gitlab_suggestion_publication_incomplete = False
    number_of_calls = 0

    def publish(suggestions):
        nonlocal number_of_calls
        number_of_calls += 1
        if number_of_calls == 1:
            return False  # Initial batch failed without landing suggestions.
        if number_of_calls == stale_call:
            provider._gitlab_suggestion_version_changed = True
            return False
        return True

    provider.publish_code_suggestions.side_effect = publish
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    tool._output_published = False
    tool._validate_suggestion = MagicMock(return_value=(True, "", True))
    tool.dedent_code = MagicMock(side_effect=lambda _, __, content: content)
    tool._validate_python_replacement_syntax = MagicMock(return_value=True)
    tool.generate_summarized_suggestions = MagicMock(return_value="stale summary")

    with patch("pr_agent.tools.pr_code_suggestions.record_command_failure") as record_failure:
        asyncio.run(tool.push_inline_code_suggestions({
            "code_suggestions": [
                _suggestion(),
                _suggestion(body="Second", improved_code="x = 3"),
                _suggestion(body="Third", improved_code="x = 4"),
            ],
        }, include_coverage_footer=False))

    record_failure.assert_called_once()
    assert provider.publish_code_suggestions.call_count == stale_call
    provider.publish_comment.assert_not_called()
    tool.generate_summarized_suggestions.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["initial", "retry"])
def test_gitlab_draft_publish_never_releases_old_revision(failure_stage):
    provider = _gl_provider()
    settings = _settings(as_review=True)
    suggestion = _suggestion()
    original_publish = provider.mr.draft_notes.bulk_publish.side_effect
    try:
        if failure_stage == "retry":
            provider.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError(
                "bulk rejected", response_code=400
            )
            assert provider.publish_code_suggestions([suggestion]) is False
            assert provider.mr.draft_notes.list()
            assert provider.mr.draft_notes.bulk_publish.call_count == 1
            provider.mr.draft_notes.bulk_publish.side_effect = original_publish
        else:
            # A push happens after the notes are queued but before bulk publication.
            original_create = provider.mr.draft_notes.create.side_effect

            def move_head_after_draft(payload):
                draft = original_create(payload)
                provider._get_merge_request.return_value.diff_refs["head_sha"] = "new-head"
                provider._get_merge_request.return_value.sha = "new-head"
                return draft

            provider.mr.draft_notes.create.side_effect = move_head_after_draft

        if failure_stage == "retry":
            provider._get_merge_request.return_value.diff_refs["head_sha"] = "new-head"
            provider._get_merge_request.return_value.sha = "new-head"
        assert provider.publish_code_suggestions([suggestion]) is False
        assert provider._gitlab_suggestion_version_changed is True
        assert provider._gitlab_pending_draft_refs is None
        assert provider.mr.draft_notes.list() == []
        assert provider.mr.draft_notes.bulk_publish.call_count == (
            1 if failure_stage == "retry" else 0
        )
    finally:
        settings.stop()


def test_gitlab_mid_batch_version_change_discards_drafts_from_prior_failed_batch():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    pending_publish = provider.mr.draft_notes.bulk_publish.side_effect
    old = _suggestion()
    new = _suggestion(body="new finding", improved_code="y = 3")
    try:
        provider.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError(
            "bulk rejected", response_code=400
        )
        assert provider.publish_code_suggestions([old]) is False
        assert len(provider.mr.draft_notes.list()) == 1

        # The new suggestion uses a different reviewed MR version, but the
        # previous command still has an unpublished draft on the same provider.
        provider.get_relevant_diff.return_value = SimpleNamespace(
            base_commit_sha="base", start_commit_sha="start", head_commit_sha="head-b",
        )
        provider.mr.draft_notes.bulk_publish.side_effect = pending_publish
        assert provider.publish_code_suggestions([new]) is False

        assert provider._gitlab_suggestion_version_changed is True
        assert provider.mr.draft_notes.list() == []
        assert provider._gitlab_pending_draft_refs is None
        provider.mr.draft_notes.bulk_publish.assert_called_once()
    finally:
        settings.stop()


def test_gitlab_empty_draft_listing_is_not_mistaken_for_success():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    original_list = provider.mr.draft_notes.list.side_effect
    try:
        provider.mr.draft_notes.list.side_effect = lambda **_kwargs: []
        with pytest.raises(GitLabSuggestionPublicationError):
            provider.publish_code_suggestions([_suggestion()])
        assert len(provider._gitlab_pending_drafts) == 1
        provider.mr.draft_notes.bulk_publish.assert_not_called()

        provider.mr.draft_notes.list.side_effect = original_list
        assert provider.publish_code_suggestions([_suggestion()]) is True
        provider.mr.draft_notes.bulk_publish.assert_called_once()
        assert provider.mr.draft_notes.list() == []
    finally:
        settings.stop()


@pytest.mark.parametrize("as_review", [False, True], ids=["live", "draft"])
@pytest.mark.parametrize("failure", ["http_503", "transport", "no_confirmation"])
def test_unconfirmed_gitlab_suggestion_create_never_tries_another_publish_path(as_review, failure):
    provider = _gl_provider()
    write = provider.mr.draft_notes.create if as_review else provider.mr.discussions.create
    if failure == "http_503":
        write.side_effect = GitlabCreateError("service unavailable", response_code=503)
    elif failure == "transport":
        write.side_effect = RequestException("connection lost after POST")
    else:
        write.side_effect = None
        write.return_value = None
    settings = _settings(as_review=as_review)
    try:
        with pytest.raises(GitLabSuggestionPublicationError):
            provider.publish_code_suggestions([_suggestion()])
    finally:
        settings.stop()

    assert provider.get_recent_inline_comment_bodies() == []
    provider.mr.notes.create.assert_not_called()
    provider.mr.draft_notes.bulk_publish.assert_not_called()
    assert write.call_count == 1


@pytest.mark.parametrize("failure", ["timeout", "missing_diff_refs", "missing_diff_refs_with_live_sha"])
def test_pending_drafts_survive_unverifiable_mr_and_recover_on_retry(failure):
    provider = _gl_provider()
    settings = _settings(as_review=True)
    original_bulk = provider.mr.draft_notes.bulk_publish.side_effect
    suggestion = _suggestion()
    try:
        provider.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError(
            "bulk rejected", response_code=400
        )
        assert provider.publish_code_suggestions([suggestion]) is False
        provider.mr.draft_notes.bulk_publish.side_effect = original_bulk
        original_mr = provider._get_merge_request.return_value
        if failure == "timeout":
            provider._get_merge_request.side_effect = RequestException("GitLab unavailable")
        else:
            # GitLab can report a valid live SHA while its diff refs are not
            # ready yet; that is still insufficient to publish queued drafts.
            provider._get_merge_request.return_value = SimpleNamespace(
                diff_refs={}, sha="head" if failure == "missing_diff_refs_with_live_sha" else None
            )
        with pytest.raises(IncompleteGitLabDiffError):
            provider.publish_code_suggestions([suggestion])
        assert provider._gitlab_suggestion_version_changed is False
        assert provider._gitlab_pending_draft_refs is not None
        assert len(provider.mr.draft_notes.list()) == 1
        provider.mr.draft_notes.create.assert_called_once()
        provider.mr.draft_notes.bulk_publish.assert_called_once()
        assert not getattr(provider, "_gitlab_stale_suggestion_drafts", [])

        provider._get_merge_request.side_effect = None
        provider._get_merge_request.return_value = original_mr
        assert provider.publish_code_suggestions([suggestion]) is True
        assert provider.mr.draft_notes.list() == []
        assert provider.mr.draft_notes.create.call_count == 1
        assert provider.mr.draft_notes.bulk_publish.call_count == 2
    finally:
        settings.stop()


def test_draft_mismatch_does_not_fall_back_to_live_gitlab_note():
    provider = _gl_provider()
    provider._gitlab_pending_draft_refs = {
        "head_sha": "old-head", "base_sha": "base", "start_sha": "start",
    }
    settings = _settings(as_review=True)
    try:
        assert provider.send_inline_comment(
            "**Suggestion:** changed", "addition", True, "a.py", "line2",
            2, _FakeTargetFile(), 2, _suggestion(), as_draft=True
        ) is False
    finally:
        settings.stop()

    assert provider._gitlab_suggestion_version_changed is True
    provider.mr.draft_notes.create.assert_not_called()
    provider.mr.discussions.create.assert_not_called()
    provider.mr.notes.create.assert_not_called()


def test_untracked_drafts_from_prior_provider_cannot_be_bulk_published():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    try:
        old = provider.mr.draft_notes.create({
            "note": "obsolete draft",
            "position": {
                "base_sha": "base", "start_sha": "start", "head_sha": "old-head",
            },
        })
        assert provider.publish_code_suggestions([_suggestion()]) is True
        assert provider._gitlab_suggestion_version_changed is False
        provider.mr.draft_notes.bulk_publish.assert_not_called()
        assert old in provider.mr.draft_notes.list()
        old.delete.assert_not_called()
    finally:
        settings.stop()


def test_untracked_draft_without_position_prevents_bulk_publish():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    try:
        manual = provider.mr.draft_notes.create({"note": "unknown origin"})
        assert provider.publish_code_suggestions([_suggestion()]) is True
        assert provider._gitlab_suggestion_version_changed is False
        provider.mr.draft_notes.bulk_publish.assert_not_called()
        assert provider.mr.draft_notes.list() == [manual]
        manual.publish.assert_not_called()
    finally:
        settings.stop()


def test_stale_draft_cleanup_clears_failed_batch_retry_bookkeeping():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    old = _suggestion()
    original_publish = provider.mr.draft_notes.bulk_publish.side_effect
    try:
        provider.mr.draft_notes.bulk_publish.side_effect = GitlabCreateError(
            "bulk rejected", response_code=400
        )
        assert provider.publish_code_suggestions([old]) is False
        assert provider._failed_draft_inputs == [old]
        assert provider._failed_draft_settled == [old]

        provider.mr.draft_notes.bulk_publish.side_effect = original_publish
        provider._get_merge_request.return_value.diff_refs["head_sha"] = "new-head"
        provider._get_merge_request.return_value.sha = "new-head"
        assert provider.publish_code_suggestions([old]) is False
        assert provider.mr.draft_notes.list() == []
        assert provider._failed_draft_batch is None
        assert provider._failed_draft_inputs == []
        assert provider._failed_draft_settled == []

        provider.get_relevant_diff.return_value = SimpleNamespace(
            base_commit_sha="base", start_commit_sha="start", head_commit_sha="new-head",
        )
        assert provider.publish_code_suggestions([old]) is True
        assert provider.mr.draft_notes.create.call_count == 2
    finally:
        settings.stop()


def test_changed_gitlab_mr_cannot_publish_notice_after_cli_weakens_policy():
    provider = _gl_provider()
    provider.publish_comment = MagicMock()
    provider._notice_policy_snapshot_required = True
    calls = 0

    def changing_version(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            provider._gitlab_suggestion_version_changed = True
            return None
        return _FakeDiff()

    provider.get_relevant_diff.side_effect = changing_version
    settings = _settings(as_review=False)
    try:
        assert provider.publish_code_suggestions([_suggestion(), _suggestion()]) is False
    finally:
        settings.stop()
    provider.publish_comment.assert_not_called()


def test_gitlab_new_drafts_use_tracked_ids_without_public_ownership_marker():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([_suggestion()]) is True
    finally:
        settings.stop()
    body = provider.mr.draft_notes.create.call_args.args[0]["note"]
    assert "<!-- pr-agent:gitlab-draft -->" not in body


def test_gitlab_cross_instance_stale_agent_cleanup_preserves_manual_drafts():
    provider = _gl_provider()
    settings = _settings(as_review=True)
    old_position = {"base_sha": "base", "start_sha": "start", "head_sha": "old"}
    try:
        # The manual note comes first to verify that discovery order does not
        # prevent detecting and clearing an obsolete agent-owned draft.
        manual = provider.mr.draft_notes.create({
            "note": "human draft", "position": old_position,
        })
        stale = provider.mr.draft_notes.create({
            "note": "previous PR-Agent suggestion\n<!-- pr-agent:gitlab-draft -->",
            "position": old_position,
        })
        assert provider.publish_code_suggestions([_suggestion()]) is True
        pending = provider.mr.draft_notes.list()
        # The marker is public/copyable; only publish this run's drafts.
        assert pending == [manual, stale]
        stale.delete.assert_not_called()
        manual.delete.assert_not_called()
        provider.mr.draft_notes.bulk_publish.assert_not_called()
    finally:
        settings.stop()


def test_gitlab_stale_cleanup_uses_draft_ids_and_preserves_ambiguous_404():
    from gitlab import GitlabGetError

    provider = _gl_provider()
    first = MagicMock(id=314)
    first.note = "obsolete draft"
    second = MagicMock(id=314)
    second.note = "obsolete draft"
    first.delete.side_effect = GitlabGetError(response_code=404)
    provider._gitlab_stale_suggestion_drafts = [first, second]

    assert provider._discard_stale_suggestion_drafts() is False
    first.delete.assert_called_once()
    second.delete.assert_not_called()
    assert provider._gitlab_stale_suggestion_drafts == [first]


def test_selective_draft_publication_does_not_touch_manual_drafts():
    provider = _gl_provider()
    manual = provider.mr.draft_notes.create({"note": "manual draft"})
    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([_suggestion()]) is True
    finally:
        settings.stop()

    manual.publish.assert_not_called()
    manual.delete.assert_not_called()
    provider.mr.draft_notes.bulk_publish.assert_not_called()
    assert provider.mr.draft_notes.list() == [manual]


def test_selective_draft_publication_stops_on_confirmed_revision_change():
    provider = _gl_provider()
    manual = provider.mr.draft_notes.create({"note": "manual note"})
    original_create = provider.mr.draft_notes.create.side_effect

    def advance_after_creation(payload):
        draft = original_create(payload)
        if payload.get("position"):
            provider._get_merge_request.return_value.diff_refs["head_sha"] = "new-head"
            provider._get_merge_request.return_value.sha = "new-head"
        return draft

    provider.mr.draft_notes.create.side_effect = advance_after_creation
    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([_suggestion()]) is False
    finally:
        settings.stop()
    assert provider._gitlab_suggestion_version_changed is True
    manual.delete.assert_not_called()
    manual.publish.assert_not_called()
    provider.mr.draft_notes.bulk_publish.assert_not_called()
    assert provider.mr.draft_notes.list() == [manual]


def test_partial_selective_publish_failure_retains_failure_until_retry():
    provider = _gl_provider()
    manual = provider.mr.draft_notes.create({"note": "manual draft"})
    create = provider.mr.draft_notes.create.side_effect
    fail_second = True

    def create_with_partial_failure(payload):
        draft = create(payload)
        if "second" in payload["note"]:
            publish = draft.publish.side_effect

            def intermittent_failure():
                if fail_second:
                    raise GitlabCreateError("draft publish rejected", response_code=400)
                return publish()

            draft.publish.side_effect = intermittent_failure
        return draft

    provider.mr.draft_notes.create.side_effect = create_with_partial_failure
    first = _suggestion(body="first suggestion")
    second = _suggestion(body="second suggestion", improved_code="x = 3")
    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([first, second]) is False
        assert provider._gitlab_suggestion_publication_incomplete is True
        assert provider._gitlab_selective_draft_landed is True
        assert len(provider.mr.draft_notes.list()) == 2  # manual + unsuccessful second
        assert len(provider.get_recent_inline_comment_bodies()) == 1
        fail_second = False
        assert provider.publish_code_suggestions([first, second]) is True
        assert provider._gitlab_selective_draft_landed is False
        assert provider.mr.draft_notes.list() == [manual]
        assert len(provider.get_recent_inline_comment_bodies()) == 2
    finally:
        settings.stop()
    provider.mr.draft_notes.bulk_publish.assert_not_called()


def test_next_command_discards_unpublished_drafts_from_a_partial_selective_batch():
    provider = _gl_provider()
    _prepare_verified_command_reset(provider)
    manual = provider.mr.draft_notes.create({"note": "manual draft"})
    create = provider.mr.draft_notes.create.side_effect

    def create_with_failed_second_publish(payload):
        draft = create(payload)
        if "second suggestion" in payload["note"]:
            draft.publish.side_effect = GitlabCreateError("rejected", response_code=400)
        return draft

    provider.mr.draft_notes.create.side_effect = create_with_failed_second_publish
    first = _suggestion(body="first suggestion")
    second = _suggestion(body="second suggestion", improved_code="x = 3")
    next_command = _suggestion(body="new command suggestion", improved_code="x = 4")
    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([first, second]) is False
        old_draft = provider._gitlab_pending_drafts[0]
        assert provider._gitlab_selective_draft_landed is True
        assert provider._gitlab_suggestion_publication_incomplete is True

        provider.reset_diff_cache_for_command()
        assert provider._gitlab_selective_draft_landed is False
        assert provider._failed_draft_batch is None
        assert old_draft in provider._gitlab_stale_suggestion_drafts
        assert provider.publish_code_suggestions([next_command]) is True
    finally:
        settings.stop()

    old_draft.delete.assert_called_once()
    old_draft.publish.assert_called_once()  # Initial failed attempt; never retried by the new command.
    assert provider.mr.draft_notes.list() == [manual]
    assert len(provider.get_recent_inline_comment_bodies()) == 2  # First command's landed note and new note.


def test_current_gitlab_command_stops_if_owned_draft_deletion_is_ambiguous():
    from gitlab import GitlabGetError

    provider = _gl_provider()
    _prepare_verified_command_reset(provider)
    old_draft = provider.mr.draft_notes.create({"note": "previous partial draft"})
    provider._gitlab_pending_drafts = [old_draft]
    provider._gitlab_stale_suggestion_drafts = [old_draft]
    provider._gitlab_selective_draft_landed = True
    old_draft.delete.side_effect = GitlabGetError(response_code=404)

    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([_suggestion(body="next command")]) is False
    finally:
        settings.stop()

    assert provider._gitlab_suggestion_publication_incomplete is True
    assert provider._gitlab_stale_suggestion_drafts == [old_draft]
    assert provider.mr.draft_notes.create.call_count == 1
    old_draft.publish.assert_not_called()


@pytest.mark.parametrize("stale_draft_visible_in_listing", [False, True])
def test_ambiguous_old_draft_does_not_block_distinct_new_command_suggestions(
        stale_draft_visible_in_listing):
    from gitlab import GitlabGetError

    provider = _gl_provider()
    _prepare_verified_command_reset(provider)
    fingerprint = "0123456789ab"
    old_draft = provider.mr.draft_notes.create({
        "note": dedup.body_with_markers("previous ambiguous draft", fingerprint, None),
    })
    store = dedup.get_inline_comment_store(provider)
    store.add(fingerprint)
    provider._gitlab_pending_drafts = [old_draft]
    old_draft.delete.side_effect = GitlabGetError(response_code=404)
    provider.reset_diff_cache_for_command()
    if not stale_draft_visible_in_listing:
        # GitLab can omit a deleted/published old draft from the listing. A
        # later bulk_publish must still never operate on an ambiguous state.
        list_drafts = provider.mr.draft_notes.list.side_effect
        provider.mr.draft_notes.list.side_effect = lambda **kwargs: [
            draft for draft in list_drafts(**kwargs) if draft is not old_draft
        ]

    settings = _settings(as_review=True)
    try:
        assert provider.publish_code_suggestions([_suggestion(body="new distinct finding")]) is True
    finally:
        settings.stop()

    # Only the current command's verified draft may be published. A 404 on
    # the old draft never authorizes publishing it or releasing its dedup key.
    if stale_draft_visible_in_listing:
        assert provider.mr.draft_notes.list() == [old_draft]
    else:
        assert provider.mr.draft_notes.list() == []
    assert provider._gitlab_stale_suggestion_drafts == [old_draft]
    assert store.seen(fingerprint)
    assert provider.mr.draft_notes.create.call_count == 2
    old_draft.publish.assert_not_called()
    provider._test_created_drafts[1].publish.assert_called_once()
    provider.mr.draft_notes.bulk_publish.assert_not_called()


def test_selective_gitlab_drafts_recheck_revision_before_each_write():
    provider = _gl_provider()
    first, second = MagicMock(id=101, note="one"), MagicMock(id=102, note="two")
    unrelated = MagicMock(id=103, note="manual")
    provider._gitlab_pending_drafts = [first, second]
    provider._draft_revision_is_current = MagicMock(side_effect=[True, False])
    provider._discard_stale_suggestion_drafts = MagicMock(return_value=True)
    assert provider._publish_suggestion_drafts([first, second, unrelated]) is False
    first.publish.assert_called_once()
    second.publish.assert_not_called()
    assert provider._gitlab_suggestion_version_changed is True
    provider.mr.draft_notes.bulk_publish.assert_not_called()


def test_unknown_gitlab_drafts_are_not_bulk_published_or_deleted():
    provider = _gl_provider()
    provider._gitlab_pending_drafts = []
    foreign = MagicMock(id=103, note="Untracked pending draft")
    assert provider._publish_suggestion_drafts([foreign]) is True
    foreign.publish.assert_not_called()
    foreign.delete.assert_not_called()
    provider.mr.draft_notes.bulk_publish.assert_not_called()


def test_gitlab_draft_cleanup_404_keeps_ambiguous_fingerprint_reserved():
    from gitlab import GitlabGetError

    provider = _gl_provider()
    draft = MagicMock(id=77)
    draft.note = dedup.body_with_markers("suggestion", "0123456789ab", None)
    assert dedup.marker_fingerprints(draft.note)
    draft.delete.side_effect = GitlabGetError(response_code=404)
    provider._gitlab_stale_suggestion_drafts = [draft]
    provider._gitlab_pending_drafts = [draft]
    provider._inline_comment_store = MagicMock()
    assert provider._discard_stale_suggestion_drafts() is False
    provider._inline_comment_store.release.assert_not_called()
    assert provider._gitlab_stale_suggestion_drafts == [draft]
