"""Guards for issue #3198: keep the documentation navigation in step with the pages.

The docs used to be rendered twice — `docs/mkdocs.yml` drove the mkdocs site and
`docs/docs/summary.md` was the GitBook table of contents — so the two files could
drift apart. Docusaurus renders the site from a single navigation, `docs/sidebars.js`,
which removes that failure mode entirely. What is still worth enforcing is that the
one remaining navigation and the pages on disk agree in both directions.
"""

import json
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
PAGES = DOCS / "docs"
SIDEBARS = DOCS / "sidebars.js"

# Pages that are deliberately outside the navigation.
NOT_IN_NAV: set[str] = {
    # The landing page at `/`, reached from the navbar logo rather than the sidebar.
    "index",
}


def _sidebar_ids() -> set[str]:
    """Doc ids referenced by sidebars.js, including category `link` targets."""
    source = SIDEBARS.read_text(encoding="utf-8")
    # Strip line comments so a commented-out entry does not count as navigation.
    source = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )
    return set(re.findall(r"id:\s*'([^']+)'", source))


def _page_ids() -> set[str]:
    """Doc ids for every page on disk, as Docusaurus derives them."""
    return {
        page.relative_to(PAGES).with_suffix("").as_posix()
        for page in [*PAGES.rglob("*.md"), *PAGES.rglob("*.mdx")]
    }


def test_every_page_is_reachable_from_the_nav():
    """No page under docs/docs is orphaned from sidebars.js."""
    orphans = _page_ids() - _sidebar_ids() - NOT_IN_NAV
    assert not orphans, (
        f"pages exist but the navigation does not point at them: {sorted(orphans)}"
    )


def test_nav_does_not_point_at_missing_files():
    """Every navigation entry resolves to a page that exists."""
    missing = sorted(_sidebar_ids() - _page_ids())
    assert not missing, f"sidebars.js references missing pages: {missing}"


def test_declared_exclusions_still_exist():
    """Keeps NOT_IN_NAV honest: a stale entry there would hide a real orphan."""
    stale = sorted(name for name in NOT_IN_NAV if name not in _page_ids())
    assert not stale, f"NOT_IN_NAV lists pages that no longer exist: {stale}"


def test_every_page_declares_front_matter():
    """Docusaurus takes the sidebar label and page title from front matter."""
    without = sorted(
        page.relative_to(PAGES).as_posix()
        for page in [*PAGES.rglob("*.md"), *PAGES.rglob("*.mdx")]
        if not page.read_text(encoding="utf-8").startswith("---\n")
    )
    assert not without, f"pages missing YAML front matter: {without}"


def test_docs_package_pins_a_docusaurus_build():
    """The CI workflow runs `npm ci`, so the manifest must stay installable."""
    manifest = json.loads((DOCS / "package.json").read_text(encoding="utf-8"))
    assert "@docusaurus/core" in manifest["dependencies"]
    assert (DOCS / "package-lock.json").exists(), "npm ci requires a committed lockfile"
