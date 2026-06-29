"""
Tests for GitHub provider inline comment creation, publishing fallback,
and multi-line code suggestion payload shape.

These tests use ``GithubProvider.__new__(GithubProvider)`` to bypass network-bound
``__init__`` and inject minimal fake collaborators. No real GitHub API access.
"""

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
