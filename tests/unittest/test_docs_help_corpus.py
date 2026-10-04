"""Guards the documentation corpus that `/help "question"` feeds to the model.

`PRHelpMessage` reads the docs tree at runtime, so a page the collector misses is
silently absent from the answer -- no error, just a worse reply. That is exactly
what happened when `tools/improve.md` became `tools/improve.mdx` while the
collector still matched `.md` only.
"""

from pathlib import Path, PurePosixPath

from pr_agent.tools.pr_help_message import (
    HELP_DOCS_EXCLUDED_DIRECTORIES,
    HELP_DOCS_EXCLUDED_FILENAMES,
    HELP_DOCS_PRIORITY_PATHS,
    HELP_DOCS_SUFFIXES,
    _get_help_docs_root,
    _is_help_doc_included,
    _iter_help_docs,
)

DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "docs"


def _pages_on_disk() -> set[str]:
    """Every page the corpus is expected to carry, as '/'-prefixed relative paths."""
    pages = set()
    for suffix in HELP_DOCS_SUFFIXES:
        for page in DOCS_PATH.rglob(f"*{suffix}"):
            relative = PurePosixPath(page.relative_to(DOCS_PATH).as_posix())
            if _is_help_doc_included(relative):
                pages.add(f"/{relative.as_posix()}")
    return pages


def _collected() -> list[str]:
    return [f"/{relative.as_posix()}" for relative, _ in _iter_help_docs(_get_help_docs_root())]


def test_every_docs_page_reaches_the_help_corpus():
    """No page is dropped, whatever suffix it happens to use."""
    missing = _pages_on_disk() - set(_collected())
    assert not missing, f"pages exist but never reach the /help corpus: {sorted(missing)}"


def test_corpus_contains_no_duplicates_and_nothing_extra():
    collected = _collected()
    assert len(collected) == len(set(collected)), "a page is collected twice"
    assert set(collected) == _pages_on_disk()


def test_mdx_pages_are_collected():
    """The specific regression: '.mdx' pages must not be invisible to /help."""
    mdx_pages = {page for page in _pages_on_disk() if page.endswith(".mdx")}
    if not mdx_pages:  # nothing to prove today, but the guard stays honest
        return
    assert mdx_pages <= set(_collected())


def test_priority_paths_still_match_real_pages():
    """A path that matches nothing is dead config, and silently stops prioritising."""
    collected = _collected()
    dead = [p for p in HELP_DOCS_PRIORITY_PATHS if not any(p in page for page in collected)]
    assert not dead, f"priority paths match no page on disk: {dead}"


def test_index_sorts_first_and_priority_pages_precede_the_rest():
    collected = _collected()
    assert collected[0].startswith("/index."), f"the overview page should sort first, got {collected[0]}"
    ranks = [0 if any(p in page for p in HELP_DOCS_PRIORITY_PATHS) else 1 for page in collected[1:]]
    assert ranks == sorted(ranks), "priority pages must all precede non-priority pages"


def test_declared_exclusions_still_exist():
    """A stale exclusion would quietly hide a real page."""
    for name in HELP_DOCS_EXCLUDED_FILENAMES:
        assert any(DOCS_PATH.rglob(name)), f"excluded file no longer exists: {name}"
    for directory in HELP_DOCS_EXCLUDED_DIRECTORIES:
        # These are allowed to be absent, but if present they must be excluded.
        for page in (DOCS_PATH / directory).rglob("*.md"):
            relative = PurePosixPath(page.relative_to(DOCS_PATH).as_posix())
            assert not _is_help_doc_included(relative)


REPO_ROOT = DOCS_PATH.parents[1]


def test_every_packaging_path_ships_every_page_suffix():
    """Keep the four places that build the /help corpus in agreement on suffixes.

    `python -m build` makes the wheel from the sdist, so a suffix missing from
    MANIFEST.in never reaches setup.py's build step and the published package
    silently answers /help without those pages.
    """
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    docs_rules = [line.split()[2:] for line in manifest.splitlines() if line.startswith("recursive-include docs/docs")]
    manifest_patterns = {pattern for rule in docs_rules for pattern in rule}
    setup_py = (REPO_ROOT / "setup.py").read_text(encoding="utf-8")
    lambda_dockerfile = (REPO_ROOT / "docker" / "Dockerfile.lambda").read_text(encoding="utf-8")

    for suffix in HELP_DOCS_SUFFIXES:
        pattern = f"*{suffix}"
        assert pattern in manifest_patterns, f"MANIFEST.in does not include docs/docs {pattern}"
        assert f'"{pattern}"' in setup_py, f"setup.py does not package {pattern}"
        assert f"-name '{pattern}'" in lambda_dockerfile, f"Dockerfile.lambda does not copy {pattern}"
