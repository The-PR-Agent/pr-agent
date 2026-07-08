"""
Unit tests for Asana ticket detection in ticket_pr_compliance_check.py.

Tests cover:
- Full Asana URL detection
- Edge cases (mixed content, no tickets, duplicates)
"""
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.ticket_pr_compliance_check import extract_tickets, find_asana_tickets


class _Issue:
    def __init__(self, number):
        self.number = number
        self.title = f"Issue {number}"
        self.body = f"Issue {number} body"
        self.labels = []


class _Repo:
    def get_issue(self, number):
        return _Issue(number)


class _GithubProvider(GithubProvider):
    repo = "owner/repo"
    base_url_html = "https://github.com"
    repo_obj = _Repo()

    def __init__(self, description):
        self.description = description

    def get_user_description(self):
        return self.description

    def get_pr_branch(self):
        return ""

    def _parse_issue_url(self, ticket):
        return self.repo, int(ticket.rsplit("/", 1)[-1])

    def fetch_sub_issues(self, ticket):
        return []


class TestFindAsanaTickets:
    """Tests for find_asana_tickets()."""

    def test_detects_full_asana_url(self):
        """Full Asana task URLs should be detected."""
        text = "See https://app.asana.com/0/123456/789012 for details"
        tickets = find_asana_tickets(text)
        assert "https://app.asana.com/0/123456/789012" in tickets

    def test_detects_multiple_urls(self):
        """Multiple Asana URLs should all be found."""
        text = (
            "See https://app.asana.com/0/11/111111111111"
            " and https://app.asana.com/0/22/333333333333"
        )
        tickets = find_asana_tickets(text)
        assert len(tickets) == 2

    def test_deduplicates_identical_urls(self):
        """Duplicate references to the same URL should be deduplicated."""
        text = (
            "https://app.asana.com/0/1/123456789012"
            " mentioned twice: https://app.asana.com/0/1/123456789012"
        )
        tickets = find_asana_tickets(text)
        assert len(tickets) == 1

    def test_returns_empty_for_no_tickets(self):
        """Text without Asana references returns an empty list."""
        text = "No tickets here, just regular text"
        tickets = find_asana_tickets(text)
        assert tickets == []

    def test_returns_empty_for_empty_string(self):
        """Empty string returns an empty list."""
        tickets = find_asana_tickets("")
        assert tickets == []

    def test_returns_empty_for_none_input(self):
        """None input returns an empty list."""
        tickets = find_asana_tickets(None)
        assert tickets == []

    def test_ignores_github_urls(self):
        """GitHub issue URLs should not be mistaken for Asana tickets."""
        text = "Fix https://github.com/owner/repo/issues/42"
        tickets = find_asana_tickets(text)
        assert tickets == []

    def test_tickets_are_sorted(self):
        """Returned list should be sorted alphabetically."""
        text = (
            "https://app.asana.com/0/2/222222222222"
            " https://app.asana.com/0/1/111111111111"
        )
        tickets = find_asana_tickets(text)
        assert tickets == sorted(tickets)

    def test_tickets_in_pr_description_mixed_content(self):
        """Asana tickets mixed with other content in a PR description."""
        text = """## Summary
        Related to https://app.asana.com/0/99/888888888888
        and https://app.asana.com/0/77/777777777777

        Also see GitHub issue #42
        """
        tickets = find_asana_tickets(text)
        assert len(tickets) == 2

    async def test_extract_tickets_includes_asana_reference(self):
        """extract_tickets() should include Asana references in ticket content."""
        provider = _GithubProvider(
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await extract_tickets(provider)

        assert tickets == [
            {
                "ticket_id": "https://app.asana.com/0/99/888888888888",
                "ticket_url": "https://app.asana.com/0/99/888888888888",
                "title": "Asana Task: https://app.asana.com/0/99/888888888888",
                "body": (
                    "Asana task referenced in PR description. "
                    "Fetch task details from Asana for full context."
                ),
                "labels": "",
            }
        ]

    async def test_extract_tickets_reserves_slot_for_asana_when_truncated(self):
        """Asana references should not be dropped when the ticket list is capped."""
        provider = _GithubProvider(
            "Fixes #1 and #2 and #3. "
            "Related Asana task: https://app.asana.com/0/99/888888888888"
        )

        tickets = await extract_tickets(provider)

        assert len(tickets) == 3
        asana_tickets = [
            ticket for ticket in tickets
            if ticket["ticket_url"].startswith("https://app.asana.com/")
        ]
        assert len(asana_tickets) == 1

    async def test_extract_tickets_backfills_with_asana_when_truncated(self):
        """Ticket truncation should still return up to 3 available Asana tickets."""
        provider = _GithubProvider(
            "Related Asana tasks: "
            "https://app.asana.com/0/99/111111111111 "
            "https://app.asana.com/0/99/222222222222 "
            "https://app.asana.com/0/99/333333333333 "
            "https://app.asana.com/0/99/444444444444"
        )

        tickets = await extract_tickets(provider)

        assert len(tickets) == 3
        assert all(
            ticket["ticket_url"].startswith("https://app.asana.com/")
            for ticket in tickets
        )
