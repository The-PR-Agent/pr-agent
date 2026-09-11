"""Files that probably do not ship (docs, mockups, fixtures) are reviewed last and summarized when budget is tight.

Never silently excluded: build scripts and dynamically loaded assets can affect production, so the
default only reorders. Ignoring is proposed to a human as a config snippet with the tokens it would save.
"""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Iterable, Mapping

DEFAULT_LOW_PRIORITY_GLOBS = ("docs/**", "design/**", "mockups/**", "**/fixtures/**", "**/*.md")


def _match(path: str, glob: str) -> bool:
    # `**/dir/**` must be handled before the plain `prefix/**` branch: otherwise prefix becomes
    # `**/dir` and never matches a real path.
    if glob.startswith("**/") and glob.endswith("/**"):
        mid = glob[3:-3]
        if not mid:
            return True
        return (
            path == mid
            or path.startswith(mid + "/")
            or path.endswith("/" + mid)
            or f"/{mid}/" in f"/{path}/"
        )
    if glob.endswith("/**"):
        prefix = glob[:-3]
        return path.startswith(prefix + "/") or path == prefix
    if glob.startswith("**/"):
        tail = glob[3:]
        return (
            fnmatch.fnmatch(path, tail)
            or any(fnmatch.fnmatch(path[i + 1 :], tail) for i, c in enumerate(path) if c == "/")
            or fnmatch.fnmatch(path, glob)
        )
    return fnmatch.fnmatch(path, glob)


def is_low_priority(path: str, globs: Iterable[str]) -> bool:
    return any(_match(path, g) for g in globs)


def order_files_by_priority(files: list, globs: Iterable[str]) -> list:
    globs = list(globs)
    high = [f for f in files if not is_low_priority(f.filename, globs)]
    low = [f for f in files if is_low_priority(f.filename, globs)]
    return high + low


def propose_ignore_globs(low_files: Iterable[str], tokens_by_file: Mapping[str, int]) -> list[tuple[str, int]]:
    by_dir: dict[str, int] = defaultdict(int)
    for path in low_files:
        top = path.split("/", 1)[0] if "/" in path else path
        by_dir[f"{top}/**" if "/" in path else path] += int(tokens_by_file.get(path, 0))
    return sorted(by_dir.items(), key=lambda kv: kv[1], reverse=True)


def render_ignore_proposal(proposals: list[tuple[str, int]]) -> str:
    if not proposals:
        return ""
    globs = ", ".join(f'"{g}"' for g, _ in proposals)
    lines = [
        "<details><summary>Suggested <code>.pr_agent.toml</code> ignore rules</summary>",
        "",
        "These files look like documentation or mockups. Reviewing them cost tokens without affecting shipped code:",
        "",
    ]
    lines += [f"- `{g}` · ~{t:,} tokens" for g, t in proposals]
    lines += ["", "```toml", "[ignore]", f"glob = [{globs}]", "```", "</details>"]
    return "\n".join(lines)
