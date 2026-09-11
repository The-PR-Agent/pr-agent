from types import SimpleNamespace

from pr_agent.algo.ship_scope import (
    cap_low_priority_files,
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


def _sized_file(name: str, patch: str):
    return SimpleNamespace(filename=name, patch=patch)


def test_cap_low_priority_files_summarizes_only_oversized_low_priority_files():
    """The cap is what makes ship scope bite on a PR whose token budget never binds: ordering
    alone leaves a large design document fully reviewed (R-9, tests/eval/BASELINE.md)."""
    globs = ["design/**", "**/*.md"]
    big_design = _sized_file("design/hero.html", "x" * 400)
    small_design = _sized_file("design/icon.svg.md", "x" * 4)
    code = _sized_file("lib/app.py", "x" * 400)

    kept, summarized = cap_low_priority_files(
        [big_design, small_design, code], globs, max_tokens=10, count_tokens=len
    )

    assert [f.filename for f in kept] == ["design/icon.svg.md", "lib/app.py"]
    assert summarized == ["design/hero.html"]


def test_cap_low_priority_files_is_disabled_by_a_non_positive_cap():
    big_design = _sized_file("design/hero.html", "x" * 400)
    kept, summarized = cap_low_priority_files([big_design], ["design/**"], 0, len)
    assert [f.filename for f in kept] == ["design/hero.html"]
    assert summarized == []


def test_cap_low_priority_files_keeps_a_file_it_cannot_price():
    """A counter that raises, or a file with no patch, must not silently drop the file from the
    review - the whole point of ship scope is that nothing is excluded without being reported."""
    def exploding(_patch):
        raise RuntimeError("no tokenizer")

    kept, summarized = cap_low_priority_files(
        [_sized_file("design/a.html", "x" * 400)], ["design/**"], 10, exploding
    )
    assert [f.filename for f in kept] == ["design/a.html"]
    assert summarized == []

    kept, summarized = cap_low_priority_files(
        [_sized_file("design/b.html", "")], ["design/**"], 10, len
    )
    assert [f.filename for f in kept] == ["design/b.html"]
    assert summarized == []


def test_cap_low_priority_files_never_empties_the_review():
    """A PR of nothing but oversized low-priority files would cap down to an empty diff, and an
    empty diff ends the run with no prediction - so no review is published at all and the PR is
    silently skipped. The cap protects real code from low-priority files; with no other code in
    the PR there is nothing to protect."""
    globs = ["design/**", "**/*.md"]
    files = [_sized_file("design/hero.html", "x" * 400), _sized_file("docs/spec.md", "x" * 400)]

    kept, summarized = cap_low_priority_files(files, globs, max_tokens=10, count_tokens=len)

    assert [f.filename for f in kept] == ["design/hero.html", "docs/spec.md"]
    assert summarized == []
