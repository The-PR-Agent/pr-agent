"""
Unit tests for Asana ticket detection in ticket_pr_compliance_check.py.

Tests cover:
- Full Asana URL detection
- Shorthand ASANA- prefix detection
- Edge cases (mixed content, no tickets, duplicates)
"""
from pr_agent.tools.ticket_pr_compliance_check import find_asana_tickets


class TestFindAsanaTickets:
    """Tests for find_asana_tickets()."""

    def test_detects_full_asana_url(self):
        """Full Asana task URLs should be detected."""
        text = "See https://app.asana.com/0/123456/789012 for details"
        tickets = find_asana_tickets(text)
        assert "https://app.asana.com/0/123456/789012" in tickets

    def test_detects_asana_shorthand(self):
        """ASANA-123456789012 shorthand format should be detected."""
        text = "Task ASANA-123456789012 is complete"
        tickets = find_asana_tickets(text)
        assert any("123456789012" in t for t in tickets)

    def test_detects_multiple_tickets(self):
        """Multiple Asana references should all be found."""
        text = (
            "See ASANA-111111111111 and https://app.asana.com/0/22/333333333333"
        )
        tickets = find_asana_tickets(text)
        assert len(tickets) == 2

    def test_deduplicates_identical_tickets(self):
        """Duplicate references to the same task should be deduplicated."""
        text = (
            "ASANA-123456789012 mentioned twice: ASANA-123456789012 again"
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

    def test_ignores_github_urls(self):
        """GitHub issue URLs should not be mistaken for Asana tickets."""
        text = "Fix https://github.com/owner/repo/issues/42"
        tickets = find_asana_tickets(text)
        assert tickets == []

    def test_shorthand_is_case_insensitive(self):
        """Both ASANA- and asana- should be detected."""
        text = "asana-999999999999 lowercase works too"
        tickets = find_asana_tickets(text)
        assert len(tickets) == 1
        assert "999999999999" in tickets[0]

    def test_shorthand_mixed_case_asana(self):
        """Mixed-case shorthand like Asana-123... should also be detected."""
        text = "Asana-888888888888 mixed case"
        tickets = find_asana_tickets(text)
        assert len(tickets) == 1
        assert "888888888888" in tickets[0]

    def test_shorthand_respects_word_boundary(self):
        """Text preceding the shorthand must not be alphanumeric.
        NOTASANA-123 (no delimiter before ASANA) should not match."""
        text = "Check NOTASANA-123456789012 in logs"
        tickets = find_asana_tickets(text)
        assert tickets == []

    def test_tickets_are_sorted(self):
        """Returned list should be sorted alphabetically."""
        text = "ASANA-222222222222 ASANA-111111111111"
        tickets = find_asana_tickets(text)
        assert tickets == sorted(tickets)

    def test_tickets_in_pr_description_mixed_content(self):
        """Asana tickets mixed with other content in a PR description."""
        text = """## Summary
        Fixes ASANA-123456789012
        Related to https://app.asana.com/0/99/888888888888

        Also see GitHub issue #42
        """
        tickets = find_asana_tickets(text)
        assert len(tickets) == 2
