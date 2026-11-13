"""Persistent GitLab review lookup should read only the Notes pages it needs."""

from math import ceil
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from gitlab import Gitlab

import pr_agent.agent.pr_agent as pr_agent_module
import pr_agent.commands.pr_reviewer as reviewer_module
from pr_agent.algo.comment_identity import PRReviewHeader, PRReviewIdentity
from pr_agent.algo.review_finding_state import serialize_review_state
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider

_MARKER = PRReviewIdentity.REGULAR.value
_LEGACY = f"{PRReviewHeader.REGULAR.value} 🔍"
_IDENTIFIERS = (_MARKER, _LEGACY)


def _notes(count, *, marker_at=None, legacy_at=None, marker_author=7, valid_state=True):
    notes = [{"id": count - index, "body": f"Ordinary comment {index}", "author": {"id": 7}}
             for index in range(count)]
    if marker_at is not None:
        state = serialize_review_state({"schema_version": 1, "findings": [], "last_run": {}}) if valid_state else ""
        notes[marker_at]["body"] = f"{_LEGACY}\n\n{_MARKER}\n\nPrevious review\n\n{state}"
        notes[marker_at]["author"]["id"] = marker_author
    if legacy_at is not None:
        notes[legacy_at]["body"] = f"{_LEGACY}\n\nLegacy review"
    return notes


def _provider_with_notes(notes, *, error_page=None):
    """Exercise the real python-gitlab page iterator with an HTTP transport stub."""
    client = Gitlab("https://gitlab.example.com", private_token="test-token")
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.mr = client.projects.get("test/repo", lazy=True).mergerequests.get(1, lazy=True)
    provider._get_own_user_id = lambda: 7

    def http_request(_method, url, **_kwargs):
        page_number = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        if page_number == error_page:
            raise RuntimeError("later Notes page failed")
        start = (page_number - 1) * 100
        response = MagicMock()
        response.json.return_value = notes[start:start + 100]
        response.headers = {}
        response.links = (
            {"next": {"url": f"https://gitlab.example.com/api/v4/projects/test%2Frepo/"
                               f"merge_requests/1/notes?page={page_number + 1}&per_page=100"}}
            if start + 100 < len(notes) else {}
        )
        return response

    client.http_request = MagicMock(side_effect=http_request)
    return provider, client


@pytest.mark.parametrize(("count", "marker_at", "expected_gets"), [
    (0, None, 1),
    (10, 0, 1),
    (99, 0, 1),
    (100, 0, 1),
    (101, 0, 1),
    (150, 0, 1),
    (150, 100, 2),
    (150, 149, 2),
    (1000, 0, 1),
    (1000, 999, 10),
    (1000, None, 10),
])
def test_lookup_reads_only_required_notes_pages(count, marker_at, expected_gets):
    provider, client = _provider_with_notes(_notes(count, marker_at=marker_at))

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER), None)

    assert (match[0].id if match else None) == (count - marker_at if marker_at is not None else None)
    assert client.http_request.call_count == expected_gets
    assert client.http_request.call_args_list[0].kwargs["per_page"] == 100
    assert client.http_request.call_count <= max(1, ceil(count / 100))


def test_newest_matching_marker_wins_without_fetching_older_page():
    notes = _notes(150, marker_at=120)
    notes[0] = {"id": 151, "body": f"{_LEGACY}\n\n{_MARKER}\n\nNewer review", "author": {"id": 7}}
    provider, client = _provider_with_notes(notes)

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 151
    assert client.http_request.call_count == 1


def test_newer_legacy_comment_never_displaces_older_marker():
    provider, client = _provider_with_notes(_notes(150, marker_at=120, legacy_at=0))

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 30
    assert client.http_request.call_count == 2


def test_legacy_lookup_reuses_pages_without_another_notes_request():
    provider, client = _provider_with_notes(_notes(150, legacy_at=0))

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 150
    assert client.http_request.call_count == 2


def test_author_check_skips_spoofed_marker_and_finds_real_author():
    notes = _notes(150, marker_at=0, marker_author=99)
    notes[120]["body"] = f"{_LEGACY}\n\n{_MARKER}\n\nReal review"
    provider, client = _provider_with_notes(notes)

    match = next(GitProvider._iter_persistent_comments(
        provider, _IDENTIFIERS, identity_marker=_MARKER, require_agent_authorship=True))

    assert match[0].id == 30
    assert client.http_request.call_count == 2


@pytest.mark.parametrize("marker_at", [0, None])
def test_later_page_error_only_matters_if_lookup_needs_that_page(marker_at):
    provider, client = _provider_with_notes(_notes(150, marker_at=marker_at), error_page=2)

    if marker_at is None:
        with pytest.raises(RuntimeError, match="later Notes page failed"):
            next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER), None)
        assert client.http_request.call_count == 2
    else:
        match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))
        assert match[0].id == 150
        assert client.http_request.call_count == 1


def test_public_comment_list_contract_remains_unchanged():
    provider, client = _provider_with_notes(_notes(150))

    comments = provider.get_issue_comments_newest_first()

    assert isinstance(comments, list)
    assert [comment.id for comment in comments] == list(range(150, 0, -1))
    assert client.http_request.call_count == 2


@pytest.mark.parametrize("scope", ["subclass", "instance"])
@pytest.mark.parametrize("accessor", ["get_issue_comments", "get_issue_comments_newest_first"])
def test_overridden_comment_accessor_keeps_existing_lookup_behavior(accessor, scope):
    provider, client = _provider_with_notes(_notes(150, marker_at=0))
    replacement = SimpleNamespace(id=999, body=f"{_LEGACY}\n\n{_MARKER}")
    if scope == "subclass":
        custom_provider = type("CustomGitLabProvider", (GitLabProvider,), {accessor: lambda self: [replacement]})
        provider.__class__ = custom_provider
    else:
        setattr(provider, accessor, lambda: [replacement])

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 999
    assert client.http_request.call_count == 0


def test_instance_overridden_persistent_iterator_hook_is_used():
    provider, client = _provider_with_notes(_notes(150))
    replacement = SimpleNamespace(id=999, body=f"{_LEGACY}\n\n{_MARKER}")
    provider._get_persistent_comment_iterators = MagicMock(return_value=iter([[replacement], [replacement]]))

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 999
    provider._get_persistent_comment_iterators.assert_called_once_with(2)
    client.http_request.assert_not_called()


def test_duck_typed_mock_without_hook_keeps_list_fallback():
    provider = MagicMock()
    replacement = SimpleNamespace(id=888, body=f"{_LEGACY}\n\n{_MARKER}")
    provider.get_issue_comments_newest_first.return_value = [replacement]

    match = next(GitProvider._iter_persistent_comments(provider, _IDENTIFIERS, identity_marker=_MARKER))

    assert match[0].id == 888
    provider.get_issue_comments_newest_first.assert_called_once()


def test_missing_finding_state_scans_all_notes_before_empty_fallback():
    provider, client = _provider_with_notes(_notes(150, marker_at=0, valid_state=False))
    reviewer = reviewer_module.PRReviewer.__new__(reviewer_module.PRReviewer)
    reviewer.git_provider = provider

    state = reviewer._load_review_finding_state()

    assert state.valid and not state.present
    assert client.http_request.call_count == 2


class _ReviewAI:
    main_pr_language = None

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        return ("review:\n"
                "  estimated_effort_to_review_[1-5]: 2\n"
                "  relevant_tests: |\n"
                "    No\n"
                "  security_concerns: |\n"
                "    No\n"
                "  key_issues_to_review: []\n", "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("use_lazy", [True, False], ids=["lazy", "original"])
@pytest.mark.parametrize(("count", "marker_at", "baseline_gets", "lazy_gets", "concurrent_note"), [
    (10, 0, 3, 3, False),
    (100, 0, 3, 3, False),
    (101, 0, 6, 3, False),
    (150, 0, 6, 3, False),
    (150, 0, 6, 3, True),
    (150, 105, 6, 6, False),
    (150, 149, 6, 6, False),
    (1000, 0, 30, 3, False),
    (1000, 999, 30, 30, False),
    (1000, None, 30, 30, False),
])
async def test_review_route_uses_fresh_lazy_notes_for_each_finding_and_publish_phase(
    monkeypatch, count, marker_at, baseline_gets, lazy_gets, concurrent_note, use_lazy
):
    """Run real request dispatch, reviewer finding preparation, and publication."""
    if not use_lazy:
        monkeypatch.delattr(GitLabProvider, "_get_persistent_comment_iterators")
    notes = _notes(count, marker_at=marker_at)
    provider, client = _provider_with_notes(notes)
    provider.mr.title = "Fix a minor bug"
    provider.get_languages = lambda: {"Python": 100}
    provider.get_files = lambda: ["app.py"]
    provider.get_num_of_files = lambda: 1
    provider.get_pr_branch = lambda: "feature/fix"
    provider.get_pr_description = lambda **kwargs: ("Fix bug", [])
    provider.get_commit_messages = lambda: "Fix bug"
    provider.get_pr_url = lambda: "https://gitlab.example.com/test/repo/-/merge_requests/1"
    provider.get_latest_commit_url = lambda: "https://gitlab.example.com/test/repo/-/commit/abc"
    provider.get_comment_url = lambda note: f"https://gitlab.example.com/test/repo/-/merge_requests/1#note_{note.id}"
    provider.get_diff_files = lambda: [FilePatchInfo(
        base_file="a = 0\n", head_file="a = 1\n", patch="@@ -1 +1 @@\n-a = 0\n+a = 1\n",
        filename="app.py", edit_type=EDIT_TYPE.MODIFIED)]
    edited = []
    published = []
    provider.edit_comment = lambda note, body: edited.append((note.id, body)) or True
    provider.publish_comment = lambda body, **kwargs: published.append(body) or SimpleNamespace(id=2000, body=body)
    provider.remove_comment = lambda _: None
    if concurrent_note:
        original_publish = provider.publish_persistent_comment_full

        def publish_after_concurrent_insert(body, **kwargs):
            notes.insert(0, {"id": count + 1, "body": notes[marker_at]["body"], "author": {"id": 7}})
            return original_publish(body, **kwargs)

        provider.publish_persistent_comment_full = publish_after_concurrent_insert

    settings = get_settings()
    monkeypatch.setitem(settings.config, "publish_output", True)
    monkeypatch.setitem(settings.config, "publish_output_progress", False)
    monkeypatch.setitem(settings.config, "is_auto_command", False)
    monkeypatch.setitem(settings.pr_reviewer, "persistent_comment", True)
    monkeypatch.setitem(settings.pr_reviewer, "persistent_finding_state", True)
    monkeypatch.setitem(settings.pr_reviewer, "inline_key_issues", False)
    monkeypatch.setitem(settings.pr_reviewer, "final_update_message", False)
    monkeypatch.setitem(settings.pr_reviewer, "enable_review_labels_effort", False)
    monkeypatch.setitem(settings.pr_reviewer, "enable_review_labels_security", False)
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda _url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda _args: (True, None))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)
    monkeypatch.setattr(reviewer_module, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", lambda _url: provider)
    monkeypatch.setattr(reviewer_module, "build_repo_context", lambda _provider: "")
    monkeypatch.setattr(reviewer_module, "get_skills_context", lambda: "")
    monkeypatch.setattr(reviewer_module.PRReviewer, "_load_dismissed_key_issues", lambda self: [])
    monkeypatch.setattr(reviewer_module, "get_pr_diff", lambda *args, **kwargs: ("## File: 'app.py'\n\n@@\n+a", []))

    async def skip_tickets(_provider, _vars):
        return None

    async def direct_model(prepare_fn, model_type=None, git_provider=None):
        return await prepare_fn("gpt-4o")

    monkeypatch.setattr(reviewer_module, "extract_and_cache_pr_tickets", skip_tickets)
    monkeypatch.setattr(reviewer_module, "retry_with_fallback_models", direct_model)

    handled = await pr_agent_module.PRAgent(ai_handler=_ReviewAI)._handle_request(
        "https://gitlab.example.com/test/repo/-/merge_requests/1", "/review"
    )

    assert handled is True
    assert client.http_request.call_count == (lazy_gets if use_lazy else baseline_gets)
    if marker_at is not None:
        assert len(edited) == 1
        assert edited[0][0] == (count + 1 if concurrent_note else count - marker_at)
    else:
        assert not edited
        assert published
