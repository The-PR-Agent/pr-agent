import pytest

from pr_agent.tools.ticket_pr_compliance_check import extract_ticket_links_from_branch_name


class TestExtractTicketsLinkFromBranchName:
    """Unit tests for branch-name issue extraction (option A: number at start of segment)."""

    def test_feature_slash_number_suffix(self):
        """feature/1-test-issue -> issue #1"""
        result = extract_ticket_links_from_branch_name(
            "feature/1-test-issue", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/1"]

    def test_fix_slash_number_suffix(self):
        """fix/123-bug -> issue #123"""
        result = extract_ticket_links_from_branch_name(
            "fix/123-bug", "owner/repo", "https://github.com"
        )
        assert result == ["https://github.com/owner/repo/issues/123"]

    def test_number_at_start_no_slash(self):
        """123-fix -> issue #123"""
        result = extract_ticket_links_from_branch_name(
            "123-fix", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/123"]

    def test_empty_branch_returns_empty(self):
        """Empty branch name -> []"""
        result = extract_ticket_links_from_branch_name("", "org/repo")
        assert result == []

    def test_none_branch_returns_empty(self):
        """None branch name -> []"""
        result = extract_ticket_links_from_branch_name(None, "org/repo")
        assert result == []

    def test_no_digits_in_segment_returns_empty(self):
        """feature/no-issue -> []"""
        result = extract_ticket_links_from_branch_name(
            "feature/no-issue", "org/repo", "https://github.com"
        )
        assert result == []

    def test_base_url_no_trailing_slash(self):
        """base_url_html without trailing slash is normalized"""
        result = extract_ticket_links_from_branch_name(
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
        import pr_agent.tools.ticket_pr_compliance_check as m
        monkeypatch.setattr(m, "get_settings", lambda: fake_settings)
        result = extract_ticket_links_from_branch_name(
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
        import pr_agent.tools.ticket_pr_compliance_check as m
        monkeypatch.setattr(m, "get_settings", lambda: fake_settings)
        result = extract_ticket_links_from_branch_name(
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
        import pr_agent.tools.ticket_pr_compliance_check as m
        monkeypatch.setattr(m, "get_settings", lambda: fake_settings)
        result = extract_ticket_links_from_branch_name(
            "feature/1-test", "org/repo", "https://github.com"
        )
        assert result == ["https://github.com/org/repo/issues/1"]

    def test_empty_repo_path_returns_empty(self):
        """Empty repo_path -> [] (guard in function)"""
        result = extract_ticket_links_from_branch_name("feature/1-test", "", "https://github.com")
        assert result == []

    def test_multiple_matches_deduplicated(self):
        """Branch with multiple segments with numbers yields unique issue URLs"""
        result = extract_ticket_links_from_branch_name(
            "feature/1-test/2-other", "org/repo", "https://github.com"
        )
        assert set(result) == {
            "https://github.com/org/repo/issues/1",
            "https://github.com/org/repo/issues/2",
        }
