"""Build a working anchor from a model-supplied line range."""
from pr_agent.git_providers.github_provider import GithubProvider


def _provider():
    p = GithubProvider.__new__(GithubProvider)
    p.base_url_html = "https://github.com"
    p.repo = "o/r"
    p.pr_num = 1
    return p


def test_an_inverted_range_is_normalised():
    """Normalise end < start, which would emit R10-R5 that GitHub cannot resolve."""
    link = _provider().get_line_link("a.py", 10, 5)

    assert "R10-R5" not in link
    assert link.endswith("R10")


def test_a_normal_range_is_unchanged():
    """Keep the existing anchor for a well-ordered range."""
    assert _provider().get_line_link("a.py", 5, 10).endswith("R5-R10")


def test_a_single_line_is_unchanged():
    """Link to the single start line when no end line is supplied."""
    assert _provider().get_line_link("a.py", 5).endswith("R5")


def test_a_non_numeric_end_falls_back_to_the_start_line():
    """Fall back to the start line when the end line is malformed."""
    assert _provider().get_line_link("a.py", 5, "not-a-number").endswith("R5")


def test_the_file_level_link_is_unchanged():
    """Produce the whole-file anchor when start is -1."""
    assert _provider().get_line_link("a.py", -1).endswith("files#diff-" + __import__("hashlib")
                                                          .sha256(b"a.py").hexdigest())
