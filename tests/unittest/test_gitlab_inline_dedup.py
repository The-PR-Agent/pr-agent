"""
Tests for GitLabProvider dedup-aware publish_code_suggestions,
get_bot_review_comments, and edit_review_comment.
"""
from unittest.mock import MagicMock, patch

from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    RESOLVED_BODY_MARKER,
    RESOLVED_NOTE,
    generate_marker,
)


def _sug(label="possible issue", file="src/app.py",
         content="Check for None before dereferencing user_id on this line.",
         start=10, end=12):
    orig = {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }
    return {
        "body": f"**Suggestion:** {content} [{label}]\n```suggestion\nfix\n```",
        "relevant_file": file,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
        "original_suggestion": orig,
    }


def _make_provider():
    """Construct a GitLabProvider bypassing __init__ and set needed attributes."""
    from pr_agent.git_providers.gitlab_provider import GitLabProvider
    p = GitLabProvider.__new__(GitLabProvider)
    p.max_comment_chars = 65000
    p.gl = MagicMock()
    p.mr = MagicMock()
    p.id_mr = 1
    return p


def _set_settings(persistent_mode="update", resolve_outdated=True):
    values = {
        "persistent_inline_comments": persistent_mode,
        "resolve_outdated_inline_comments": resolve_outdated,
    }
    return patch(
        "pr_agent.git_providers.gitlab_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None: values.get(key, default)),
        ),
    )


def _set_mode(mode):
    return _set_settings(persistent_mode=mode, resolve_outdated=False)


# ---------------------------------------------------------------------------
# Off mode
# ---------------------------------------------------------------------------

class TestOffMode:
    def test_off_mode_skips_fetch(self):
        """In 'off' mode, get_bot_review_comments must never be called."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock()
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("off"):
            p.publish_code_suggestions([_sug()])

        p.get_bot_review_comments.assert_not_called()
        p.edit_review_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Update mode
# ---------------------------------------------------------------------------

class TestUpdateMode:
    def test_no_match_creates_new(self):
        """When no existing comment matches, send_inline_comment is called and marker is embedded."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(return_value=[])
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        # Set up get_diff_files so the suggestion loop completes
        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        s = _sug()
        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.get_bot_review_comments.assert_called_once()
        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_called_once()
        # The body passed to send_inline_comment should contain the marker
        call_body = p.send_inline_comment.call_args[0][0]
        assert MARKER_PREFIX in call_body
        assert MARKER_SUFFIX in call_body

    def test_match_edits_and_skips_creation(self):
        """When an existing comment matches, edit_review_comment is called and send_inline_comment is not."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing_body = "old body\n\n" + marker
        p.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 777,
                "body": existing_body,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        p.edit_review_comment = MagicMock(return_value=True)
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 777
        assert marker in called_body
        assert s["body"] in called_body
        p.send_inline_comment.assert_not_called()

    def test_edit_failure_falls_back_to_create(self):
        """When edit_review_comment returns False, fall back to send_inline_comment."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        p.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 777,
                "body": "old " + marker,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        p.edit_review_comment = MagicMock(return_value=False)
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_called_once()
        p.send_inline_comment.assert_called_once()

    def test_mixed_match_and_new(self):
        """Matched suggestions are edited; unmatched ones are created."""
        p = _make_provider()
        matched = _sug(content="Matched suggestion")
        unmatched = _sug(content="Brand new suggestion", start=40, end=42)
        marker_matched = generate_marker(matched["original_suggestion"])

        p.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 1,
                "body": marker_matched,
                "path": matched["relevant_file"],
                "line": matched["relevant_lines_end"],
                "start_line": matched["relevant_lines_start"],
            }]
        )
        p.edit_review_comment = MagicMock(return_value=True)
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([matched, unmatched])

        assert p.edit_review_comment.call_count == 1
        p.send_inline_comment.assert_called_once()
        call_body = p.send_inline_comment.call_args[0][0]
        assert "Brand new" in call_body

    def test_fetch_failure_falls_back_to_creating_all(self):
        """When get_bot_review_comments raises, fall back to creating all suggestions."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(side_effect=RuntimeError("api down"))
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([_sug()])

        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_called_once()


# ---------------------------------------------------------------------------
# Skip mode
# ---------------------------------------------------------------------------

class TestSkipMode:
    def test_match_skips_entirely(self):
        """In skip mode, a matched suggestion is not re-posted at all."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        p.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 1,
                "body": marker,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("skip"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_not_called()

    def test_no_match_still_creates(self):
        """In skip mode, suggestions without a matching existing comment are still created."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(return_value=[])
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("skip"):
            p.publish_code_suggestions([_sug()])

        p.send_inline_comment.assert_called_once()

    def test_resolved_match_reopens_and_refreshes_body(self):
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = {
            "id": 22,
            "thread_id": "DIS-22",
            "discussion_id": "DIS-22",
            "body": f"old body\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}\n\n{marker}",
            "path": s["relevant_file"],
            "line": s["relevant_lines_end"],
            "start_line": s["relevant_lines_start"],
            "is_resolved": True,
        }
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.unresolve_review_thread = MagicMock(return_value=True)
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])
        with _set_settings(persistent_mode="skip", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 22
        assert s["body"] in called_body
        assert RESOLVED_BODY_MARKER not in called_body
        p.unresolve_review_thread.assert_called_once_with(existing)
        p.send_inline_comment.assert_not_called()


# ---------------------------------------------------------------------------
# get_bot_review_comments — bot identity filtering
# ---------------------------------------------------------------------------

class TestGetBotReviewCommentsFiltering:
    """Exercises the real get_bot_review_comments filter (not mocked out)."""

    def _make_discussions(self, notes_list):
        """Build a list of mock discussion objects from a list of note dicts."""
        discussions = []
        for i, notes in enumerate(notes_list):
            d = MagicMock()
            d.id = f"discussion-{i}"
            d.attributes = {"notes": notes}
            discussions.append(d)
        return discussions

    def _note(self, note_id, username, body="comment body", path="src/app.py", new_line=5):
        return {
            "id": note_id,
            "type": "DiffNote",
            "body": body,
            "author": {"username": username},
            "position": {
                "new_path": path,
                "new_line": new_line,
                "line_range": None,
            },
        }

    def test_bot_authored_notes_kept(self):
        """Notes authored by the bot username are returned."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        bot_note = self._note(10, "my-bot", body="bot comment")
        human_note = self._note(11, "alice", body="human comment")

        p.mr = MagicMock()
        p.mr.discussions.list.return_value = self._make_discussions([[bot_note, human_note]])

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert [c["id"] for c in result] == [10]

    def test_human_authored_notes_rejected(self):
        """Notes not authored by the bot are excluded."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        human_note = self._note(20, "alice", body="human")

        p.mr = MagicMock()
        p.mr.discussions.list.return_value = self._make_discussions([[human_note]])

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_non_diff_notes_excluded(self):
        """Regular MR notes (type != DiffNote) are excluded."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        regular_note = {
            "id": 30,
            "type": "Note",  # NOT a DiffNote
            "body": "general comment",
            "author": {"username": "my-bot"},
            "position": None,
        }

        p.mr = MagicMock()
        d = MagicMock()
        d.id = "disc-0"
        d.attributes = {"notes": [regular_note]}
        p.mr.discussions.list.return_value = [d]

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_returns_empty_on_exception(self):
        """If discussions.list raises, return [] and log a warning."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        p.mr = MagicMock()
        p.mr.discussions.list.side_effect = RuntimeError("api failure")

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_multiline_note_extracts_start_line(self):
        """Multi-line DiffNote has start_line extracted from line_range.start.new_line."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        multiline_note = {
            "id": 40,
            "type": "DiffNote",
            "body": "multi-line",
            "author": {"username": "my-bot"},
            "position": {
                "new_path": "src/foo.py",
                "new_line": 20,
                "line_range": {
                    "start": {"new_line": 15},
                    "end": {"new_line": 20},
                },
            },
        }

        p.mr = MagicMock()
        d = MagicMock()
        d.id = "disc-0"
        d.attributes = {"notes": [multiline_note]}
        p.mr.discussions.list.return_value = [d]

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert len(result) == 1
        assert result[0]["start_line"] == 15
        assert result[0]["line"] == 20


# ---------------------------------------------------------------------------
# edit_review_comment
# ---------------------------------------------------------------------------

class TestEditReviewComment:
    def test_returns_true_on_success(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        mock_note = MagicMock()
        p.mr = MagicMock()
        p.mr.notes.get.return_value = mock_note

        result = p.edit_review_comment(99, "new body")

        assert result is True
        p.mr.notes.get.assert_called_once_with(99)
        assert mock_note.body == "new body"
        mock_note.save.assert_called_once()

    def test_returns_false_on_exception(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        p.mr = MagicMock()
        p.mr.notes.get.side_effect = RuntimeError("not found")

        result = p.edit_review_comment(99, "new body")

        assert result is False


class TestGitLabResolveUnresolve:
    def _provider(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.mr = MagicMock()
        return p

    def test_resolve_calls_save_with_resolved_true(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = True
        d.resolved = False
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is True
        p.mr.discussions.get.assert_called_once_with("DIS123")
        assert d.resolved is True
        d.save.assert_called_once()

    def test_unresolve_calls_save_with_resolved_false(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = True
        d.resolved = True
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.unresolve_review_thread({"thread_id": "DIS123"}) is True
        assert d.resolved is False
        d.save.assert_called_once()

    def test_non_resolvable_discussion_returns_false(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = False
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is False
        d.save.assert_not_called()

    def test_resolve_returns_false_on_exception(self):
        p = self._provider()
        p.mr.discussions.get = MagicMock(side_effect=RuntimeError("api down"))
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is False

    def test_resolve_returns_false_when_save_raises(self):
        """resolve_review_thread returns False and logs a warning when save() raises."""
        p = self._provider()
        d = MagicMock()
        d.resolvable = True
        d.resolved = False
        d.save.side_effect = RuntimeError("save failed")
        p.mr.discussions.get = MagicMock(return_value=d)

        import logging
        with patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_logger:
            mock_log = MagicMock()
            mock_logger.return_value = mock_log
            result = p.resolve_review_thread({"thread_id": "DIS123"})

        assert result is False
        mock_log.warning.assert_called_once()

    def test_resolve_returns_false_when_thread_id_missing(self):
        p = self._provider()
        p.mr.discussions.get = MagicMock()
        assert p.resolve_review_thread({"id": 1}) is False
        p.mr.discussions.get.assert_not_called()


class TestGetBotReviewCommentsIncludesIsResolved:
    def _make_provider(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.gl = MagicMock()
        p.gl.user.username = "pr-agent-bot"
        p.mr = MagicMock()
        return p

    def _note(self, note_id, resolved=False):
        return {
            "type": "DiffNote",
            "id": note_id,
            "body": "x",
            "author": {"username": "pr-agent-bot"},
            "position": {"new_path": "a.py", "new_line": 5},
            "resolved": resolved,
        }

    def test_is_resolved_propagates_from_discussion(self):
        p = self._make_provider()
        d_resolved = MagicMock()
        d_resolved.id = "D-1"
        d_resolved.resolved = True   # top-level signal
        d_resolved.attributes = {"notes": [self._note(1, resolved=True)]}
        d_unresolved = MagicMock()
        d_unresolved.id = "D-2"
        d_unresolved.resolved = False  # top-level signal
        d_unresolved.attributes = {"notes": [self._note(2, resolved=False)]}
        p.mr.discussions.list = MagicMock(return_value=[d_resolved, d_unresolved])
        out = p.get_bot_review_comments()
        ids_to_resolved = {c["id"]: c["is_resolved"] for c in out}
        assert ids_to_resolved == {1: True, 2: False}
        assert all("thread_id" in c for c in out)

    def test_top_level_resolved_wins_over_note_resolved(self):
        """Top-level discussion.resolved=True takes precedence over first-note resolved=False."""
        p = self._make_provider()
        d = MagicMock()
        d.id = "D-1"
        d.resolved = True  # top-level says resolved
        d.attributes = {"notes": [self._note(1, resolved=False)]}  # note says not resolved
        p.mr.discussions.list = MagicMock(return_value=[d])
        out = p.get_bot_review_comments()
        assert len(out) == 1
        assert out[0]["is_resolved"] is True

    def test_fallback_to_note_resolved_when_top_level_absent(self):
        """When discussion has no top-level resolved attribute, first-note resolved is used."""
        p = self._make_provider()
        d = MagicMock(spec=["id", "attributes"])  # no .resolved attribute
        d.id = "D-1"
        d.attributes = {"notes": [self._note(1, resolved=True)]}
        p.mr.discussions.list = MagicMock(return_value=[d])
        out = p.get_bot_review_comments()
        assert len(out) == 1
        assert out[0]["is_resolved"] is True


# ---------------------------------------------------------------------------
# Outdated pass
# ---------------------------------------------------------------------------

def _gl_existing(c_id, marker, *, is_resolved=False, body_extra="", thread_id=None):
    return {
        "id": c_id,
        "thread_id": thread_id or f"DIS-{c_id}",
        "discussion_id": thread_id or f"DIS-{c_id}",
        "body": "old body" + body_extra + "\n\n" + marker,
        "path": "src/app.py",
        "line": 12,
        "start_line": 10,
        "is_resolved": is_resolved,
    }


class TestGitLabOutdatedPass:
    def _provider(self):
        p = _make_provider()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])
        return p

    def test_outdated_marker_resolves_and_edits(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=10, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock(return_value=True)
        p.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_called_once()
        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 10
        assert RESOLVED_NOTE in called_body
        assert RESOLVED_BODY_MARKER in called_body

    def test_same_marker_different_location_creates_new_and_resolves_old(self):
        p = self._provider()
        s = _sug(start=40, end=42)
        marker = generate_marker(s["original_suggestion"])
        existing = _gl_existing(c_id=9, marker=marker)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock(return_value=True)
        p.unresolve_review_thread = MagicMock()
        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 80))
        p.get_diff_files = MagicMock(return_value=[fake_file])
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.send_inline_comment.assert_called_once()
        p.resolve_review_thread.assert_called_once_with(existing)
        p.unresolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 9
        assert RESOLVED_NOTE in called_body
        assert s["body"] not in called_body

    def test_already_resolved_is_skipped(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=11, marker=marker_outdated, is_resolved=True)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_body_marker_skips_resolve(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(
            c_id=12, marker=marker_outdated, is_resolved=False,
            body_extra=f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}",
        )
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_re_emit_after_prior_resolve_calls_unresolve(self):
        p = self._provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = _gl_existing(c_id=13, marker=marker, is_resolved=True)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock()
        p.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.edit_review_comment.assert_called_once()
        p.unresolve_review_thread.assert_called_once()
        p.resolve_review_thread.assert_not_called()

    def test_setting_off_skips_outdated_pass(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=14, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=False):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_persistent_off_skips_outdated_pass(self):
        p = self._provider()
        s = _sug()
        p.get_bot_review_comments = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="off", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.get_bot_review_comments.assert_not_called()
        p.resolve_review_thread.assert_not_called()

    def test_resolve_failure_skips_edit(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=15, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock(return_value=False)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_called_once()
        p.edit_review_comment.assert_not_called()

    def test_edit_failure_after_resolve_unresolves_thread(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=16, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=False)
        p.resolve_review_thread = MagicMock(return_value=True)
        p.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True), \
             patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_called_once_with(existing)
        p.edit_review_comment.assert_called_once()
        p.unresolve_review_thread.assert_called_once_with(existing)
        warning_message = logger.warning.call_args[0][0]
        assert "failed to write resolved marker" in warning_message
        assert "16" in warning_message

    def test_existing_hash_re_emitted_skips_outdated_resolve(self):
        p = self._provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = _gl_existing(c_id=20, marker=marker)  # is_resolved defaults to False
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock()
        p.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.resolve_review_thread.assert_not_called()
        # Edit happened in the update path, not the outdated pass:
        p.edit_review_comment.assert_called_once()
        # Not previously resolved, so no unresolve:
        p.unresolve_review_thread.assert_not_called()
