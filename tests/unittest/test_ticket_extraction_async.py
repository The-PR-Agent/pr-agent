"""
Unit tests for async ticket extraction & caching in
``pr_agent.tools.ticket_pr_compliance_check``.

These tests are deterministic and fake-provider based — no live API or
network access is performed.
"""
import asyncio

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider, GithubProvider
from pr_agent.tools import ticket_pr_compliance_check as tpc
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets,
    extract_tickets,
)
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _FakeLabel:
    def __init__(self, name):
        self.name = name


class _FakeIssue:
    def __init__(self, number, title="t", body="b", labels=None):
        self.number = number
        self.title = title
        self.body = body
        self.labels = labels if labels is not None else []


class _FakeRepoObj:
    """Mimics PyGithub Repository.get_issue lookup behaviour."""

    def __init__(self, issues_by_number=None, raise_for=None):
        self._issues = issues_by_number or {}
        self._raise_for = raise_for or set()

    def get_issue(self, number):
        if number in self._raise_for:
            raise RuntimeError(f"boom for issue {number}")
        if number not in self._issues:
            raise KeyError(f"unknown issue {number}")
        return self._issues[number]


class _FakeGithubClient:
    """Mimics PyGithub Github.get_repo lookup, counting calls."""

    def __init__(self, repos_by_name=None):
        self._repos = repos_by_name or {}
        self.get_repo_calls = []

    def get_repo(self, full_name):
        self.get_repo_calls.append(full_name)
        if full_name not in self._repos:
            raise RuntimeError(f"no access to repository {full_name}")
        return self._repos[full_name]


def _make_github_provider(
    *,
    user_description="",
    branch="main",
    repo="org/repo",
    base_url_html="https://github.com",
    repo_obj=None,
    sub_issues_map=None,
    sub_issues_raises=False,
    github_client=None,
):
    """Build a GithubProvider that passes ``isinstance`` checks without __init__."""
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = repo
    provider.base_url_html = base_url_html
    provider.repo_obj = repo_obj
    provider.github_client = github_client
    provider.get_user_description = lambda: user_description
    provider.get_pr_branch = lambda: branch

    sub_issues_map = sub_issues_map or {}

    def _fetch_sub_issues(ticket_url):
        if sub_issues_raises:
            raise RuntimeError("sub-issue fetch failed")
        return sub_issues_map.get(ticket_url, [])

    provider.fetch_sub_issues = _fetch_sub_issues
    return provider


def _make_azure_provider(work_items):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.get_linked_work_items = lambda: work_items
    return provider


# ---------------------------------------------------------------------------
# Settings snapshot helper
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_snapshot():
    """Snapshot and restore settings keys mutated by these tests.

    Uses the shared sentinel-based helpers so that keys originally absent
    (including the dotted ``pr_reviewer.require_ticket_analysis_review``
    leaf) are truly removed on restore — never left as a ``None`` value
    that would leak into subsequent tests.
    """
    s = get_settings()
    snapshot = snapshot_settings(
        ["related_tickets", "pr_reviewer.require_ticket_analysis_review"]
    )
    # Reset to known defaults for each test
    s.set("related_tickets", [])
    s.set("pr_reviewer.require_ticket_analysis_review", False)
    try:
        yield s
    finally:
        restore_settings(snapshot)


# ---------------------------------------------------------------------------
# Scenario 1: GitHub extraction merges description + branch, dedupes, caps
# ---------------------------------------------------------------------------

class TestGithubExtractionMerging:
    def test_branch_extraction_contributes_ticket_not_in_description(self, settings_snapshot):
        # Description mentions only #1; branch contributes #2. Without branch
        # extraction the result would be [1]; with it, [1, 2] (description first).
        desc = "Fixes #1"
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="One", body="body1"),
            2: _FakeIssue(2, title="Two", body="body2"),
        })
        provider = _make_github_provider(
            user_description=desc,
            branch="feature/2-dup",
            repo_obj=repo_obj,
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        ids = [t["ticket_id"] for t in result]
        # Order is meaningful: description-derived ticket first, then branch.
        assert ids == [1, 2]

    def test_branch_duplicate_is_deduped_against_description(self, settings_snapshot):
        # Description references both #1 and #2; branch also points at #2.
        # The branch duplicate must not produce a second entry for #2.
        desc = "Fixes #1 and addresses #2"
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="One", body="body1"),
            2: _FakeIssue(2, title="Two", body="body2"),
        })
        provider = _make_github_provider(
            user_description=desc,
            branch="feature/2-dup",
            repo_obj=repo_obj,
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        urls = [t["ticket_url"] for t in result]
        assert len(urls) == len(set(urls))
        ids = sorted(t["ticket_id"] for t in result)
        assert ids == [1, 2]

    def test_branch_only_extraction_produces_single_ticket(self, settings_snapshot):
        # Description carries no ticket references — the branch must still
        # surface its issue number on its own.
        repo_obj = _FakeRepoObj({
            77: _FakeIssue(77, title="From branch", body="bb"),
        })
        provider = _make_github_provider(
            user_description="No ticket reference here.",
            branch="feature/77-add-thing",
            repo_obj=repo_obj,
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        assert len(result) == 1
        assert result[0]["ticket_id"] == 77
        assert result[0]["ticket_url"].endswith("/issues/77")

    def test_caps_total_tickets_to_three(self, settings_snapshot):
        # Description has 3 explicit URLs; branch adds a 4th — total must be
        # capped at 3 and the dropped one must be the branch-derived #13.
        desc = (
            "See https://github.com/org/repo/issues/10 "
            "and https://github.com/org/repo/issues/11 "
            "and https://github.com/org/repo/issues/12"
        )
        repo_obj = _FakeRepoObj({
            10: _FakeIssue(10),
            11: _FakeIssue(11),
            12: _FakeIssue(12),
            13: _FakeIssue(13),
        })
        provider = _make_github_provider(
            user_description=desc,
            branch="feature/13-extra",
            repo_obj=repo_obj,
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        assert len(result) == 3
        ids = sorted(t["ticket_id"] for t in result)
        # The branch-derived #13 must be the one dropped: description tickets
        # come first in the merge order, so the cap drops the trailing entry.
        assert ids == [10, 11, 12]


# ---------------------------------------------------------------------------
# Scenario 1b: tickets are fetched from the repository that owns them
# ---------------------------------------------------------------------------

class TestCrossRepoTicketResolution:
    def test_ticket_in_other_repo_is_fetched_from_that_repo(self, settings_snapshot):
        # Both repositories happen to have an issue #5. The PR links the one in
        # ``other/repo``, so the ``other/repo`` issue must be the one returned —
        # not the same-numbered issue that exists in the PR's own repository.
        pr_repo_obj = _FakeRepoObj({5: _FakeIssue(5, title="Unrelated issue in PR repo")})
        other_repo_obj = _FakeRepoObj({5: _FakeIssue(5, title="Linked issue", body="linked")})
        provider = _make_github_provider(
            user_description="Relates to https://github.com/other/repo/issues/5",
            repo="org/repo",
            repo_obj=pr_repo_obj,
            github_client=_FakeGithubClient({"other/repo": other_repo_obj}),
        )
        result = asyncio.run(extract_tickets(provider))
        assert result and len(result) == 1
        assert result[0]["title"] == "Linked issue"
        assert provider.github_client.get_repo_calls == ["other/repo"]

    def test_same_repo_ticket_reuses_repo_obj_without_extra_api_call(self, settings_snapshot):
        repo_obj = _FakeRepoObj({1: _FakeIssue(1, title="One")})
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo="org/repo",
            repo_obj=repo_obj,
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert result and result[0]["title"] == "One"
        # The PR's own repository handle is reused — no repository lookup at all.
        assert provider.github_client.get_repo_calls == []

    def test_same_repo_differing_in_case_reuses_repo_obj(self, settings_snapshot):
        # GitHub repository names are case-insensitive, so a link spelled with
        # different case is the PR's own repository and must take the fast path.
        repo_obj = _FakeRepoObj({4: _FakeIssue(4, title="Four")})
        provider = _make_github_provider(
            user_description="See https://github.com/Org/Repo/issues/4",
            repo="org/repo",
            repo_obj=repo_obj,
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert [t["ticket_id"] for t in result] == [4]
        assert provider.github_client.get_repo_calls == []

    def test_unreachable_repo_is_skipped_without_failing_other_tickets(self, settings_snapshot):
        repo_obj = _FakeRepoObj({1: _FakeIssue(1, title="One")})
        provider = _make_github_provider(
            user_description="Fixes #1, see https://github.com/private/repo/issues/9",
            repo="org/repo",
            repo_obj=repo_obj,
            # ``private/repo`` is absent -> get_repo raises, e.g. no read access.
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        assert [t["ticket_id"] for t in result] == [1]

    def test_repeated_unreachable_repo_is_looked_up_once(self, settings_snapshot):
        # A failed lookup is cached as well, so several tickets pointing at one
        # inaccessible repository do not repeat the failing call.
        provider = _make_github_provider(
            user_description=(
                "See https://github.com/private/repo/issues/1 "
                "and https://github.com/private/repo/issues/2"
            ),
            repo="org/repo",
            repo_obj=_FakeRepoObj({}),
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert result == []
        assert provider.github_client.get_repo_calls == ["private/repo"]

    def test_repeated_foreign_repo_is_looked_up_once(self, settings_snapshot):
        other_repo_obj = _FakeRepoObj({
            5: _FakeIssue(5, title="Five"),
            6: _FakeIssue(6, title="Six"),
        })
        provider = _make_github_provider(
            user_description=(
                "See https://github.com/other/repo/issues/5 "
                "and https://github.com/other/repo/issues/6"
            ),
            repo="org/repo",
            repo_obj=_FakeRepoObj({}),
            github_client=_FakeGithubClient({"other/repo": other_repo_obj}),
        )
        result = asyncio.run(extract_tickets(provider))
        assert sorted(t["ticket_id"] for t in result) == [5, 6]
        assert provider.github_client.get_repo_calls == ["other/repo"]

    def test_ticket_on_another_github_host_is_skipped_not_read_from_pr_repo(self, settings_snapshot):
        # ``_parse_issue_url`` drops the host, so this ticket parses to the same
        # "org/repo" as the PR. It lives on a different GitHub instance, which the
        # PR's client cannot reach, so it must be skipped rather than served from
        # the PR's own repository.
        pr_repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="One"),
            7: _FakeIssue(7, title="Unrelated issue on the PR's host"),
        })
        provider = _make_github_provider(
            user_description="Fixes #1, see https://github.enterprise.local/org/repo/issues/7",
            repo="org/repo",
            base_url_html="https://github.com",
            repo_obj=pr_repo_obj,
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert [t["ticket_id"] for t in result] == [1]
        # Nor may it fall through to a lookup on the PR's (wrong) instance.
        assert provider.github_client.get_repo_calls == []

    def test_api_host_form_counts_as_the_same_instance(self, settings_snapshot):
        # Sub-issue URLs may arrive in api.github.com form; that is the same
        # instance as the PR's https://github.com and must not be rejected.
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="Main"),
            99: _FakeIssue(99, title="Sub", body="s"),
        })
        sub_url = "https://api.github.com/repos/org/repo/issues/99"
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo="org/repo",
            base_url_html="https://github.com",
            repo_obj=repo_obj,
            github_client=_FakeGithubClient(),
            sub_issues_map={"https://github.com/org/repo/issues/1": [sub_url]},
        )
        result = asyncio.run(extract_tickets(provider))
        subs = result[0]["sub_issues"]
        assert [s["title"] for s in subs] == ["Sub"]
        assert provider.github_client.get_repo_calls == []

    def test_explicit_default_port_is_the_same_instance(self, settings_snapshot):
        # An explicit port must not make the host check reject an otherwise local
        # ticket — hosts are compared without port or userinfo.
        repo_obj = _FakeRepoObj({7: _FakeIssue(7, title="Seven")})
        provider = _make_github_provider(
            user_description="See https://github.com:443/org/repo/issues/7",
            repo="org/repo",
            base_url_html="https://github.com",
            repo_obj=repo_obj,
            github_client=_FakeGithubClient(),
        )
        result = asyncio.run(extract_tickets(provider))
        assert [t["ticket_id"] for t in result] == [7]
        assert provider.github_client.get_repo_calls == []

    def test_sub_issue_in_other_repo_is_fetched_from_that_repo(self, settings_snapshot):
        pr_repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="Main"),
            99: _FakeIssue(99, title="Unrelated issue in PR repo"),
        })
        other_repo_obj = _FakeRepoObj({99: _FakeIssue(99, title="Linked sub-issue", body="s")})
        sub_url = "https://github.com/other/repo/issues/99"
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo="org/repo",
            repo_obj=pr_repo_obj,
            github_client=_FakeGithubClient({"other/repo": other_repo_obj}),
            sub_issues_map={"https://github.com/org/repo/issues/1": [sub_url]},
        )
        result = asyncio.run(extract_tickets(provider))
        subs = result[0]["sub_issues"]
        assert len(subs) == 1
        assert subs[0]["title"] == "Linked sub-issue"
        assert provider.github_client.get_repo_calls == ["other/repo"]


# ---------------------------------------------------------------------------
# Scenario 2: Long body truncation
# ---------------------------------------------------------------------------

class TestBodyTruncation:
    def test_main_issue_body_truncated_to_10000_chars_plus_ellipsis(self, settings_snapshot):
        long_body = "x" * 10500
        repo_obj = _FakeRepoObj({1: _FakeIssue(1, body=long_body)})
        provider = _make_github_provider(
            user_description="Fixes #1", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result and len(result) == 1
        body = result[0]["body"]
        assert body.endswith("...")
        assert len(body) == 10000 + len("...")

    def test_short_body_not_truncated(self, settings_snapshot):
        repo_obj = _FakeRepoObj({1: _FakeIssue(1, body="short")})
        provider = _make_github_provider(
            user_description="Fixes #1", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result[0]["body"] == "short"


# ---------------------------------------------------------------------------
# Scenario 3: get_issue failure on one ticket does not block others
# ---------------------------------------------------------------------------

class TestGetIssueFailureIsolated:
    def test_failure_on_one_issue_does_not_break_others(self, settings_snapshot):
        repo_obj = _FakeRepoObj(
            issues_by_number={2: _FakeIssue(2, title="Two")},
            raise_for={1},
        )
        provider = _make_github_provider(
            user_description="Fixes #1 and #2", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result is not None
        ids = [t["ticket_id"] for t in result]
        assert ids == [2]


# ---------------------------------------------------------------------------
# Scenario 4 + 5: sub-issue fetch success and exception handling
# ---------------------------------------------------------------------------

class TestSubIssues:
    def test_sub_issue_success_populates_and_truncates(self, settings_snapshot):
        long_sub_body = "y" * 10500
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, title="Main", body="m"),
            99: _FakeIssue(99, title="Sub", body=long_sub_body),
        })
        sub_url = "https://github.com/org/repo/issues/99"
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo_obj=repo_obj,
            sub_issues_map={"https://github.com/org/repo/issues/1": [sub_url]},
        )
        result = asyncio.run(extract_tickets(provider))
        assert result and len(result) == 1
        subs = result[0]["sub_issues"]
        assert len(subs) == 1
        assert subs[0]["ticket_url"] == sub_url
        assert subs[0]["title"] == "Sub"
        assert subs[0]["body"].endswith("...")
        assert len(subs[0]["body"]) == 10000 + len("...")

    def test_sub_issue_fetch_exception_yields_empty_sub_issues(self, settings_snapshot):
        repo_obj = _FakeRepoObj({1: _FakeIssue(1, title="Main", body="m")})
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo_obj=repo_obj,
            sub_issues_raises=True,
        )
        result = asyncio.run(extract_tickets(provider))
        assert result and len(result) == 1
        assert result[0]["sub_issues"] == []

    def test_single_sub_issue_failure_does_not_break_others(self, settings_snapshot):
        repo_obj = _FakeRepoObj(
            issues_by_number={
                1: _FakeIssue(1, title="Main"),
                99: _FakeIssue(99, title="OK", body="ok"),
            },
            raise_for={50},
        )
        sub_bad = "https://github.com/org/repo/issues/50"
        sub_good = "https://github.com/org/repo/issues/99"
        provider = _make_github_provider(
            user_description="Fixes #1",
            repo_obj=repo_obj,
            sub_issues_map={
                "https://github.com/org/repo/issues/1": [sub_bad, sub_good]
            },
        )
        result = asyncio.run(extract_tickets(provider))
        subs = result[0]["sub_issues"]
        assert [s["ticket_url"] for s in subs] == [sub_good]


# ---------------------------------------------------------------------------
# Scenario 6: labels — supports both object-style and string-style
# ---------------------------------------------------------------------------

class TestLabelExtraction:
    def test_object_labels_extracted_by_name(self, settings_snapshot):
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, labels=[_FakeLabel("bug"), _FakeLabel("urgent")]),
        })
        provider = _make_github_provider(
            user_description="Fixes #1", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result[0]["labels"] == "bug, urgent"

    def test_string_labels_also_supported(self, settings_snapshot):
        repo_obj = _FakeRepoObj({
            1: _FakeIssue(1, labels=["bug", "urgent"]),
        })
        provider = _make_github_provider(
            user_description="Fixes #1", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result[0]["labels"] == "bug, urgent"

    def test_label_iteration_failure_yields_empty_labels(self, settings_snapshot):
        class _Boom:
            def __iter__(self):
                raise RuntimeError("nope")

        issue = _FakeIssue(1)
        issue.labels = _Boom()
        repo_obj = _FakeRepoObj({1: issue})
        provider = _make_github_provider(
            user_description="Fixes #1", repo_obj=repo_obj
        )
        result = asyncio.run(extract_tickets(provider))
        assert result[0]["labels"] == ""


# ---------------------------------------------------------------------------
# Scenario 7: Azure DevOps linked work items mapping
# ---------------------------------------------------------------------------

class TestAzureDevopsExtraction:
    def test_linked_work_items_mapped_with_truncation(self, settings_snapshot):
        long_body = "z" * 10500
        work_items = [
            {
                "id": 1,
                "url": "https://dev.azure.com/o/p/_workitems/edit/1",
                "title": "WI 1",
                "body": long_body,
                "acceptance_criteria": "AC1",
                "labels": ["a", "b"],
            },
            {
                "id": 2,
                "url": "https://dev.azure.com/o/p/_workitems/edit/2",
                "title": "WI 2",
                "body": "short",
                "labels": [],
            },
        ]
        provider = _make_azure_provider(work_items)
        result = asyncio.run(extract_tickets(provider))
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["ticket_id"] == 1
        assert result[0]["title"] == "WI 1"
        assert result[0]["body"].endswith("...")
        assert len(result[0]["body"]) == 10000 + len("...")
        assert result[0]["requirements"] == "AC1"
        assert result[0]["labels"] == "a, b"
        assert result[1]["body"] == "short"
        assert result[1]["labels"] == ""
        assert result[1].get("requirements", "") == ""


# ---------------------------------------------------------------------------
# Scenario 11: Unsupported provider returns None per current contract
# ---------------------------------------------------------------------------

class TestUnsupportedProvider:
    def test_non_github_non_azure_provider_returns_none(self, settings_snapshot):
        class _OtherProvider:
            pass

        result = asyncio.run(extract_tickets(_OtherProvider()))
        # Current contract: function returns implicit None for unsupported providers
        assert result is None


# ---------------------------------------------------------------------------
# Scenarios 8-10: extract_and_cache_pr_tickets behavior
# ---------------------------------------------------------------------------

class TestExtractAndCachePrTickets:
    def test_review_setting_disabled_returns_without_provider_calls(
        self, settings_snapshot
    ):
        settings_snapshot.set("pr_reviewer.require_ticket_analysis_review", False)
        calls = {"n": 0}

        class _Tripwire:
            def __getattr__(self, name):
                calls["n"] += 1
                raise AttributeError(
                    f"Provider should not be touched (attr={name})"
                )

        vars_ = {}
        result = asyncio.run(extract_and_cache_pr_tickets(_Tripwire(), vars_))
        assert result is None
        assert calls["n"] == 0
        assert "related_tickets" not in vars_

    def test_uses_existing_related_tickets_cache_without_extract(
        self, settings_snapshot, monkeypatch
    ):
        settings_snapshot.set("pr_reviewer.require_ticket_analysis_review", True)
        cached = [{"ticket_id": 42, "title": "cached"}]
        settings_snapshot.set("related_tickets", cached)

        async def _boom(_):
            raise AssertionError("extract_tickets should not be called when cache is set")

        monkeypatch.setattr(tpc, "extract_tickets", _boom)

        vars_ = {}
        # Provider value irrelevant — should never be used
        asyncio.run(extract_and_cache_pr_tickets(object(), vars_))
        assert vars_["related_tickets"] == cached

    def test_stores_sub_issues_before_main_issue_in_related_tickets(
        self, settings_snapshot, monkeypatch
    ):
        settings_snapshot.set("pr_reviewer.require_ticket_analysis_review", True)
        settings_snapshot.set("related_tickets", [])

        sub_a = {"ticket_url": "u/sub_a", "title": "sub_a", "body": "s1"}
        sub_b = {"ticket_url": "u/sub_b", "title": "sub_b", "body": "s2"}
        main_ticket = {
            "ticket_id": 1,
            "ticket_url": "u/main",
            "title": "main",
            "body": "m",
            "labels": "",
            "sub_issues": [sub_a, sub_b],
        }

        async def _fake_extract(_):
            return [main_ticket]

        monkeypatch.setattr(tpc, "extract_tickets", _fake_extract)

        vars_ = {}
        asyncio.run(extract_and_cache_pr_tickets(object(), vars_))

        # Per current production order: sub-issues are appended first, then main.
        stored = vars_["related_tickets"]
        assert stored == [sub_a, sub_b, main_ticket]
        # Settings cache is also populated
        assert get_settings().get("related_tickets") == stored

    def test_no_tickets_extracted_leaves_vars_untouched(
        self, settings_snapshot, monkeypatch
    ):
        settings_snapshot.set("pr_reviewer.require_ticket_analysis_review", True)
        settings_snapshot.set("related_tickets", [])

        async def _empty(_):
            return []

        monkeypatch.setattr(tpc, "extract_tickets", _empty)

        vars_ = {}
        asyncio.run(extract_and_cache_pr_tickets(object(), vars_))
        assert "related_tickets" not in vars_
