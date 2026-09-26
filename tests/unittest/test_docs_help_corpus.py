"""Guards the documentation corpus that `/help "question"` feeds to the model.

`PRHelpMessage.run` reads the docs tree off disk at runtime, so a page that the
collector misses is silently absent from the answer -- no error, just a worse
reply. That is exactly what happened when `tools/improve.md` became
`tools/improve.mdx` while the collector still globbed `**/*.md` only.
"""

from pathlib import Path

from pr_agent.tools.pr_help_message import (
    DOCS_FILES_TO_EXCLUDE,
    DOCS_FOLDERS_TO_EXCLUDE,
    DOCS_PRIORITY_MARKERS,
    collect_docs_files,
    docs_root,
)

DOCS_PATH = docs_root()

PAGE_SUFFIXES = (".md", ".mdx")


def _pages_on_disk() -> set[Path]:
    return {
        page
        for suffix in PAGE_SUFFIXES
        for page in DOCS_PATH.rglob(f"*{suffix}")
        if not any(folder in page.as_posix() for folder in DOCS_FOLDERS_TO_EXCLUDE)
        and page.name not in DOCS_FILES_TO_EXCLUDE
    }


def test_every_docs_page_reaches_the_help_corpus():
    """No page is dropped, whatever suffix it happens to use."""
    missing = _pages_on_disk() - set(collect_docs_files(DOCS_PATH))
    assert not missing, (
        "documentation pages exist but never reach the /help corpus: "
        f"{sorted(p.relative_to(DOCS_PATH).as_posix() for p in missing)}"
    )


def test_corpus_contains_no_duplicates_and_nothing_extra():
    collected = collect_docs_files(DOCS_PATH)
    assert len(collected) == len(set(collected)), "a page is collected twice"
    assert set(collected) == _pages_on_disk()


def test_mdx_pages_are_collected():
    """The specific regression: '.mdx' pages must not be invisible to /help."""
    mdx_pages = {p for p in _pages_on_disk() if p.suffix == ".mdx"}
    if not mdx_pages:  # nothing to prove today, but the guard stays honest
        return
    assert mdx_pages <= set(collect_docs_files(DOCS_PATH))


def test_priority_markers_still_match_real_pages():
    """A marker that matches nothing is dead config, and silently stops prioritising."""
    collected = [p.as_posix() for p in collect_docs_files(DOCS_PATH)]
    dead = [m for m in DOCS_PRIORITY_MARKERS if not any(m in path for path in collected)]
    assert not dead, f"priority markers match no page on disk: {dead}"


def test_priority_pages_are_ordered_first():
    collected = collect_docs_files(DOCS_PATH)
    is_priority = [
        any(marker in page.as_posix() for marker in DOCS_PRIORITY_MARKERS) for page in collected
    ]
    assert is_priority == sorted(is_priority, reverse=True), (
        "priority pages must all precede non-priority pages"
    )


def test_declared_exclusions_still_exist():
    """A stale exclusion would quietly hide a real page."""
    for name in DOCS_FILES_TO_EXCLUDE:
        assert any(DOCS_PATH.rglob(name)), f"excluded file no longer exists: {name}"
