"""
Tests for GitHub provider inline comment creation, publishing fallback,
and multi-line code suggestion payload shape.

These tests use ``GithubProvider.__new__(GithubProvider)`` to bypass network-bound
``__init__`` and inject minimal fake collaborators. No real GitHub API access.
"""

import json
from types import SimpleNamespace

import pytest

from pr_agent.git_providers import github_provider as gh_module
from pr_agent.git_providers.github_provider import GithubProvider


class _FakeGithubException(Exception):
    """Mimics github.GithubException enough for the provider's ``e.status`` check."""

    def __init__(self, status, data=None):
        super().__init__(f"GithubException status={status}")
        self.status = status
        self.data = data or {}


class _FakePR:
    """Captures create_review calls; can be configured to raise on the first call."""

    def __init__(self, raise_on_first=None):
        self.create_review_calls = []
        self._raise_on_first = raise_on_first
        self._calls = 0

    def create_review(self, commit=None, comments=None):
        self._calls += 1
        self.create_review_calls.append({"commit": commit, "comments": comments})
        if self._raise_on_first is not None and self._calls == 1:
            exc = self._raise_on_first
            self._raise_on_first = None
            raise exc
        return SimpleNamespace(id=1)


def _make_provider(pr=None, max_chars=65000):
    p = GithubProvider.__new__(GithubProvider)
    p.pr = pr if pr is not None else _FakePR()
    p.repo = "owner/repo"
    p.pr_num = 1
    p.max_comment_chars = max_chars
    p.last_commit_id = SimpleNamespace(sha="deadbeef")
    p.diff_files = []
    p.base_url = "https://api.github.com"
    return p


# ---------------------------------------------------------------------------
# create_inline_comment
# ---------------------------------------------------------------------------

def test_create_inline_comment_returns_line_payload(monkeypatch):
    """When a position is resolved, payload must include body/path/position."""
    provider = _make_provider()

    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda diff_files, rel_file, rel_line, abs_pos: (5, 42),
    )

    payload = provider.create_inline_comment("LGTM", "src/foo.py", "x = 1")

    assert payload == {"body": "LGTM", "path": "src/foo.py", "position": 5}


def test_create_inline_comment_returns_empty_when_position_unresolved(monkeypatch):
    """If no position can be resolved (position == -1) current behavior returns {}."""
    provider = _make_provider()

    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda *a, **kw: (-1, -1),
    )

    payload = provider.create_inline_comment("body", "src/foo.py", "x = 1")
    assert payload == {}


def test_create_inline_comment_lookup_strips_backticks_but_payload_preserves_them(monkeypatch):
    """Backtick handling is asymmetric in current production code.

    ``find_line_number_of_relevant_line_in_file`` is called with
    ``relevant_file.strip('`')`` (so the *lookup* sees the un-backticked
    path), but the payload ``path`` only has ``.strip()`` applied — so any
    surrounding backticks survive into the resulting comment payload. This
    test documents that asymmetry; it does not endorse it.
    """
    provider = _make_provider()
    recorded = {}

    def recording_resolver(diff_files, rel_file, rel_line, abs_pos):
        recorded["rel_file"] = rel_file
        return (3, 9)

    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        recording_resolver,
    )

    payload = provider.create_inline_comment("b", "`src/foo.py`", "x = 1")

    # Lookup arg has backticks stripped.
    assert recorded["rel_file"] == "src/foo.py"
    # Payload path preserves backticks (only .strip() runs on it).
    assert payload["path"] == "`src/foo.py`"


def test_create_inline_comment_payload_strips_surrounding_whitespace(monkeypatch):
    """Whitespace-only test: payload path is .strip()'d before being returned."""
    provider = _make_provider()
    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda *a, **kw: (3, 9),
    )

    payload = provider.create_inline_comment("b", "  src/foo.py  ", "x = 1")
    assert payload["path"] == "src/foo.py"


def test_create_inline_comment_limits_body_length(monkeypatch):
    """Body longer than max_comment_chars must be truncated with trailing '...'."""
    provider = _make_provider(max_chars=10)
    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda *a, **kw: (1, 1),
    )

    long_body = "A" * 50
    payload = provider.create_inline_comment(long_body, "f.py", "line")

    assert payload["body"].endswith("...")
    # limit_output_characters: output[:max_chars] + '...'
    assert payload["body"] == "A" * 10 + "..."


def test_create_inline_comment_does_not_truncate_short_body(monkeypatch):
    provider = _make_provider(max_chars=100)
    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda *a, **kw: (1, 1),
    )

    payload = provider.create_inline_comment("short", "f.py", "line")
    assert payload["body"] == "short"


# ---------------------------------------------------------------------------
# publish_inline_comment(s)
# ---------------------------------------------------------------------------

def test_publish_inline_comment_delegates_to_create_review(monkeypatch):
    """Single-comment publish path should result in a create_review call."""
    fake_pr = _FakePR()
    provider = _make_provider(pr=fake_pr)
    monkeypatch.setattr(
        gh_module,
        "find_line_number_of_relevant_line_in_file",
        lambda *a, **kw: (2, 7),
    )

    provider.publish_inline_comment("hi", "src/foo.py", "x = 1")

    assert len(fake_pr.create_review_calls) == 1
    call = fake_pr.create_review_calls[0]
    assert call["commit"].sha == "deadbeef"
    assert call["comments"] == [{"body": "hi", "path": "src/foo.py", "position": 2}]


def test_publish_inline_comments_non_422_reraises():
    """Non-422 exceptions during create_review must propagate (no fallback)."""
    fake_pr = _FakePR(raise_on_first=_FakeGithubException(status=500))
    provider = _make_provider(pr=fake_pr)

    with pytest.raises(_FakeGithubException) as excinfo:
        provider.publish_inline_comments(
            [{"body": "b", "path": "f.py", "position": 1}]
        )
    assert excinfo.value.status == 500
    # Only the original failing call was attempted - no fallback create_review.
    assert len(fake_pr.create_review_calls) == 1


def test_publish_inline_comments_disable_fallback_reraises_422():
    """When disable_fallback=True even a 422 must not trigger the fallback path."""
    fake_pr = _FakePR(raise_on_first=_FakeGithubException(status=422))
    provider = _make_provider(pr=fake_pr)

    with pytest.raises(_FakeGithubException):
        provider.publish_inline_comments(
            [{"body": "b", "path": "f.py", "position": 1}],
            disable_fallback=True,
        )
    assert len(fake_pr.create_review_calls) == 1


def test_publish_inline_comments_422_triggers_fallback(monkeypatch):
    """On 422 the provider should invoke the verification-based fallback."""
    fake_pr = _FakePR(raise_on_first=_FakeGithubException(status=422))
    provider = _make_provider(pr=fake_pr)

    called = {"n": 0, "args": None}

    def fake_fallback(comments):
        called["n"] += 1
        called["args"] = comments

    provider._publish_inline_comments_fallback_with_verification = fake_fallback

    comments = [{"body": "b", "path": "f.py", "position": 1}]
    provider.publish_inline_comments(comments)

    assert called["n"] == 1
    assert called["args"] == comments
    # The initial create_review attempt is the only one made directly here;
    # the fallback is stubbed out and would normally do further work.
    assert len(fake_pr.create_review_calls) == 1


def test_publish_inline_comments_fallback_failure_propagates(monkeypatch):
    fake_pr = _FakePR(raise_on_first=_FakeGithubException(status=422))
    provider = _make_provider(pr=fake_pr)

    def broken_fallback(comments):
        raise RuntimeError("fallback boom")

    provider._publish_inline_comments_fallback_with_verification = broken_fallback

    with pytest.raises(RuntimeError, match="fallback boom"):
        provider.publish_inline_comments(
            [{"body": "b", "path": "f.py", "position": 1}]
        )


def test_publish_inline_comments_success_no_fallback():
    """On a clean create_review call no fallback should be invoked."""
    fake_pr = _FakePR()
    provider = _make_provider(pr=fake_pr)

    sentinel = {"called": False}

    def should_not_run(_):
        sentinel["called"] = True

    provider._publish_inline_comments_fallback_with_verification = should_not_run

    provider.publish_inline_comments([{"body": "b", "path": "f.py", "position": 1}])

    assert sentinel["called"] is False
    assert len(fake_pr.create_review_calls) == 1


# ---------------------------------------------------------------------------
# publish_code_suggestions - multi-line vs single-line payload shape
# ---------------------------------------------------------------------------

def _stub_validation_passthrough(provider):
    """Bypass hunk-validation so we can directly assert the constructed payload."""
    provider.validate_comments_inside_hunks = lambda suggestions: suggestions


def test_publish_code_suggestions_multi_line_payload_shape():
    """Multi-line suggestions (end > start) must use start_line/start_side fields."""
    fake_pr = _FakePR()
    provider = _make_provider(pr=fake_pr)
    _stub_validation_passthrough(provider)

    captured = {}

    def capture(comments, disable_fallback=False):
        captured["comments"] = comments

    provider.publish_inline_comments = capture

    suggestions = [{
        "body": "```suggestion\nnew\n```",
        "relevant_file": "src/foo.py",
        "relevant_lines_start": 10,
        "relevant_lines_end": 14,
    }]

    assert provider.publish_code_suggestions(suggestions) is True

    assert "comments" in captured
    payload = captured["comments"][0]
    # publish_code_suggestions attaches an internal '_dedup_code_fp' fingerprint that
    # publish_inline_comments consumes and strips before the GitHub API call; it is not
    # part of the API payload shape under test, so drop it before comparing.
    assert "_dedup_code_fp" in payload
    payload = {key: value for key, value in payload.items() if key != "_dedup_code_fp"}
    assert payload == {
        "body": "```suggestion\nnew\n```",
        "path": "src/foo.py",
        "line": 14,
        "start_line": 10,
        "start_side": "RIGHT",
    }
    # Multi-line payloads must NOT carry a top-level 'side'; GitHub infers it.
    assert "side" not in payload


def test_publish_code_suggestions_single_line_payload_shape():
    """When start == end the API shape differs: no start_line/start_side, side only."""
    fake_pr = _FakePR()
    provider = _make_provider(pr=fake_pr)
    _stub_validation_passthrough(provider)

    captured = {}
    provider.publish_inline_comments = lambda comments, disable_fallback=False: captured.setdefault("c", comments)

    suggestions = [{
        "body": "fix",
        "relevant_file": "src/foo.py",
        "relevant_lines_start": 7,
        "relevant_lines_end": 7,
    }]

    assert provider.publish_code_suggestions(suggestions) is True
    payload = captured["c"][0]
    # publish_code_suggestions attaches an internal '_dedup_code_fp' fingerprint that
    # publish_inline_comments consumes and strips before the GitHub API call; it is not
    # part of the API payload shape under test, so drop it before comparing.
    assert "_dedup_code_fp" in payload
    payload = {key: value for key, value in payload.items() if key != "_dedup_code_fp"}
    assert payload == {
        "body": "fix",
        "path": "src/foo.py",
        "line": 7,
        "side": "RIGHT",
    }
    assert "start_line" not in payload and "start_side" not in payload


def test_publish_code_suggestions_skips_invalid_ranges():
    """Suggestions with missing/negative start, or end<start, must be skipped silently."""
    provider = _make_provider()
    _stub_validation_passthrough(provider)

    captured = {}
    provider.publish_inline_comments = lambda comments, disable_fallback=False: captured.setdefault("c", comments)

    suggestions = [
        {"body": "a", "relevant_file": "f.py",
         "relevant_lines_start": None, "relevant_lines_end": 5},
        {"body": "b", "relevant_file": "f.py",
         "relevant_lines_start": -1, "relevant_lines_end": 5},
        {"body": "c", "relevant_file": "f.py",
         "relevant_lines_start": 10, "relevant_lines_end": 3},
        {"body": "d", "relevant_file": "f.py",
         "relevant_lines_start": 4, "relevant_lines_end": 4},
    ]

    assert provider.publish_code_suggestions(suggestions) is True
    # Only the last (single-line) suggestion should be forwarded.
    assert len(captured["c"]) == 1
    assert captured["c"][0]["body"] == "d"


def test_publish_code_suggestions_returns_false_on_publish_error():
    """If publish_inline_comments raises, publish_code_suggestions returns False."""
    provider = _make_provider()
    _stub_validation_passthrough(provider)

    def boom(comments, disable_fallback=False):
        raise RuntimeError("nope")

    provider.publish_inline_comments = boom

    result = provider.publish_code_suggestions([{
        "body": "x", "relevant_file": "f.py",
        "relevant_lines_start": 1, "relevant_lines_end": 2,
    }])
    assert result is False


# ---------------------------------------------------------------------------
# resolve_comment_thread
# ---------------------------------------------------------------------------


def _make_graphql_response(data, errors=None):
    """Build a tuple mimicking PyGitHub's requestJson return for GraphQL."""
    body = {"data": data}
    if errors:
        body["errors"] = errors
    return (200, {}, json.dumps(body))


class _FakeRequester:
    """Records GraphQL calls and returns canned responses."""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def requestJsonAndCheck(self, method, url, input=None):
        self.calls.append(("check", method, url, input))
        return ({}, self._responses.pop(0))

    def requestJson(self, method, url, input=None):
        self.calls.append(("json", method, url, input))
        return self._responses.pop(0)


def _make_provider_with_graphql(rest_comment_data, graphql_responses):
    """Build a provider wired with fake REST + GraphQL responses."""
    p = GithubProvider.__new__(GithubProvider)
    p.repo = "owner/repo"
    p.pr_num = 42
    p.base_url = "https://api.github.com"

    all_responses = [rest_comment_data] + graphql_responses
    requester = _FakeRequester(all_responses)
    p.pr = SimpleNamespace(_requester=requester)
    p.github_client = SimpleNamespace(_Github__requester=requester)
    return p, requester


def _make_threads_response(threads, has_next_page=False, end_cursor=None):
    """Build a GraphQL response for reviewThreads with pageInfo."""
    return _make_graphql_response({
        "repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
            "nodes": threads,
        }}},
    })


class TestResolveCommentThread:
    def test_resolves_thread_successfully(self):
        rest_data = {"node_id": "PRR_comment1"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])
        resolve_response = _make_graphql_response({
            "resolveReviewThread": {"thread": {"isResolved": True}},
        })

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response, resolve_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is True
        assert len(requester.calls) == 3
        assert "resolveReviewThread" in requester.calls[2][3]["query"]

    def test_already_resolved_thread_returns_true(self):
        rest_data = {"node_id": "PRR_comment1"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": True,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is True
        assert len(requester.calls) == 2

    def test_handles_no_matching_thread(self):
        rest_data = {"node_id": "PRR_commentX"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_other"}]}},
        ])

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is False
        assert len(requester.calls) == 2

    def test_handles_missing_node_id(self):
        rest_data = {}  # no node_id

        provider, requester = _make_provider_with_graphql(rest_data, [])
        result = provider.resolve_comment_thread(123)

        assert result is False
        assert len(requester.calls) == 1

    def test_handles_graphql_errors_in_resolve_mutation(self):
        rest_data = {"node_id": "PRR_comment1"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])
        error_response = _make_graphql_response(
            {"resolveReviewThread": None},
            errors=[{"message": "Insufficient permissions"}],
        )

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response, error_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is False
        assert len(requester.calls) == 3

    def test_handles_resolve_returning_false(self):
        rest_data = {"node_id": "PRR_comment1"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])
        resolve_response = _make_graphql_response({
            "resolveReviewThread": {"thread": {"isResolved": False}},
        })

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response, resolve_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is False
        assert len(requester.calls) == 3

    def test_handles_unexpected_mutation_response_format(self):
        """Mutation returns non-tuple — should return False, not fall through to True."""
        rest_data = {"node_id": "PRR_comment1"}
        threads_response = _make_threads_response([
            {"id": "PRRT_thread1", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])

        provider, requester = _make_provider_with_graphql(
            rest_data, [threads_response, "not-a-tuple"]
        )
        result = provider.resolve_comment_thread(123)

        assert result is False

    def test_paginates_to_find_thread(self):
        """Thread is on the second page — pagination must follow."""
        rest_data = {"node_id": "PRR_comment1"}
        page1 = _make_threads_response(
            [{"id": "PRRT_other", "isResolved": False,
              "comments": {"nodes": [{"id": "PRR_other"}]}}],
            has_next_page=True, end_cursor="cursor1",
        )
        page2 = _make_threads_response([
            {"id": "PRRT_target", "isResolved": False,
             "comments": {"nodes": [{"id": "PRR_comment1"}]}},
        ])
        resolve_response = _make_graphql_response({
            "resolveReviewThread": {"thread": {"isResolved": True}},
        })

        provider, requester = _make_provider_with_graphql(
            rest_data, [page1, page2, resolve_response]
        )
        result = provider.resolve_comment_thread(123)

        assert result is True
        assert 'after: "cursor1"' in requester.calls[2][3]["query"]
        assert "resolveReviewThread" in requester.calls[3][3]["query"]

    def test_handles_rest_api_exception(self):
        """REST call to fetch comment throws — should not propagate."""
        p = GithubProvider.__new__(GithubProvider)
        p.repo = "owner/repo"
        p.pr_num = 42
        p.base_url = "https://api.github.com"

        class _BrokenRequester:
            def requestJsonAndCheck(self, *a, **kw):
                raise RuntimeError("network error")
            def requestJson(self, *a, **kw):
                raise RuntimeError("network error")

        p.pr = SimpleNamespace(_requester=_BrokenRequester())
        p.github_client = SimpleNamespace(_Github__requester=_BrokenRequester())

        result = p.resolve_comment_thread(123)
        assert result is False
