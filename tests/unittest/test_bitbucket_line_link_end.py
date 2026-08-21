"""Bitbucket links must honour the end line for a multi-line suggestion."""
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider


def _provider():
    p = BitbucketProvider.__new__(BitbucketProvider)
    p.pr_url = "https://bitbucket.org/o/r/pull-requests/1"
    return p


def test_a_multi_line_range_reaches_the_link():
    """relevant_line_end was accepted and then ignored entirely."""
    link = _provider().get_line_link("a.py", 5, 10)

    assert "T5" in link
    assert "T10" in link


def test_a_single_line_link_is_unchanged():
    """Keep the existing anchor when there is no end line."""
    assert _provider().get_line_link("a.py", 5).endswith("T5")


def test_an_end_equal_to_the_start_is_unchanged():
    """A one-line range renders as a single-line anchor."""
    assert _provider().get_line_link("a.py", 5, 5).endswith("T5")


def test_the_file_level_link_is_unchanged():
    """start == -1 still produces the whole-file anchor."""
    assert _provider().get_line_link("a.py", -1).endswith("#La.py")
