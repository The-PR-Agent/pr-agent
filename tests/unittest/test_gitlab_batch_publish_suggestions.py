import asyncio
import re
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import gitlab
import pytest
from gitlab import GitlabCreateError, GitlabParsingError
from requests import Response
from requests.exceptions import ConnectionError, ConnectTimeout, InvalidURL, ReadTimeout, RequestException, SSLError
from starlette_context import request_cycle_context
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError
from urllib3.exceptions import ProxyError as Urllib3ProxyError

import pr_agent.git_providers as providers
from pr_agent.algo import inline_comment_dedup as dedup
from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions
from pr_agent.git_providers.gitlab_provider import GitLabProvider


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
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = p
    command.progress_response = None
    suggestion = _suggestion(relevant_lines_start=start, relevant_lines_end=start, existing_code=f'line{start}')
    gs = _settings(as_review=as_review)
    try:
        asyncio.run(command.push_inline_code_suggestions({"code_suggestions": [suggestion]},
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
    p.mr.draft_notes.create.side_effect = GitlabCreateError("draft notes unavailable", response_code=400)
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


@pytest.mark.parametrize("as_review", [False, True])
@pytest.mark.parametrize("error", [
    ReadTimeout("response lost after write"),
    SSLError("connection failed after request was sent"),
    GitlabCreateError("service unavailable", response_code=503),
    GitlabCreateError("request timed out", response_code=408),
    GitlabParsingError("unreadable success body", response_code=201),
])
def test_unconfirmed_suggestion_create_is_not_retried_or_fallen_back(as_review, error):
    p = _gl_provider()
    create = p.mr.draft_notes.create if as_review else p.mr.discussions.create
    create.side_effect = error
    gs = _settings(as_review=as_review, persistent_inline_comments=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    create.assert_called_once()
    p.mr.notes.create.assert_not_called()
    if as_review:
        p.mr.discussions.create.assert_not_called()
        assert p._code_suggestion_draft_queued is True
    else:
        p.mr.draft_notes.create.assert_not_called()
    if as_review:
        assert p.get_recent_inline_comment_bodies() == []
    else:
        [body] = p.get_recent_inline_comment_bodies()
        assert "fix it" in body
        assert "pr-agent-dedup" in body


def test_unconfirmed_general_note_fallback_is_not_retried():
    p = _gl_provider()
    p.mr.discussions.create.side_effect = GitlabCreateError("invalid position", response_code=400)
    p.mr.notes.create.side_effect = GitlabCreateError("server error after write", response_code=503)
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    p.mr.discussions.create.assert_called_once()
    p.mr.notes.create.assert_called_once()
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


@pytest.mark.parametrize("error", [
    ConnectTimeout("connection was not established"),
    InvalidURL("invalid URL before sending"),
    ConnectionError(MaxRetryError(
        HTTPSConnectionPool("gitlab.test"), "/api/v4/merge_requests/1/discussions",
        NewConnectionError(None, "connection refused"))),
    ConnectionError(MaxRetryError(
        HTTPSConnectionPool("gitlab.test"), "/api/v4/merge_requests/1/discussions",
        Urllib3ProxyError("proxy refused connection", NewConnectionError(None, "connection refused")))),
    ConnectionError(MaxRetryError(
        HTTPSConnectionPool("gitlab.test"), "/api/v4/merge_requests/1/discussions",
        ConnectTimeoutError(None, "TLS connection never established"))),
])
def test_pre_write_connection_failure_can_use_general_note_fallback(error):
    p = _gl_provider()
    p.mr.discussions.create.side_effect = error
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    p.mr.discussions.create.assert_called_once()
    p.mr.notes.create.assert_called_once()
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


def test_failure_while_preparing_general_note_does_not_look_like_an_unconfirmed_write():
    p = _gl_provider()
    p.mr.discussions.create.side_effect = GitlabCreateError("invalid position", response_code=400)
    p.get_line_link.side_effect = RequestException("failed before fallback create")
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is False
    finally:
        gs.stop()

    p.mr.notes.create.assert_not_called()
    assert p.get_recent_inline_comment_bodies() == []


def test_accepted_draft_with_lost_response_is_published_only_once():
    p = _gl_provider()
    create = p.mr.draft_notes.create.side_effect

    def accept_then_timeout(payload):
        create(payload)
        raise ReadTimeout("response lost after GitLab queued the draft")

    p.mr.draft_notes.create.side_effect = accept_then_timeout
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_called_once()
    p.mr.draft_notes.bulk_publish.assert_called_once()
    assert p.mr.draft_notes.list() == []
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


def test_unconfirmed_draft_is_not_tracked_as_published_if_bulk_publish_fails():
    p = _gl_provider()
    create = p.mr.draft_notes.create.side_effect

    def accept_then_timeout(payload):
        create(payload)
        raise ReadTimeout("response lost after GitLab queued the draft")

    p.mr.draft_notes.create.side_effect = accept_then_timeout
    p.mr.draft_notes.bulk_publish.side_effect = RequestException("bulk publication unavailable")
    gs = _settings(as_review=True)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is False
    finally:
        gs.stop()

    p.mr.draft_notes.create.assert_called_once()
    assert len(p.mr.draft_notes.list()) == 1
    assert p.get_recent_inline_comment_bodies() == []


def test_real_gitlab_client_invalid_success_response_does_not_retry():
    p = _gl_provider()
    client = gitlab.Gitlab("https://gitlab.test", private_token="test-token", retry_transient_errors=False)
    p.mr = client.projects.get(1, lazy=True).mergerequests.get(1, lazy=True)
    p.resolve_outdated_inline_threads = MagicMock()
    p.reconcile_code_suggestion_threads = MagicMock()
    posted_paths = []

    def send(request, **kwargs):
        response = Response()
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response.status_code = 201
        posted_paths.append(urlparse(request.url).path)
        response._content = b"invalid JSON after accepting write"
        return response

    client.session.send = MagicMock(side_effect=send)
    gs = _settings(as_review=False)
    try:
        assert p.publish_code_suggestions([_suggestion()]) is True
    finally:
        gs.stop()

    assert len(posted_paths) == 1
    assert posted_paths[0].endswith("/discussions")
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


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
    manager.bulk_publish.side_effect = RequestException("temporary publication failure")
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
    manager.bulk_publish.side_effect = RequestException("cannot publish batch")
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
    manager.bulk_publish.side_effect = RequestException("cannot publish batch")
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
    p.mr.draft_notes.bulk_publish.side_effect = RequestException("temporary failure")
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
    p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported", response_code=400)
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
    p.mr.draft_notes.create({"note": ""})
    assert p.publish_code_suggestions([_suggestion()]) is True
    assert p.mr.draft_notes.list() == []
    assert len(p.get_recent_inline_comment_bodies()) == 1
    assert "fix it" in p.get_recent_inline_comment_bodies()[0]


@pytest.mark.parametrize("failure", ["bulk", "list", "drafts-unsupported"])
@pytest.mark.parametrize("count", [1, 2])
def test_improve_caller_retries_queued_batch_but_not_live_fallback(publication_provider, failure, count):
    p = publication_provider
    if failure == "drafts-unsupported":
        p.mr.draft_notes.create.side_effect = GitlabCreateError("drafts unsupported", response_code=400)
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
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = p
    command.progress_response = None
    command._validate_suggestion = lambda *args: (True, "", True)
    command.dedent_code = lambda filename, line, code: code
    command._validate_python_replacement_syntax = lambda *args: True
    suggestions = [_suggestion()]
    if count == 2:
        suggestions.append(_suggestion(suggestion_content="second", improved_code="x = 3"))
    asyncio.run(command.push_inline_code_suggestions({"code_suggestions": suggestions},
                                                include_coverage_footer=False))
    assert command._output_published is True
    p.mr.notes.create.assert_not_called()  # no duplicate summary on top of delivered comments
    assert p.get_recent_inline_comment_bodies()
    if failure == "drafts-unsupported":
        assert p.mr.discussions.create.call_count == count
        p.mr.draft_notes.bulk_publish.assert_not_called()
    else:
        assert p.mr.draft_notes.create.call_count == count
        assert p.mr.draft_notes.list() == []


@pytest.mark.parametrize("unqueued_first", [False, True])
@pytest.mark.parametrize("recovers", [False, True])
def test_improve_caller_retries_unsettled_suggestion(publication_provider, unqueued_first, recovers):
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
            raise RequestException("cannot publish batch")
        return publish(*args, **kwargs)

    manager.create.side_effect = create_or_reject
    manager.bulk_publish.side_effect = fail_first_publish
    p.mr.discussions.create.side_effect = GitlabCreateError("cannot create live discussion", response_code=400)
    p.mr.notes.create.side_effect = GitlabCreateError("cannot create live note", response_code=400)
    command = PRCodeSuggestions.__new__(PRCodeSuggestions)
    command.git_provider = p
    command.progress_response = None
    command._validate_suggestion = lambda *args: (True, "", True)
    command.dedent_code = lambda filename, line, code: code
    command._validate_python_replacement_syntax = lambda *args: True
    queued = _suggestion(suggestion_content="queued", improved_code="x = 3")
    unqueued = _suggestion(suggestion_content="unqueued", improved_code="x = 4")
    suggestions = [unqueued, queued] if unqueued_first else [queued, unqueued]
    asyncio.run(command.push_inline_code_suggestions({"code_suggestions": suggestions},
                                                include_coverage_footer=False))
    assert command._output_published is True
    assert manager.list() == []
    bodies = p.get_recent_inline_comment_bodies()
    assert sum("queued" in body and "unqueued" not in body for body in bodies) == 1
    assert sum("unqueued" in body for body in bodies) == int(recovers)
    # Preserve the caller's existing partial-success policy, not a per-input summary guarantee.
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
        p.mr.draft_notes.bulk_publish.side_effect = RequestException("review publication failed")
        assert first.publish_code_suggestions([_suggestion()]) is False
        second = providers.get_git_provider_with_context("https://gitlab.test/mr/1")
        assert second is first
        factory.assert_called_once()
        p.mr.draft_notes.bulk_publish.side_effect = publish
        assert second.publish_code_suggestions([_suggestion(body="improve batch", improved_code="x = 3")]) is True
    assert p.mr.draft_notes.create.call_count == 2
    assert p.mr.draft_notes.list() == []
    assert any("improve batch" in body for body in p.get_recent_inline_comment_bodies())
