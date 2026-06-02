from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.ticket_pr_compliance_check import (
    _get_jira_client,
    _get_pr_title,
    add_jira_tickets,
    extract_jira_tickets,
    find_jira_tickets,
)

# Keys the tests mutate via get_settings().set(...). Snapshot and restore them around
# every test so values (e.g. JIRA_REQUIREMENTS_FIELD) don't leak between tests.
_JIRA_KEYS = (
    "JIRA.JIRA_BASE_URL",
    "JIRA.JIRA_API_EMAIL",
    "JIRA.JIRA_API_TOKEN",
    "JIRA.JIRA_REQUIREMENTS_FIELD",
)


@pytest.fixture(autouse=True)
def restore_jira_settings():
    saved = {key: get_settings().get(key, None) for key in _JIRA_KEYS}
    yield
    for key, value in saved.items():
        get_settings().set(key, value)


class TestFindJiraTickets:
    """Jira key extraction from arbitrary text (PR title, description, branch name)."""

    def test_uppercase_key_with_prefix(self):
        """feature/ABC-123-description -> ABC-123"""
        assert find_jira_tickets("feature/ABC-123-description-of-branch") == ["ABC-123"]

    def test_lowercase_key_with_prefix_normalized(self):
        """bugfix/abc-123-description -> ABC-123 (case-insensitive, normalized to upper)"""
        assert find_jira_tickets("bugfix/abc-123-description-of-branch") == ["ABC-123"]

    def test_mixed_case_key_normalized(self):
        """Abc-123 -> ABC-123"""
        assert find_jira_tickets("Abc-123-fix") == ["ABC-123"]

    def test_arbitrary_prefix_segment(self):
        """Any prefix segment, not just feature/bugfix."""
        assert find_jira_tickets("chore/PROJ-45-cleanup") == ["PROJ-45"]
        assert find_jira_tickets("hotfix/proj-45-cleanup") == ["PROJ-45"]

    def test_key_at_start_no_prefix(self):
        """ABC-123-fix -> ABC-123"""
        assert find_jira_tickets("ABC-123-fix") == ["ABC-123"]

    def test_key_anywhere_in_branch(self):
        """Key embedded in the middle of a branch name is still found."""
        assert find_jira_tickets("release/v1.2.3-ABC-9-final") == ["ABC-9"]

    def test_key_in_description_text(self):
        """Key mentioned in free text."""
        assert find_jira_tickets("This implements ABC-123 as discussed") == ["ABC-123"]

    def test_browse_url(self):
        """Full Jira browse URL -> key."""
        assert find_jira_tickets(
            "see https://acme.atlassian.net/browse/ABC-123 for details"
        ) == ["ABC-123"]

    def test_no_ticket(self):
        """Branch with no key -> []"""
        assert find_jira_tickets("feature/no-ticket-here") == []
        assert find_jira_tickets("") == []

    def test_multiple_tickets_deduped_in_order(self):
        """Distinct keys are returned de-duplicated and case-normalized, in first-seen
        order. Order must be stable so the later MAX_TICKETS cap is deterministic."""
        result = find_jira_tickets("ABC-1 and DEF-2, again ABC-1 and abc-1")
        assert result == ["ABC-1", "DEF-2"]

    def test_order_preserved_across_patterns(self):
        """First-seen order holds even when keys arrive via different patterns (plain
        key vs. browse URL)."""
        result = find_jira_tickets(
            "GHI-3 first, then https://acme.atlassian.net/browse/ABC-1, then DEF-2"
        )
        assert result == ["GHI-3", "ABC-1", "DEF-2"]


class TestExtractJiraTickets:
    """End-to-end extraction: find keys, fetch via the Jira client, map to ticket dicts."""

    def _configure_jira(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "https://acme.atlassian.net")
        get_settings().set("JIRA.JIRA_API_EMAIL", "me@acme.com")
        get_settings().set("JIRA.JIRA_API_TOKEN", "token123")

    def _disable_jira(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "")
        get_settings().set("JIRA.JIRA_API_EMAIL", "")
        get_settings().set("JIRA.JIRA_API_TOKEN", "")

    def _fake_client(self, fields=None):
        fields = fields or {"summary": "Title", "description": "Body", "labels": ["a"]}
        client = MagicMock()
        client.issue.return_value = {"fields": fields}
        return client

    def test_returns_empty_when_not_configured(self):
        self._disable_jira()
        assert extract_jira_tickets("bugfix/abc-123-x") == []

    def test_fetches_lowercase_branch_key(self):
        """The whole point: a lowercased branch key is detected and fetched as upper."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("bugfix/abc-123-description-of-branch")
        client.issue.assert_called_once_with("ABC-123")
        assert len(result) == 1
        assert result[0]["ticket_id"] == "ABC-123"
        assert result[0]["ticket_url"] == "https://acme.atlassian.net/browse/ABC-123"
        assert result[0]["title"] == "Title"
        assert result[0]["labels"] == "a"

    def test_requirements_field_populated_when_configured(self):
        """When jira_requirements_field is set, that custom field maps to requirements."""
        self._configure_jira()
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "customfield_10127")
        client = MagicMock()
        client.issue.return_value = {"fields": {
            "summary": "T", "description": "B", "labels": [],
            "customfield_10127": "Acceptance criteria text",
        }}
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1")
        assert result[0]["requirements"] == "Acceptance criteria text"

    def test_requirements_truncated_like_body(self):
        """A large requirements custom field is capped the same way the body is, so it
        can't push an unbounded blob into the review prompt."""
        self._configure_jira()
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "customfield_10127")
        client = MagicMock()
        client.issue.return_value = {"fields": {
            "summary": "T", "description": "B", "labels": [],
            "customfield_10127": "x" * 50,
        }}
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1", max_characters=10)
        assert result[0]["requirements"] == "x" * 10 + "..."

    def test_requirements_empty_when_field_not_configured(self):
        """With no requirements field configured, requirements stays empty."""
        self._configure_jira()
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "")
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1")
        assert result[0]["requirements"] == ""

    def test_checks_all_tickets_when_multiple(self):
        """When several distinct keys are present, each is fetched."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 DEF-2 GHI-3")
        fetched = {c.args[0] for c in client.issue.call_args_list}
        assert fetched == {"ABC-1", "DEF-2", "GHI-3"}
        assert len(result) == 3

    def test_caps_candidate_keys_at_three(self):
        """No more than three candidate keys are fetched (matches the GitHub branch)."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 ABC-2 ABC-3 ABC-4 ABC-5")
        assert client.issue.call_count == 3
        assert len(result) == 3

    def test_skips_ticket_on_fetch_error(self):
        """A failed fetch for one key does not abort the others."""
        self._configure_jira()
        client = MagicMock()
        client.issue.side_effect = [
            Exception("404 not found"),
            {"fields": {"summary": "Second", "description": "B", "labels": []}},
        ]
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 ABC-2")
        assert len(result) == 1
        assert result[0]["title"] == "Second"

    def test_nonexistent_keys_are_skipped(self):
        """Key-like noise (utf-8, sha-1) that does not resolve in Jira is skipped,
        leaving only the real ticket."""
        self._configure_jira()

        def fake_issue(key):
            if key == "ABC-123":
                return {"fields": {"summary": "Real", "description": "Body", "labels": []}}
            raise Exception("404 not found")

        client = MagicMock()
        client.issue.side_effect = fake_issue
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("abc-123 utf-8")
        assert [t["ticket_id"] for t in result] == ["ABC-123"]


class TestGetJiraClient:
    """Client construction: auth mode selection and the pinned REST API version."""

    def test_cloud_uses_basic_auth_and_pins_v2(self):
        """Email present -> username/password basic auth, REST v2 pinned."""
        get_settings().set("JIRA.JIRA_BASE_URL", "https://acme.atlassian.net/")
        get_settings().set("JIRA.JIRA_API_EMAIL", "me@acme.com")
        get_settings().set("JIRA.JIRA_API_TOKEN", "token123")
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira") as jira_cls:
            _get_jira_client()
        jira_cls.assert_called_once_with(
            url="https://acme.atlassian.net", username="me@acme.com",
            password="token123", api_version="2",
        )

    def test_server_pat_uses_token_auth_and_pins_v2(self):
        """No email -> token (PAT) auth, REST v2 pinned."""
        get_settings().set("JIRA.JIRA_BASE_URL", "https://jira.example.com")
        get_settings().set("JIRA.JIRA_API_EMAIL", "")
        get_settings().set("JIRA.JIRA_API_TOKEN", "pat456")
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira") as jira_cls:
            _get_jira_client()
        jira_cls.assert_called_once_with(
            url="https://jira.example.com", token="pat456", api_version="2",
        )

    def test_returns_none_when_not_configured(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "")
        get_settings().set("JIRA.JIRA_API_TOKEN", "")
        assert _get_jira_client() is None


class TestGetPrTitle:
    """Provider-agnostic title access (GitHub/Bitbucket use .pr, GitLab uses .mr)."""

    def test_reads_pr_title(self):
        gp = MagicMock(spec=["pr"])
        gp.pr = MagicMock(title="From PR object")
        assert _get_pr_title(gp) == "From PR object"

    def test_reads_mr_title_when_no_pr(self):
        """GitLab stores the merge request as .mr, not .pr."""
        gp = MagicMock(spec=["mr"])
        gp.mr = MagicMock(title="From MR object")
        assert _get_pr_title(gp) == "From MR object"

    def test_returns_empty_when_no_title(self):
        gp = MagicMock(spec=[])
        assert _get_pr_title(gp) == ""


class TestAddJiraTickets:
    """Provider-agnostic Jira append used by extract_tickets for every provider."""

    def _provider(self, title="", description="", branch=""):
        gp = MagicMock(spec=["pr", "get_user_description", "get_pr_branch"])
        gp.pr = MagicMock(title=title)
        gp.get_user_description.return_value = description
        gp.get_pr_branch.return_value = branch
        return gp

    def _configure_jira(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "https://acme.atlassian.net")
        get_settings().set("JIRA.JIRA_API_EMAIL", "me@acme.com")
        get_settings().set("JIRA.JIRA_API_TOKEN", "token123")
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "")

    def test_appends_ticket_from_any_provider(self):
        """Works off get_user_description + get_pr_branch, so it is provider-neutral."""
        self._configure_jira()
        client = MagicMock()
        client.issue.return_value = {"fields": {"summary": "T", "description": "B", "labels": []}}
        gp = self._provider(branch="feature/ABC-123-x")
        out = []
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            add_jira_tickets(gp, out)
        assert [t["ticket_id"] for t in out] == ["ABC-123"]

    def test_dedupes_against_existing_tickets(self):
        """A Jira ticket already present (same url) is not added twice."""
        self._configure_jira()
        client = MagicMock()
        client.issue.return_value = {"fields": {"summary": "T", "description": "B", "labels": []}}
        gp = self._provider(title="ABC-123")
        existing = [{"ticket_url": "https://acme.atlassian.net/browse/ABC-123"}]
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            add_jira_tickets(gp, existing)
        assert len(existing) == 1

    def test_noop_when_jira_not_configured(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "")
        get_settings().set("JIRA.JIRA_API_TOKEN", "")
        gp = self._provider(branch="feature/ABC-123-x")
        out = []
        add_jira_tickets(gp, out)
        assert out == []

    def test_respects_overall_cap_with_existing_tickets(self):
        """MAX_TICKETS is the combined per-PR cap: provider-native tickets already in
        tickets_content count against it, and Jira is appended only up to the cap."""
        from pr_agent.tools.ticket_pr_compliance_check import MAX_TICKETS
        self._configure_jira()
        client = MagicMock()
        client.issue.return_value = {"fields": {"summary": "T", "description": "B", "labels": []}}
        # Pre-fill with (MAX_TICKETS - 1) provider-native tickets, then offer several Jira keys.
        existing = [{"ticket_url": f"https://example/issues/{i}"} for i in range(MAX_TICKETS - 1)]
        gp = self._provider(description="ABC-1 DEF-2 GHI-3 JKL-4")
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            add_jira_tickets(gp, existing)
        assert len(existing) == MAX_TICKETS  # only one Jira ticket was appended

    def test_skips_jira_entirely_when_cap_already_reached(self):
        """When provider-native tickets already fill the cap, no Jira client is built."""
        from pr_agent.tools.ticket_pr_compliance_check import MAX_TICKETS
        self._configure_jira()
        existing = [{"ticket_url": f"https://example/issues/{i}"} for i in range(MAX_TICKETS)]
        gp = self._provider(description="ABC-1 DEF-2")
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira") as jira_cls:
            add_jira_tickets(gp, existing)
        jira_cls.assert_not_called()
        assert len(existing) == MAX_TICKETS
