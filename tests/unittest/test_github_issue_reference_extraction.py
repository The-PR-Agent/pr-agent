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
