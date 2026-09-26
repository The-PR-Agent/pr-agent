"""Every docs URL hardcoded in the codebase must point at a page that exists.

PR-Agent writes absolute documentation links into pull request comments, so a
link to a page that was renamed or removed ships to users silently. This is the
fast half of that guard: it resolves URL *paths* against the docs sources and
needs no site build, so it runs on every pull request.

Anchors are checked by `scripts/check_docs_urls.py` in the `docs-ci` workflow,
against the built HTML -- the only place real heading slugs exist.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "docs" / "docs"

_spec = importlib.util.spec_from_file_location("check_docs_urls", ROOT / "scripts" / "check_docs_urls.py")
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)

DOCS_SITE_URL = _checker.DOCS_SITE_URL
PAGE_SUFFIXES = (".md", ".mdx")


def _source_page_for(route: str) -> Path | None:
    """Map a site route to the page file that produces it, mirroring Docusaurus."""
    route = route.strip("/")
    candidates = [Path(route or "index")]
    if route:
        # A route may be produced either by `<route>.md` or by `<route>/index.md`.
        candidates.append(Path(route) / "index")
    for candidate in candidates:
        for suffix in PAGE_SUFFIXES:
            page = PAGES / candidate.with_suffix(suffix)
            if page.is_file():
                return page
    return None


def test_referenced_docs_urls_point_at_existing_pages():
    referenced = _checker.collect_referenced_urls()
    assert referenced, "found no docs URLs at all -- the scanner is probably broken"

    broken = {}
    for url, sources in referenced.items():
        route = url[len(DOCS_SITE_URL):].partition("#")[0]
        if _source_page_for(route) is None:
            broken[url] = sorted(set(sources))

    assert not broken, "documentation URLs point at pages that do not exist: " + "; ".join(
        f"{url} (referenced by {', '.join(src)})" for url, src in sorted(broken.items())
    )


def test_scanner_sees_the_known_reference_sites():
    """Keeps the scanner honest: if it silently stops reading a file, this fails."""
    sources = {src for sources in _checker.collect_referenced_urls().values() for src in sources}
    for expected in ("README.md", "pr_agent/servers/help.py", "pr_agent/tools/pr_help_message.py"):
        assert expected in sources, f"scanner no longer finds docs URLs in {expected}"
