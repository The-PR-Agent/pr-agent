#!/usr/bin/env python3
"""Verify every docs URL referenced from the codebase resolves in the built site.

PR-Agent posts absolute documentation links into real pull request comments
(`pr_agent/tools/pr_help_message.py`, `pr_agent/servers/help.py`), and the README
and package metadata link into the site too. A docs restructure that silently
breaks one of those is invisible until a user clicks it.

This runs against `docs/build`, so it checks anchors as well as paths -- the
built HTML is the only place the real heading slugs exist. Run it after
`npm run build`:

    python3 scripts/check_docs_urls.py [--build-dir docs/build]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_SITE_URL = "https://docs.pr-agent.ai"

# Where absolute docs links are written. `docs/` itself is excluded: links inside
# the site are relative and already checked by Docusaurus' own broken-link pass.
SEARCH_ROOTS = ("pr_agent",)
SEARCH_FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "pyproject.toml")
SEARCH_SUFFIXES = {".py", ".md", ".toml"}

URL_RE = re.compile(re.escape(DOCS_SITE_URL) + r"[^\s\"'`)\],<>]*")
ID_RE = re.compile(r'id="([^"]+)"')


def _iter_source_files():
    for name in SEARCH_FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for root in SEARCH_ROOTS:
        for path in (ROOT / root).rglob("*"):
            if path.is_file() and path.suffix in SEARCH_SUFFIXES:
                yield path


def collect_referenced_urls() -> dict[str, list[str]]:
    """Map each referenced URL to the files that reference it."""
    found: dict[str, list[str]] = {}
    for path in _iter_source_files():
        # Deliberately not guarded: every searched file is text, and skipping one we
        # cannot read would silently check fewer URLs. A read error should fail the run.
        text = path.read_text(encoding="utf-8")
        for match in URL_RE.findall(text):
            url = match.rstrip(".,;:")
            found.setdefault(url, []).append(path.relative_to(ROOT).as_posix())
    return found


def resolve(url: str, build_dir: Path) -> tuple[Path | None, str]:
    """Return the built HTML file backing `url`, plus its anchor (may be empty)."""
    route, _, anchor = url[len(DOCS_SITE_URL):].partition("#")
    route = route.strip("/")
    page = build_dir / route / "index.html" if route else build_dir / "index.html"
    return (page if page.is_file() else None), anchor


ASSET_HREF_RE = re.compile(r'href="(/[^"#?]+\.[a-z0-9]{2,5}/?)"', re.I)
# Routes are directories; these are file extensions that must resolve to a real file.
ASSET_SUFFIXES = {
    ".png", ".webp", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".pdf", ".zip", ".txt", ".json", ".xml", ".csv",
}


def check_asset_links(build_dir: Path) -> list[str]:
    """Check that every in-page link to a static file resolves to a file in the build.

    `trailingSlash: true` appends a slash to asset links that go through the
    asset pipeline, which turns them into 404s. Docusaurus' own broken-link pass
    checks page routes, not files, so this is not covered by the build.
    """
    failures = []
    for page in sorted(build_dir.rglob("*.html")):
        for href in set(ASSET_HREF_RE.findall(page.read_text(encoding="utf-8", errors="ignore"))):
            target = href.rstrip("/")
            if Path(target).suffix.lower() not in ASSET_SUFFIXES:
                continue
            where = f"/{page.relative_to(build_dir).parent.as_posix()}/".replace("/./", "/")
            on_disk = build_dir / target.lstrip("/")
            if href.endswith("/"):
                # The file is there, but the served URL has a slash appended and
                # will 404. Link such files with `pathname://` to bypass routing.
                failures.append(f"  {href}\n      asset link has a trailing slash (404) on {where}")
            elif not on_disk.is_file():
                failures.append(f"  {href}\n      asset link points at a missing file, on {where}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", default="docs/build", type=Path)
    args = parser.parse_args()

    build_dir = args.build_dir if args.build_dir.is_absolute() else ROOT / args.build_dir
    if not build_dir.is_dir():
        print(f"error: no built site at {build_dir} -- run `npm run build` in docs/ first")
        return 2

    referenced = collect_referenced_urls()
    failures: list[str] = []
    for url in sorted(referenced):
        page, anchor = resolve(url, build_dir)
        where = ", ".join(sorted(set(referenced[url])))
        if page is None:
            failures.append(f"  {url}\n      no such page in the built site (referenced by {where})")
            continue
        if anchor and anchor not in set(ID_RE.findall(page.read_text(encoding="utf-8"))):
            failures.append(f"  {url}\n      page exists but has no #{anchor} (referenced by {where})")

    asset_failures = check_asset_links(build_dir)
    print(f"checked {len(referenced)} documentation URLs referenced from the codebase")
    print(f"checked static-asset links across {sum(1 for _ in build_dir.rglob('*.html'))} built pages")
    failures += asset_failures
    if failures:
        print(f"\n{len(failures)} broken:")
        print("\n".join(failures))
        return 1
    print("all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
