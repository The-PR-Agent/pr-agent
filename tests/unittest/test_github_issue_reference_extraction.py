"""A bare `#123` reference is followed to the same depth as one in a branch name.

`BRANCH_ISSUE_PATTERN` already accepts up to six digits, so `123456-fix` as a branch resolves
while `#123456` in the description did not. The two now agree.
"""
import pytest

from pr_agent.tools.ticket_pr_compliance_check import (
    BRANCH_ISSUE_PATTERN,
    MAX_SHORTHAND_ISSUE_DIGITS,
    extract_ticket_links_from_pr_description,
)

REPO = "org/repo"
BASE = "https://github.com"


def _links(description):
    return extract_ticket_links_from_pr_description(description, REPO, BASE)


@pytest.mark.parametrize("number", ["1", "42", "999", "1234", "12345", "123456"])
def test_a_shorthand_reference_is_extracted(number):
    assert _links(f"Fixes #{number}") == [f"{BASE}/{REPO}/issues/{number}"]


@pytest.mark.parametrize("number", ["1234567", "12345678", "20260906"])
def test_a_number_too_long_to_be_an_issue_is_ignored(number):
    """Control: the guard against mistaking an error code for an issue is still there."""
    assert _links(f"Related to #{number}") == []


@pytest.mark.parametrize("number", ["1", "1234", "123456"])
def test_the_bound_agrees_with_the_branch_name_pattern(number):
    """The inconsistency this fixes: the same number, written two ways, resolving differently."""
    assert BRANCH_ISSUE_PATTERN.search(f"feature/{number}-fix") is not None
    assert _links(f"Fixes #{number}") == [f"{BASE}/{REPO}/issues/{number}"]


def test_the_bound_matches_the_branch_pattern_by_construction():
    assert MAX_SHORTHAND_ISSUE_DIGITS == 6
    assert BRANCH_ISSUE_PATTERN.search("feature/1234567-fix") is None


def test_several_references_keep_their_order():
    links = _links("Fixes #12345, closes #7 and #98765")

    assert links == [f"{BASE}/{REPO}/issues/12345",
                     f"{BASE}/{REPO}/issues/7",
                     f"{BASE}/{REPO}/issues/98765"]


def test_a_repeated_reference_is_listed_once():
    assert _links("Fixes #12345 and again #12345") == [f"{BASE}/{REPO}/issues/12345"]


def test_a_full_url_is_not_bounded():
    """Control: an explicit URL is unambiguous, so it never had a length bound."""
    url = f"{BASE}/{REPO}/issues/1234567"

    assert _links(f"Fixes {url}") == [url]


def test_a_cross_repo_shorthand_is_not_bounded():
    """Control: owner/repo#123 names its repository, so it is unambiguous too."""
    assert _links("Fixes other/project#12345") == [f"{BASE}/other/project/issues/12345"]


@pytest.mark.parametrize(
    ("shorthand", "expected_url"),
    [
        ("my-org/repo#42", f"{BASE}/my-org/repo/issues/42"),
        ("org/my-repo#42", f"{BASE}/org/my-repo/issues/42"),
        ("my-org/my-repo#42", f"{BASE}/my-org/my-repo/issues/42"),
    ],
)
def test_cross_repo_with_hyphens(shorthand, expected_url):
    assert _links(f"Fixes {shorthand}") == [expected_url]


@pytest.mark.parametrize(
    ("shorthand", "expected_url"),
    [
        ("org/my.repo#7", f"{BASE}/org/my.repo/issues/7"),
        ("org/my_repo#7", f"{BASE}/org/my_repo/issues/7"),
    ],
)
def test_cross_repo_with_period_and_underscore_in_repo_name(shorthand, expected_url):
    assert _links(f"Fixes {shorthand}") == [expected_url]


def test_cross_repo_this_repo_own_name():
    assert _links("Fixes The-PR-Agent/pr-agent#3081") == [f"{BASE}/The-PR-Agent/pr-agent/issues/3081"]


def test_regression_bare_shorthand_resolves_against_current_repo():
    assert _links("Fixes #42") == [f"{BASE}/{REPO}/issues/42"]


def test_regression_full_url_still_wins():
    url = f"{BASE}/some-org/some-repo/issues/99"
    assert _links(f"Fixes {url}") == [url]


def test_regression_mid_token_does_not_match_cross_repo():
    assert _links("see x/my-org/my-repo#42") == [f"{BASE}/{REPO}/issues/42"]
    assert _links("see x/other/project#12345") == [f"{BASE}/{REPO}/issues/12345"]


@pytest.fixture
def description_regex(monkeypatch):
    def configure(pattern):
        monkeypatch.setattr(
            "pr_agent.tools.ticket_pr_compliance_check.get_settings",
            lambda: {"config.description_issue_regex": pattern},
        )
    return configure


def test_custom_syntax_ignores_incidental_pr_reference(description_regex):
    description_regex(r"(?i)(?:fixes|closes|resolves|refs|ticket)\s*[:#]?\s*#?(\d+)")
    assert _links("The fix landed in PR #56. Fixes #123; ticket: 456") == [
        f"{BASE}/{REPO}/issues/123", f"{BASE}/{REPO}/issues/456",
    ]


def test_empty_custom_pattern_preserves_default(description_regex):
    description_regex("")
    assert _links("The fix landed in PR #56") == [f"{BASE}/{REPO}/issues/56"]


@pytest.mark.parametrize("pattern", ["[", r"#\d+", r"(fixes) #(\d+)", 42, r"(a){4294967296}", "(" * 1000])
def test_invalid_custom_pattern_falls_back(description_regex, pattern, monkeypatch):
    from unittest.mock import Mock

    logger = Mock()
    monkeypatch.setattr("pr_agent.tools.ticket_pr_compliance_check.get_logger", lambda: logger)
    description_regex(pattern)
    assert _links("PR #56") == [f"{BASE}/{REPO}/issues/56"]
    logger.warning.assert_called_once()


@pytest.mark.parametrize("number", ["9" * 4301, "²", "١٢"])
def test_invalid_numeric_capture_preserves_valid_references(description_regex, number):
    description_regex(r"ticket: (\S+)")
    assert _links(f"ticket: {number} ticket: 42") == [f"{BASE}/{REPO}/issues/42"]


def test_custom_matches_keep_explicit_references_order_and_cap(description_regex):
    description_regex(r"ticket: (\d+)")
    assert _links("ticket: 1 other/project#2 ticket: 1 https://github.com/org/repo/issues/3 ticket: 4") == [
        f"{BASE}/{REPO}/issues/1", f"{BASE}/other/project/issues/2", f"{BASE}/{REPO}/issues/3",
    ]


def test_custom_pattern_does_not_duplicate_explicit_numbers(description_regex):
    description_regex(r"(\d+)")
    assert _links("other/project#42 https://github.com/elsewhere/project/issues/99") == [
        f"{BASE}/other/project/issues/42", f"{BASE}/elsewhere/project/issues/99",
    ]


def test_custom_pattern_controls_digit_bound(description_regex):
    description_regex(r"ticket: (\d+)")
    assert _links("ticket: 1234567") == [f"{BASE}/{REPO}/issues/1234567"]


@pytest.mark.parametrize("description", ["ticket:", "ticket: abc"])
def test_custom_capture_must_be_present_and_numeric(description_regex, description):
    description_regex(r"ticket:(?: (\w+))?")
    assert _links(description) == []


def test_custom_pattern_without_repo_preserves_explicit_links(description_regex):
    description_regex(r"ticket: (\d+)")
    assert extract_ticket_links_from_pr_description("ticket: 1 other/project#2", "") == [
        f"{BASE}/other/project/issues/2",
    ]


def test_custom_pattern_respects_enterprise_base_url(description_regex):
    description_regex(r"ticket: (\d+)")
    assert extract_ticket_links_from_pr_description("ticket: 42", REPO, "https://github.example.com/") == [
        f"https://github.example.com/{REPO}/issues/42",
    ]
