from pr_agent.algo.ship_scope import (
    is_low_priority,
    order_files_by_priority,
    propose_ignore_globs,
    render_ignore_proposal,
)

GLOBS = ["docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md"]


class _F:
    def __init__(self, name):
        self.filename = name


def test_low_priority_matches_defaults():
    assert is_low_priority("design/_v_fever.html", GLOBS)
    assert is_low_priority("README.md", GLOBS)
    assert is_low_priority("test/fixtures/x.json", GLOBS)
    assert not is_low_priority("lib/src/iap/iap_providers.dart", GLOBS)


def test_is_low_priority_does_not_match_prefix_lookalikes():
    """`docs/**` must not match `docs_helper.py`; `**/fixtures/**` must not match
    a filename that merely contains the substring `fixtures`."""
    assert not is_low_priority("docs_helper.py", GLOBS)
    assert not is_low_priority("lib/fixtures_loader.dart", GLOBS)


def test_order_is_stable_high_first():
    files = [_F("design/a.html"), _F("lib/a.dart"), _F("docs/b.md"), _F("lib/b.dart")]
    assert [f.filename for f in order_files_by_priority(files, GLOBS)] == [
        "lib/a.dart", "lib/b.dart", "design/a.html", "docs/b.md"
    ]


def test_propose_ignore_globs_groups_by_top_dir_and_sorts_by_tokens():
    low = ["design/a.html", "design/b.html", "docs/c.md"]
    tokens = {"design/a.html": 30000, "design/b.html": 25000, "docs/c.md": 2000}
    assert propose_ignore_globs(low, tokens) == [("design/**", 55000), ("docs/**", 2000)]


def test_render_proposal_is_toml_snippet():
    text = render_ignore_proposal([("design/**", 55000)])
    assert "[ignore]" in text and 'glob = ["design/**"]' in text and "55,000" in text
