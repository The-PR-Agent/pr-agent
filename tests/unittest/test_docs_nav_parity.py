"""Guards for issue #3198: keep the two documentation navigations in step.

The docs are rendered twice. `docs/mkdocs.yml` drives the mkdocs site, and
`docs/docs/summary.md` is the GitBook table of contents, wired up in
`docs/docs/.gitbook.yaml` as `summary: ./summary.md`. `extending_pr_agent.md`
tells contributors to register every new page in both, so the two files are
expected to list exactly the same set of pages — and nothing enforces that
today. These tests do.
"""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
PAGES = DOCS / "docs"
MKDOCS = DOCS / "mkdocs.yml"
SUMMARY = PAGES / "summary.md"

# Pages that are deliberately outside both navigations.
NOT_IN_NAV = {
    # summary.md is a navigation file itself, not a page of the site.
    "summary.md",
}


def _mkdocs_pages() -> set[str]:
    """Page paths listed in the mkdocs nav, ignoring commented-out entries."""
    lines = [
        line
        for line in MKDOCS.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return set(re.findall(r"'([^']+\.md)'", "\n".join(lines)))


def _summary_pages() -> set[str]:
    """Page paths linked from the GitBook summary."""
    return set(re.findall(r"\(([^()]+\.md)\)", SUMMARY.read_text(encoding="utf-8")))


def test_every_page_is_reachable_from_a_nav():
    """No page under docs/docs is orphaned from both navigations."""
    on_disk = {p.relative_to(PAGES).as_posix() for p in PAGES.rglob("*.md")}
    reachable = _mkdocs_pages() | _summary_pages() | NOT_IN_NAV
    assert not on_disk - reachable, (
        "pages exist but no navigation points at them: "
        f"{sorted(on_disk - reachable)}"
    )


def test_navs_list_the_same_pages():
    """mkdocs.yml and summary.md must not drift apart."""
    mkdocs, summary = _mkdocs_pages(), _summary_pages()
    assert not mkdocs - summary, (
        f"in mkdocs.yml but missing from summary.md: {sorted(mkdocs - summary)}"
    )
    assert not summary - mkdocs, (
        f"in summary.md but missing from mkdocs.yml: {sorted(summary - mkdocs)}"
    )


def test_navs_do_not_point_at_missing_files():
    """Every navigation entry resolves to a file that exists."""
    for label, pages in (("mkdocs.yml", _mkdocs_pages()), ("summary.md", _summary_pages())):
        missing = sorted(page for page in pages if not (PAGES / page).exists())
        assert not missing, f"{label} references missing pages: {missing}"


def test_declared_exclusions_still_exist():
    """Keeps NOT_IN_NAV honest: a stale entry there would hide a real orphan."""
    missing = sorted(name for name in NOT_IN_NAV if not (PAGES / name).exists())
    assert not missing, f"NOT_IN_NAV lists files that no longer exist: {missing}"
