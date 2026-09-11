"""Files that probably do not ship (docs, mockups, fixtures) are reviewed last and summarized when budget is tight.

Never silently excluded: build scripts and dynamically loaded assets can affect production, so the
default only reorders. Ignoring is proposed to a human as a config snippet with the tokens it would save.
"""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Callable, Iterable, Mapping

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


DEFAULT_LOW_PRIORITY_MAX_TOKENS_PER_FILE = 3000


def cap_low_priority_files(files: list, globs: Iterable[str], max_tokens: int,
                           count_tokens: Callable[[str], int]) -> tuple[list, list[str]]:
    """Split `files` into the ones to review and the low-priority ones to summarize instead.

    Ordering alone only helps when the token budget binds. On a PR whose budget never binds,
    a large mockup or design document is reviewed in full and can take half the run's tokens
    for output nobody ships (R-9, tests/eval/BASELINE.md). So a low-priority file whose patch
    is over `max_tokens` is summarized regardless of budget.

    `max_tokens` <= 0 disables the cap. Nothing is dropped silently: the returned paths are
    reported in the review footer and marked `low_priority_summary` in the coverage ledger.
    """
    if max_tokens <= 0:
        return list(files), []
    globs = list(globs)
    kept, summarized = [], []
    for file in files:
        patch = getattr(file, "patch", None)
        if not is_low_priority(file.filename, globs) or not patch:
            kept.append(file)
            continue
        try:
            tokens = count_tokens(patch)
        except Exception:
            # A counter that cannot price this patch must not drop the file: review it.
            kept.append(file)
            continue
        if tokens > max_tokens:
            summarized.append(file.filename)
        else:
            kept.append(file)
    if not kept:
        # Capping every file leaves an empty diff, and an empty diff ends the run with no
        # prediction and so no published review at all - the PR would be silently skipped.
        # The cap exists to stop low-priority files starving real code; with no other code in
        # the PR there is nothing to protect, so review them.
        return list(files), []
    return kept, summarized


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
