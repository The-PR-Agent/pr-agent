"""
Unit tests for Asana ticket detection in ticket_pr_compliance_check.py.

Tests cover:
- Legacy and current Asana URL detection
- Authenticated task fetching and provider integration
- Edge cases (mixed content, no tickets, duplicates, API failures)
"""
import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools import ticket_pr_compliance_check as tpc
from tests.unittest import _settings_helpers


class _Issue:
    def __init__(self, number):
        self.number = number
        self.title = f"Issue {number}"
        self.body = f"Issue {number} body"
        self.labels = []


class _Repo:
    def get_issue(self, number):
        return _Issue(number)


class _PartiallyFailingRepo:
    def get_issue(self, number):
        if number == 2:
            raise RuntimeError("issue unavailable")
        return _Issue(number)


class _FirstTwoFailingRepo:
    def get_issue(self, number):
        if number in {1, 2}:
            raise RuntimeError("issue unavailable")
        return _Issue(number)


def _make_github_provider(description, repo_obj=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url_html = "https://github.com"
    provider.repo_obj = repo_obj if repo_obj is not None else _Repo()
    provider.get_user_description = lambda: description
    provider.get_pr_branch = lambda: ""
    provider._parse_issue_url = lambda ticket: (
        provider.repo,
        int(ticket.rsplit("/", 1)[-1]),
    )
    provider.fetch_sub_issues = lambda _ticket: []
    return provider


class _GenericProvider:
    def __init__(self, description):
        self.description = description

    def get_user_description(self):
        return self.description

    def get_pr_branch(self):
        return ""


class _FailingProvider:
    def get_user_description(self):
        raise RuntimeError("description unavailable")


def _make_azure_provider(description, work_items):
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.get_user_description = lambda: description
    provider.get_linked_work_items = lambda: work_items
    return provider


@pytest.fixture
def asana_fetch_stub(monkeypatch):
    settings_snapshot = _settings_helpers.snapshot_settings(
        ["asana.api_token", "asana.request_timeout"]
    )
    get_settings().set("asana.api_token", "test-token")

    async def _fake_fetch(_session, ticket_url, _max_body_characters):
        task_gid = tpc._get_asana_task_gid(ticket_url)
        return {
            "ticket_id": task_gid,
            "ticket_url": ticket_url,
            "title": f"Asana task {task_gid}",
            "body": f"Task notes {task_gid}",
            "labels": "backend",
        }

    monkeypatch.setattr(tpc, "_fetch_asana_ticket_content", _fake_fetch)
    try:
        yield
    finally:
        _settings_helpers.restore_settings(settings_snapshot)


@pytest.mark.usefixtures("asana_fetch_stub")
class TestFindAsanaTickets:
    """Tests for find_asana_tickets()."""

    def test_detects_full_asana_url(self):
        """Legacy Asana task URLs should be detected."""
        text = "See https://app.asana.com/0/123456/789012 for details"
        tickets = tpc.find_asana_tickets(text)
        assert "https://app.asana.com/0/123456/789012" in tickets

    def test_detects_current_asana_permalinks(self):
        text = (
            "See https://app.asana.com/1/12345/task/111111 "
            "and https://app.asana.com/1/12345/project/98765/task/222222 "
            "and https://app.asana.com/1/12345/home/task/333333 "
            "and https://app.asana.com/1/12345/project/98765/task/444444/comment/555555"
        )

        assert tpc.find_asana_tickets(text) == [
            "https://app.asana.com/1/12345/task/111111",
            "https://app.asana.com/1/12345/project/98765/task/222222",
            "https://app.asana.com/1/12345/home/task/333333",
            "https://app.asana.com/1/12345/project/98765/task/444444/comment/555555",
        ]

    def test_detects_focus_query_and_trailing_slash(self):
        text = (
            "https://app.asana.com/1/12345/home/task/111111/?focus=true "
            "https://app.asana.com/1/12345/task/222222?focus=true"
        )

        assert tpc.find_asana_tickets(text) == [
            "https://app.asana.com/1/12345/home/task/111111/",
            "https://app.asana.com/1/12345/task/222222",
        ]

    def test_detects_multiple_urls(self):
        """Multiple Asana URLs should all be found."""
        text = (
            "See https://app.asana.com/0/11/111111111111"
            " and https://app.asana.com/0/22/333333333333"
        )
        tickets = tpc.find_asana_tickets(text)
        assert len(tickets) == 2

    def test_deduplicates_identical_urls(self):
        """Duplicate references to the same URL should be deduplicated."""
        text = (
            "https://app.asana.com/0/1/123456789012"
            " mentioned twice: https://app.asana.com/0/1/123456789012"
        )
        tickets = tpc.find_asana_tickets(text)
        assert len(tickets) == 1

    def test_deduplicates_same_task_across_url_formats(self):
        text = (
            "https://app.asana.com/0/111/999999 first, "
            "https://app.asana.com/0/222/999999 second, "
            "https://app.asana.com/1/333/task/999999 third"
        )

        assert tpc.find_asana_tickets(text) == ["https://app.asana.com/0/111/999999"]

    def test_returns_empty_for_no_tickets(self):
        """Text without Asana references returns an empty list."""
        text = "No tickets here, just regular text"
        tickets = tpc.find_asana_tickets(text)
        assert tickets == []

    def test_returns_empty_for_empty_string(self):
        """Empty string returns an empty list."""
        tickets = tpc.find_asana_tickets("")
        assert tickets == []

    def test_returns_empty_for_none_input(self):
        """None input returns an empty list."""
        tickets = tpc.find_asana_tickets(None)
        assert tickets == []

    def test_ignores_github_urls(self):
        """GitHub issue URLs should not be mistaken for Asana tickets."""
        text = "Fix https://github.com/owner/repo/issues/42"
        tickets = tpc.find_asana_tickets(text)
        assert tickets == []

    def test_tickets_preserve_first_seen_order(self):
        text = (
            "https://app.asana.com/0/2/222222222222"
            " https://app.asana.com/0/1/111111111111"
        )
        tickets = tpc.find_asana_tickets(text)
        assert tickets == [
            "https://app.asana.com/0/2/222222222222",
            "https://app.asana.com/0/1/111111111111",
        ]

    def test_tickets_in_pr_description_mixed_content(self):
        """Asana tickets mixed with other content in a PR description."""
        text = """## Summary
        Related to https://app.asana.com/0/99/888888888888
        and https://app.asana.com/0/77/777777777777

        Also see GitHub issue #42
        """
        tickets = tpc.find_asana_tickets(text)
        assert len(tickets) == 2

    @pytest.mark.asyncio
    async def test_extract_tickets_includes_asana_reference(self):
        """extract_tickets() should include Asana references in ticket content."""
        provider = _make_github_provider(
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await tpc.extract_tickets(provider)

        assert tickets == [
            {
                "ticket_id": "888888888888",
                "ticket_url": "https://app.asana.com/0/99/888888888888",
                "title": "Asana task 888888888888",
                "body": "Task notes 888888888888",
                "labels": "backend",
            }
        ]

    @pytest.mark.asyncio
    async def test_extract_tickets_adds_asana_without_displacing_github(self):
        """Asana context should not displace GitHub's three existing ticket slots."""
        provider = _make_github_provider(
            "Fixes #1 and #2 and #3. "
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await tpc.extract_tickets(provider)

        assert [ticket["ticket_url"] for ticket in tickets] == [
            "https://github.com/owner/repo/issues/1",
            "https://github.com/owner/repo/issues/2",
            "https://github.com/owner/repo/issues/3",
            "https://app.asana.com/0/99/888888888888",
        ]

    @pytest.mark.asyncio
    async def test_extract_tickets_backfills_with_asana_when_truncated(self):
        """Ticket truncation should still return up to 3 available Asana tickets."""
        provider = _make_github_provider(
            "Related Asana tasks: "
            "https://app.asana.com/0/99/111111111111 "
            "https://app.asana.com/0/99/222222222222 "
            "https://app.asana.com/0/99/333333333333 "
            "https://app.asana.com/0/99/444444444444"
        )

        tickets = await tpc.extract_tickets(provider)

        assert len(tickets) == 3
        assert all(
            ticket["ticket_url"].startswith("https://app.asana.com/")
            for ticket in tickets
        )

    @pytest.mark.asyncio
    async def test_extract_tickets_backfills_asana_after_github_fetch_failure(self):
        """Asana tickets should fill available slots after GitHub issue fetch failures."""
        provider = _make_github_provider(
            "Fixes #1 and #2. "
            "Related Asana tasks: "
            "https://app.asana.com/0/99/111111111111 "
            "https://app.asana.com/0/99/222222222222 "
            "https://app.asana.com/0/99/333333333333",
            repo_obj=_PartiallyFailingRepo(),
        )

        tickets = await tpc.extract_tickets(provider)

        assert len(tickets) == 4
        assert tickets[0]["ticket_url"] == "https://github.com/owner/repo/issues/1"
        assert [
            ticket["ticket_url"] for ticket in tickets[1:]
        ] == [
            "https://app.asana.com/0/99/111111111111",
            "https://app.asana.com/0/99/222222222222",
            "https://app.asana.com/0/99/333333333333",
        ]

    @pytest.mark.asyncio
    async def test_asana_addition_does_not_skip_later_github_candidate(self):
        """Adding Asana must not reduce the existing GitHub attempt budget."""
        provider = _make_github_provider(
            "Fixes #1 and #2 and #3. "
            "Related Asana task: https://app.asana.com/0/99/111111111111",
            repo_obj=_FirstTwoFailingRepo(),
        )

        tickets = await tpc.extract_tickets(provider)

        assert [ticket["ticket_url"] for ticket in tickets] == [
            "https://github.com/owner/repo/issues/3",
            "https://app.asana.com/0/99/111111111111",
        ]

    @pytest.mark.asyncio
    async def test_extract_tickets_includes_asana_for_non_github_provider(self):
        """Asana detection should not be limited to the GitHub provider path."""
        provider = _GenericProvider(
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await tpc.extract_tickets(provider)

        assert tickets == [
            {
                "ticket_id": "888888888888",
                "ticket_url": "https://app.asana.com/0/99/888888888888",
                "title": "Asana task 888888888888",
                "body": "Task notes 888888888888",
                "labels": "backend",
            }
        ]

    @pytest.mark.asyncio
    async def test_extract_tickets_preserves_unsupported_provider_contract(self):
        """A non-ticket provider without Asana references remains unsupported."""
        provider = _GenericProvider("No related ticket references.")

        tickets = await tpc.extract_tickets(provider)

        assert tickets is None

    @pytest.mark.asyncio
    async def test_asana_description_error_is_isolated(self):
        """An optional Asana scan failure should not invent provider support."""
        tickets = await tpc.extract_tickets(_FailingProvider())

        assert tickets is None

    @pytest.mark.asyncio
    async def test_extract_tickets_caps_non_github_asana_references(self):
        """Provider-agnostic Asana fallback should keep ticket context bounded."""
        provider = _GenericProvider(
            "Related Asana tasks: "
            "https://app.asana.com/0/99/111111111111 "
            "https://app.asana.com/0/99/222222222222 "
            "https://app.asana.com/0/99/333333333333 "
            "https://app.asana.com/0/99/444444444444"
        )

        tickets = await tpc.extract_tickets(provider)

        assert len(tickets) == 3
        assert all(
            ticket["ticket_url"].startswith("https://app.asana.com/")
            for ticket in tickets
        )

    @pytest.mark.asyncio
    async def test_extract_tickets_adds_asana_without_truncating_azure(self):
        """Azure work items remain intact while Asana additions keep their own cap."""
        provider = _make_azure_provider(
            "Related Asana tasks: "
            "https://app.asana.com/0/99/111111111111 "
            "https://app.asana.com/0/99/222222222222 "
            "https://app.asana.com/0/99/333333333333 "
            "https://app.asana.com/0/99/444444444444",
            [
                {
                    "id": 1,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/1",
                    "title": "Issue 1",
                    "body": "Issue 1 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 2,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/2",
                    "title": "Issue 2",
                    "body": "Issue 2 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 3,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/3",
                    "title": "Issue 3",
                    "body": "Issue 3 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 4,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/4",
                    "title": "Issue 4",
                    "body": "Issue 4 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
            ],
        )

        tickets = await tpc.extract_tickets(provider)

        assert len(tickets) == 7
        assert [ticket["ticket_id"] for ticket in tickets[:4]] == [1, 2, 3, 4]
        assert [ticket["ticket_url"] for ticket in tickets[-3:]] == [
            "https://app.asana.com/0/99/111111111111",
            "https://app.asana.com/0/99/222222222222",
            "https://app.asana.com/0/99/333333333333",
        ]

    @pytest.mark.asyncio
    async def test_extract_tickets_preserves_all_azure_work_items_without_asana(self):
        """Azure-only extraction should preserve its pre-Asana behavior."""
        provider = _make_azure_provider(
            "",
            [
                {
                    "id": 1,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/1",
                    "title": "Issue 1",
                    "body": "Issue 1 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 2,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/2",
                    "title": "Issue 2",
                    "body": "Issue 2 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 3,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/3",
                    "title": "Issue 3",
                    "body": "Issue 3 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
                {
                    "id": 4,
                    "url": "https://dev.azure.com/org/project/_workitems/edit/4",
                    "title": "Issue 4",
                    "body": "Issue 4 body",
                    "acceptance_criteria": "",
                    "labels": [],
                },
            ],
        )

        tickets = await tpc.extract_tickets(provider)

        assert len(tickets) == 4
        assert [ticket["ticket_id"] for ticket in tickets] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_missing_token_does_not_create_placeholder_ticket(self):
        get_settings().set("asana.api_token", "")
        provider = _GenericProvider(
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        assert await tpc.extract_tickets(provider) == []

    @pytest.mark.asyncio
    async def test_asana_fetch_failure_does_not_displace_github_ticket(self, monkeypatch):
        async def _failing_fetch(*_args, **_kwargs):
            raise RuntimeError("task unavailable")

        monkeypatch.setattr(tpc, "_fetch_asana_ticket_content", _failing_fetch)
        provider = _make_github_provider(
            "Fixes #1 and #2 and #3. "
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await tpc.extract_tickets(provider)

        assert [ticket["ticket_id"] for ticket in tickets] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_asana_fetch_attempts_are_bounded_when_tasks_fail(self, monkeypatch):
        attempted_urls = []

        async def _failing_fetch(_session, ticket_url, _max_body_characters):
            attempted_urls.append(ticket_url)
            raise RuntimeError("task unavailable")

        monkeypatch.setattr(tpc, "_fetch_asana_ticket_content", _failing_fetch)
        ticket_urls = [
            f"https://app.asana.com/0/99/{task_gid}"
            for task_gid in range(111111111111, 111111111119)
        ]

        tickets = await tpc._fetch_asana_ticket_contents(ticket_urls, 3, 10000)

        assert tickets == []
        assert attempted_urls == ticket_urls[:3]

    @pytest.mark.asyncio
    async def test_invalid_asana_url_does_not_abort_valid_batch_entry(self, monkeypatch):
        attempted_urls = []

        async def _recording_fetch(_session, ticket_url, _max_body_characters):
            attempted_urls.append(ticket_url)
            task_gid = tpc._get_asana_task_gid(ticket_url)
            return {
                "ticket_id": task_gid,
                "ticket_url": ticket_url,
                "title": f"Asana task {task_gid}",
                "body": "",
                "labels": "",
            }

        monkeypatch.setattr(tpc, "_fetch_asana_ticket_content", _recording_fetch)
        valid_url = "https://app.asana.com/0/99/888888888888"

        tickets = await tpc._fetch_asana_ticket_contents(
            ["https://app.asana.com/not-a-task", valid_url],
            2,
            10000,
        )

        assert [ticket["ticket_id"] for ticket in tickets] == ["888888888888"]
        assert attempted_urls == [valid_url]

    @pytest.mark.asyncio
    async def test_asana_api_uses_bearer_token(self, monkeypatch):
        captured = {}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        def _client_session(*, headers, timeout):
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _Session()

        monkeypatch.setattr(tpc.aiohttp, "ClientSession", _client_session)

        tickets = await tpc._fetch_asana_ticket_contents(
            ["https://app.asana.com/0/99/888888888888"],
            1,
            10000,
        )

        assert tickets[0]["ticket_id"] == "888888888888"
        assert captured["headers"] == {"Authorization": "Bearer test-token"}
        assert captured["timeout"].total == 10

    @pytest.mark.parametrize(
        ("configured_timeout", "expected_timeout"),
        [
            (float("inf"), tpc.DEFAULT_ASANA_REQUEST_TIMEOUT),
            (float("nan"), tpc.DEFAULT_ASANA_REQUEST_TIMEOUT),
            (-1, tpc.DEFAULT_ASANA_REQUEST_TIMEOUT),
            (10**1000, tpc.DEFAULT_ASANA_REQUEST_TIMEOUT),
            (600, tpc.MAX_ASANA_REQUEST_TIMEOUT),
        ],
    )
    def test_asana_request_timeout_is_finite_and_bounded(
        self, configured_timeout, expected_timeout
    ):
        get_settings().set("asana.request_timeout", configured_timeout)

        assert tpc._get_asana_request_timeout() == expected_timeout


class _AsanaResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _AsanaSession:
    def __init__(self, response):
        self.response = response
        self.request_url = None
        self.request_params = None

    def get(self, request_url, params):
        self.request_url = request_url
        self.request_params = params
        return self.response


class TestFetchAsanaTicketContent:
    @pytest.mark.asyncio
    async def test_maps_real_task_fields_and_truncates_notes(self):
        response = _AsanaResponse(
            200,
            {
                "data": {
                    "gid": "888888888888",
                    "name": "Fix login race",
                    "notes": "x" * 25,
                    "permalink_url": "https://app.asana.com/1/42/task/888888888888",
                    "tags": [{"name": "backend"}, {"name": "urgent"}],
                }
            },
        )
        session = _AsanaSession(response)

        ticket = await tpc._fetch_asana_ticket_content(
            session,
            "https://app.asana.com/0/99/888888888888",
            20,
        )

        assert session.request_url == "https://app.asana.com/api/1.0/tasks/888888888888"
        assert session.request_params == {"opt_fields": tpc.ASANA_TASK_OPT_FIELDS}
        assert ticket == {
            "ticket_id": "888888888888",
            "ticket_url": "https://app.asana.com/1/42/task/888888888888",
            "title": "Fix login race",
            "body": "x" * 20 + "...",
            "labels": "backend, urgent",
        }

    @pytest.mark.asyncio
    async def test_non_success_response_raises_without_placeholder(self):
        session = _AsanaSession(_AsanaResponse(403, {"errors": []}))

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await tpc._fetch_asana_ticket_content(
                session,
                "https://app.asana.com/1/42/task/888888888888",
                10000,
            )
