from unittest.mock import MagicMock, patch

import pytest

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


@pytest.fixture
def provider():
    with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
         patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
         patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
        from pr_agent.git_providers.github_provider import GithubProvider
        p = GithubProvider.__new__(GithubProvider)
        p.pr = MagicMock()
        p.last_commit_id = MagicMock(sha="abc123")
        p.repo = "owner/repo"
        p.base_url = "https://api.github.com"
        p.max_comment_chars = 65000
        p.github_user_id = "pr-agent-bot"
        p.deployment_type = "user"
        p.validate_comments_inside_hunks = lambda x: x
        return p


def _set_settings(persistent_mode="update", resolve_outdated=True):
    """Patch get_settings to return both persistent_inline_comments and resolve_outdated_inline_comments."""
    values = {
        "persistent_inline_comments": persistent_mode,
        "resolve_outdated_inline_comments": resolve_outdated,
    }
    return patch(
        "pr_agent.git_providers.github_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None: values.get(key, default)),
        ),
    )


# Backward-compat wrapper for existing tests that only set persistent mode.
def _set_mode(mode):
    return _set_settings(persistent_mode=mode, resolve_outdated=False)


class TestOffMode:
    def test_off_mode_skips_fetch(self, provider):
        provider.get_bot_review_comments = MagicMock()
        provider.edit_review_comment = MagicMock()
        with _set_mode("off"):
            provider.publish_code_suggestions([_sug()])
        provider.get_bot_review_comments.assert_not_called()
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()


class TestUpdateMode:
    def test_no_match_creates_new(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()
        args, kwargs = provider.pr.create_review.call_args
        body_published = kwargs["comments"][0]["body"]
        assert MARKER_PREFIX in body_published and MARKER_SUFFIX in body_published

    def test_match_edits_and_skips_creation(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing_body = "old body\n\n" + marker
        provider.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 777,
                "body": existing_body,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 777
        assert marker in called_body
        assert s["body"] in called_body
        provider.pr.create_review.assert_not_called()

    def test_edit_failure_falls_back_to_create(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 777,
                "body": "old " + marker,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        provider.edit_review_comment = MagicMock(return_value=False)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.pr.create_review.assert_called_once()

    def test_match_without_comment_id_creates_new_without_editing(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": None,
                "body": "old " + marker,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()

    def test_mixed_match_and_new(self, provider):
        matched = _sug(content="Matched suggestion")
        unmatched = _sug(content="Brand new suggestion", start=40, end=42)
        marker_matched = generate_marker(matched["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 1,
                "body": marker_matched,
                "path": matched["relevant_file"],
                "line": matched["relevant_lines_end"],
                "start_line": matched["relevant_lines_start"],
            }]
        )
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([matched, unmatched])
        assert provider.edit_review_comment.call_count == 1
        provider.pr.create_review.assert_called_once()
        created = provider.pr.create_review.call_args.kwargs["comments"]
        assert len(created) == 1
        assert "Brand new" in created[0]["body"]

    def test_fetch_failure_falls_back_to_creating_all(self, provider):
        provider.get_bot_review_comments = MagicMock(side_effect=RuntimeError("api down"))
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()


class TestSkipMode:
    def test_match_skips_entirely(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{
                "id": 1,
                "body": marker,
                "path": s["relevant_file"],
                "line": s["relevant_lines_end"],
                "start_line": s["relevant_lines_start"],
            }]
        )
        provider.edit_review_comment = MagicMock()
        with _set_mode("skip"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_not_called()

    def test_no_match_still_creates(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        provider.edit_review_comment = MagicMock()
        with _set_mode("skip"):
            provider.publish_code_suggestions([_sug()])
        provider.pr.create_review.assert_called_once()

    def test_resolved_match_reopens_and_refreshes_body(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = {
            "id": 22,
            "thread_id": "T22",
            "body": f"old body\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}\n\n{marker}",
            "path": s["relevant_file"],
            "line": s["relevant_lines_end"],
            "start_line": s["relevant_lines_start"],
            "is_resolved": True,
        }
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="skip", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 22
        assert s["body"] in called_body
        assert RESOLVED_BODY_MARKER not in called_body
        provider.unresolve_review_thread.assert_called_once_with(existing)
        provider.pr.create_review.assert_not_called()


class TestGetBotReviewCommentsGraphQL:
    """Exercises the GraphQL-backed get_bot_review_comments."""

    def _make_provider(self, deployment_type, user_id=None):
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            p = GithubProvider.__new__(GithubProvider)
            p.pr = MagicMock()
            p.pr.number = 42
            p.repo = "owner/repo"
            p.base_url = "https://api.github.com"
            p.deployment_type = deployment_type
            p.github_user_id = user_id
            return p

    def _gql_response(self, threads, has_next_page=False, end_cursor=None):
        return ({}, {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                            "nodes": threads,
                        }
                    }
                }
            }
        })

    def _thread(self, thread_id, is_resolved, comments):
        return {"id": thread_id, "isResolved": is_resolved, "comments": {"nodes": comments}}

    def _comment(self, db_id, login, body="x", path="a.py", line=1, start_line=None):
        return {"databaseId": db_id, "body": body, "path": path, "line": line,
                "startLine": start_line, "author": {"login": login}}

    def test_app_deployment_filters_by_app_name(self):
        provider = self._make_provider(deployment_type="app")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response([
                self._thread("T1", False, [self._comment(1, "my-bot[bot]")]),
                self._thread("T2", False, [self._comment(2, "alice")]),
            ])
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": "my-bot" if key == "GITHUB.APP_NAME" else default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1]
        assert out[0]["thread_id"] == "T1"
        assert out[0]["is_resolved"] is False

    def test_user_deployment_filters_by_user_id(self):
        provider = self._make_provider(deployment_type="user", user_id=None)
        provider.get_user_id = MagicMock(return_value="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response([
                self._thread("T1", True, [self._comment(5, "pr-agent-bot")]),
                self._thread("T2", False, [self._comment(6, "someone-else")]),
            ])
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [5]
        assert out[0]["is_resolved"] is True
        provider.get_user_id.assert_called_once()

    def test_skips_comments_without_database_id(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response([
                self._thread("T1", False, [self._comment(None, "pr-agent-bot")]),
                self._thread("T2", False, [self._comment(6, "pr-agent-bot")]),
            ])
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [6]

    def test_paginates_until_has_next_page_false(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        page1 = self._gql_response(
            [self._thread("T1", False, [self._comment(1, "pr-agent-bot")])],
            has_next_page=True, end_cursor="cur1",
        )
        page2 = self._gql_response(
            [self._thread("T2", False, [self._comment(2, "pr-agent-bot")])],
            has_next_page=False, end_cursor=None,
        )
        provider.pr._requester.requestJsonAndCheck = MagicMock(side_effect=[page1, page2])
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1, 2]
        assert provider.pr._requester.requestJsonAndCheck.call_count == 2

    def test_pagination_breaks_when_end_cursor_missing_despite_has_next_page(self):
        # Guard against a malformed server response (hasNextPage=True but no
        # endCursor) infinite-looping by re-issuing the first-page query.
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response(
                [self._thread("T1", False, [self._comment(1, "pr-agent-bot")])],
                has_next_page=True, end_cursor=None,
            )
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1]
        assert provider.pr._requester.requestJsonAndCheck.call_count == 1

    def test_graphql_errors_array_returns_empty(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"errors": [{"message": "boom"}]})
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert out == []

    def test_graphql_exception_returns_empty(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(side_effect=RuntimeError("net"))
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert out == []

    def test_graphql_url_strips_api_v3_suffix_for_ghes(self):
        # GHES REST base is `.../api/v3` but GraphQL lives at `.../api/graphql`;
        # naive `{base_url}/graphql` would 404 on GHES.
        from pr_agent.git_providers.github_provider import GithubProvider
        p = GithubProvider.__new__(GithubProvider)
        p.base_url = "https://ghes.example.com/api/v3"
        assert p._graphql_url() == "https://ghes.example.com/api/graphql"

    def test_graphql_url_passes_through_for_github_com(self):
        from pr_agent.git_providers.github_provider import GithubProvider
        p = GithubProvider.__new__(GithubProvider)
        p.base_url = "https://api.github.com"
        assert p._graphql_url() == "https://api.github.com/graphql"


class TestGitHubResolveUnresolve:
    """Exercises resolve_review_thread / unresolve_review_thread mutations."""

    def _make_provider(self):
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            p = GithubProvider.__new__(GithubProvider)
            p.pr = MagicMock()
            p.base_url = "https://api.github.com"
            return p

    def test_resolve_calls_graphql_and_returns_true(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        )
        assert p.resolve_review_thread({"thread_id": "T1"}) is True
        method, url = p.pr._requester.requestJsonAndCheck.call_args[0]
        assert method == "POST"
        assert url.endswith("/graphql")
        payload = p.pr._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert "resolveReviewThread" in payload["query"]
        assert payload["variables"] == {"threadId": "T1"}

    def test_unresolve_calls_graphql_and_returns_true(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}})
        )
        assert p.unresolve_review_thread({"thread_id": "T1"}) is True
        payload = p.pr._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert "unresolveReviewThread" in payload["query"]
        assert payload["variables"] == {"threadId": "T1"}

    def test_resolve_returns_false_on_errors_array(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"errors": [{"message": "perm denied"}]})
        )
        assert p.resolve_review_thread({"thread_id": "T1"}) is False

    def test_resolve_returns_false_on_exception(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(side_effect=RuntimeError("net"))
        assert p.resolve_review_thread({"thread_id": "T1"}) is False

    def test_resolve_returns_false_when_thread_id_missing(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock()
        assert p.resolve_review_thread({"id": 5}) is False
        p.pr._requester.requestJsonAndCheck.assert_not_called()


class TestGetRepoSettings:
    def test_logs_warning_when_repo_settings_cannot_be_read(self, provider):
        provider.repo_obj = MagicMock()
        provider.repo_obj.get_contents.side_effect = RuntimeError("ghes read failed")

        with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            result = provider.get_repo_settings()

        assert result == ""
        logger.warning.assert_called_once()
        warning_message = logger.warning.call_args[0][0]
        assert "Failed to load .pr_agent.toml file" in warning_message
        assert "ghes read failed" in warning_message


def _existing(c_id, marker, *, is_resolved=False, body_extra="", path="src/app.py", thread_id=None):
    return {
        "id": c_id,
        "thread_id": thread_id or f"T{c_id}",
        "body": "old body" + body_extra + "\n\n" + marker,
        "path": path,
        "line": 12,
        "start_line": 10,
        "is_resolved": is_resolved,
    }


class TestOutdatedPass:
    def test_same_marker_different_location_creates_new_and_resolves_old(self, provider):
        s = _sug(start=40, end=42)
        marker = generate_marker(s["original_suggestion"])
        existing = _existing(c_id=776, marker=marker, path=s["relevant_file"])
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.pr.create_review.assert_called_once()
        provider.resolve_review_thread.assert_called_once_with(existing)
        provider.unresolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 776
        assert RESOLVED_NOTE in called_body
        assert s["body"] not in called_body

    def test_outdated_marker_resolves_and_edits(self, provider):
        s_emitted = _sug(content="A new and different suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="An old suggestion that's no longer flagged")["original_suggestion"])
        existing = _existing(c_id=777, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        called_comment = provider.resolve_review_thread.call_args[0][0]
        assert called_comment["id"] == 777
        # edit_review_comment called once for the resolution note
        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 777
        assert RESOLVED_NOTE in called_body
        assert RESOLVED_BODY_MARKER in called_body
        provider.pr.create_review.assert_called_once()  # for the new suggestion

    def test_already_resolved_is_skipped(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=778, marker=marker_outdated, is_resolved=True)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        # edit_review_comment must not be called for the outdated comment;
        # there are no matched existing comments either, so total calls == 0.
        provider.edit_review_comment.assert_not_called()

    def test_body_marker_signals_user_unresolved_skip(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(
            c_id=779, marker=marker_outdated, is_resolved=False,
            body_extra=f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}",
        )
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()

    def test_re_emit_after_prior_resolve_calls_unresolve(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = _existing(c_id=780, marker=marker, is_resolved=True)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock()
        provider.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        provider.unresolve_review_thread.assert_called_once()
        provider.resolve_review_thread.assert_not_called()
        provider.pr.create_review.assert_not_called()

    def test_setting_off_skips_outdated_pass(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=781, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=False):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()

    @pytest.mark.parametrize("resolve_outdated", ["false", "0", "no", "off", "False", " OFF "])
    def test_setting_false_string_skips_outdated_pass(self, provider, resolve_outdated):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=784, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=resolve_outdated):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()

    @pytest.mark.parametrize("resolve_outdated", ["true", "1", "yes", "on", "True", " ON "])
    def test_setting_true_string_enables_outdated_pass(self, provider, resolve_outdated):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=785, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=resolve_outdated):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once_with(existing)
        provider.edit_review_comment.assert_called_once()

    def test_invalid_resolve_outdated_setting_warns_and_uses_default(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=786, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated="definitely"), \
             patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once_with(existing)
        warning_message = logger.warning.call_args[0][0]
        assert "Invalid boolean value for pr_code_suggestions.resolve_outdated_inline_comments" in warning_message
        assert "'definitely'" in warning_message

    def test_persistent_off_skips_outdated_pass_even_when_setting_on(self, provider):
        s = _sug()
        provider.get_bot_review_comments = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="off", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.get_bot_review_comments.assert_not_called()
        provider.resolve_review_thread.assert_not_called()

    def test_resolve_failure_skips_edit(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=782, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock(return_value=False)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        # The resolution-note edit must not happen for this outdated comment.
        # No other edit calls expected (the emitted suggestion creates new).
        provider.edit_review_comment.assert_not_called()

    def test_edit_failure_after_resolve_does_not_raise(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=783, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=False)
        provider.resolve_review_thread = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True), \
             patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
            logger = MagicMock()
            mock_get_logger.return_value = logger
            # Should not raise.
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        provider.edit_review_comment.assert_called_once()
        provider.unresolve_review_thread.assert_called_once_with(existing)
        warning_message = logger.warning.call_args[0][0]
        assert "failed to write resolved marker" in warning_message
        assert "783" in warning_message

    def test_outdated_comment_without_id_is_not_resolved_or_edited(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=None, marker=marker_outdated, thread_id="T-missing-id")
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()


class TestStructuredHashLivePath:
    """End-to-end: paraphrased prose + identical improved_code → update, not create."""

    def test_paraphrased_prose_same_edit_routes_to_edit_review_comment(self, provider):
        # Two suggestions that differ only in wording; improved_code is identical.
        improved = "cleanup_mode=None if dry_run else cleanup_mode,"
        file = "src/release.py"
        first_run = {
            "relevant_file": file,
            "label": "possible issue",
            "suggestion_content": (
                "When dry_run=True, cleanup_mode is still passed through "
                "unchanged to bump_version."
            ),
            "improved_code": improved,
        }
        second_run = {
            "relevant_file": file,
            "label": "possible issue",
            "suggestion_content": (
                "When dry_run=True, the cleanup_mode is still forwarded "
                "unchanged to bump_version."
            ),
            "improved_code": improved,
        }

        marker_first = generate_marker(first_run)
        marker_second = generate_marker(second_run)
        assert marker_first == marker_second, \
            "paraphrased prose with identical improved_code must collide"

        existing_comment = {
            "id": 777,
            "thread_id": "T1",
            "body": f"old body\n\n{marker_first}",
            "path": file,
            "line": 12,
            "start_line": 10,
            "is_resolved": False,
        }
        provider.get_bot_review_comments = MagicMock(return_value=[existing_comment])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock()

        body_text = (
            "**Suggestion:** paraphrased wording, same fix [possible issue]\n"
            "```suggestion\n"
            f"{improved}\n"
            "```"
        )
        code_suggestion = {
            "body": body_text,
            "relevant_file": file,
            "relevant_lines_start": 10,
            "relevant_lines_end": 12,
            "original_suggestion": second_run,
        }

        with _set_mode("update"):
            provider.publish_code_suggestions([code_suggestion])

        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 777
        assert marker_first in called_body
        provider.pr.create_review.assert_not_called()
