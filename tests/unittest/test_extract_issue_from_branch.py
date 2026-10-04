
from pr_agent.tools import ticket_pr_compliance_check as tpc

# The PR-description extractor caps results at 3 (hardcoded in the function).
MAX_TICKETS = 3


class _ReverseIterationSet(set):
    """Use a set double whose iteration order cannot accidentally match insertion order."""

    def __init__(self):
        super().__init__()
        self._insertion_order = []

    def add(self, item):
        if item not in self:
            self._insertion_order.append(item)
        super().add(item)

    def __iter__(self):
        return iter(reversed(self._insertion_order))

    def __eq__(self, other):
        return set.__eq__(self, other)


class TestExtractTicketsLinkFromBranchName:
    """Unit tests for branch-name issue extraction (option A: number at start of segment)."""

    def test_feature_slash_number_suffix(self):
        """feature/1-test-issue -> issue #1"""
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test-issue", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/1"]

    def test_fix_slash_number_suffix(self):
        """fix/123-bug -> issue #123"""
        result = tpc.extract_ticket_links_from_branch_name(
            "fix/123-bug", "owner/repo", "https://github.com"
        )
        assert result == ["https://github.com/owner/repo/issues/123"]

    def test_number_at_start_no_slash(self):
        """123-fix -> issue #123"""
        result = tpc.extract_ticket_links_from_branch_name(
            "123-fix", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/123"]

    def test_empty_branch_returns_empty(self):
        """Empty branch name -> []"""
        result = tpc.extract_ticket_links_from_branch_name("", "org/repo")
        assert result == []

    def test_none_branch_returns_empty(self):
        """None branch name -> []"""
        result = tpc.extract_ticket_links_from_branch_name(None, "org/repo")
        assert result == []

    def test_no_digits_in_segment_returns_empty(self):
        """feature/no-issue -> []"""
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/no-issue", "org/repo", "https://github.com"
        )
        assert result == []

    def test_base_url_no_trailing_slash(self):
        """base_url_html without trailing slash is normalized"""
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test", "org/repo", "https://github.com/"
        )
        assert result == ["https://github.com/org/repo/issues/1"]

    def test_disable_via_config_returns_empty(self, monkeypatch):
        """When extract_issue_from_branch is False, return []"""
        fake_settings = type("Settings", (), {})()
        fake_settings.get = lambda key, default=None: (
            False if key in ("extract_issue_from_branch", "config.extract_issue_from_branch") else (
                "" if key in ("branch_issue_regex", "config.branch_issue_regex") else default
            )
        )
        monkeypatch.setattr(tpc, "get_settings", lambda: fake_settings)
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test", "org/repo", "https://github.com"
        )
        assert result == []

    def test_invalid_custom_regex_returns_empty(self, monkeypatch):
        """When branch_issue_regex is invalid, log and return []"""
        fake_settings = type("Settings", (), {})()
        fake_settings.get = lambda key, default=None: (
            True if key in ("extract_issue_from_branch", "config.extract_issue_from_branch") else (
                "[" if key in ("branch_issue_regex", "config.branch_issue_regex") else default
            )
        )
        monkeypatch.setattr(tpc, "get_settings", lambda: fake_settings)
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test", "org/repo", "https://github.com"
        )
        assert result == []

    def test_custom_regex_without_capturing_group_falls_back_to_default(self, monkeypatch):
        """When branch_issue_regex has no capturing group, fall back to default pattern (no crash)."""
        fake_settings = type("Settings", (), {})()
        fake_settings.get = lambda key, default=None: (
            True if key in ("extract_issue_from_branch", "config.extract_issue_from_branch") else (
                r"\d+" if key in ("branch_issue_regex", "config.branch_issue_regex") else default
            )
        )
        monkeypatch.setattr(tpc, "get_settings", lambda: fake_settings)
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/1"]

    def test_empty_repo_path_returns_empty(self):
        """Empty repo_path -> [] (guard in function)"""
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/1-test", "", "https://github.com"
        )
        assert result == []

    def test_multiple_matches_preserve_first_seen_order(self, monkeypatch):
        """Verify that branch matches retain first-seen order and remove duplicate URLs."""
        monkeypatch.setattr(tpc, "set", _ReverseIterationSet, raising=False)
        result = tpc.extract_ticket_links_from_branch_name(
            "feature/2-test/1-other/2-again/3-final", "org/repo", "https://github.com"
        )
        assert result == [
            "https://github.com/org/repo/issues/2",
            "https://github.com/org/repo/issues/1",
            "https://github.com/org/repo/issues/3",
        ]


class TestExtractTicketLinksFromPrDescription:
    """GitHub issue extraction from the PR description."""

    def test_preserves_first_seen_order(self):
        """Issues are returned in first-seen order, de-duplicated.

        Note: this documents the intended ordered behaviour. It does not reliably fail
        against the old set-based code, because set iteration order is randomised per
        process (PYTHONHASHSEED) and may coincidentally match insertion order for a
        small input. test_cap_selects_deterministic_first_seen_subset is the reliable
        regression guard (see its note)."""
        desc = "Fixes #3, relates to #1, also #3 again and #2"
        result = tpc.extract_ticket_links_from_pr_description(
            desc, "org/repo", "https://github.com"
        )
        assert result == [
            "https://github.com/org/repo/issues/3",
            "https://github.com/org/repo/issues/1",
            "https://github.com/org/repo/issues/2",
        ]

    def test_cap_selects_deterministic_first_seen_subset(self):
        """When more than MAX_TICKETS issues are present, the first MAX_TICKETS in
        first-seen order are kept (not an arbitrary subset from a set).

        This is the reliable regression guard for the bug: with > MAX_TICKETS issues,
        the old code sliced list(set)[:MAX_TICKETS], so it returned an arbitrary subset
        that (essentially) never equals the first-seen subset, on any hash seed."""
        nums = list(range(1, MAX_TICKETS + 4))
        desc = " ".join(f"#{n}" for n in nums)
        result = tpc.extract_ticket_links_from_pr_description(
            desc, "org/repo", "https://github.com"
        )
        expected = [f"https://github.com/org/repo/issues/{n}" for n in nums[:MAX_TICKETS]]
        assert result == expected
