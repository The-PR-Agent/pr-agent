"""Helpers for stripping a Markdown code fence that wraps a whole model answer.

Kept in its own module so the tools that need it (the changelog and the
description paths) do not import each other.
"""

import re

# Fence lines: 3+ backticks, optionally indented. Four-backtick fences are
# standard Markdown for embedding ``` inside a block.
_FENCE_LINE_RE = re.compile(r"(?m)^[^\S\n]*`{3,}")
# A long run of backticks inside a fence-line check is scanned once per line
# (the regex has no backtracking surface), so the whole helper stays linear in
# the input size — the earlier whole-text regex
# whose lazy body made every start position rescan the closing run (O(n²)).


def strip_wrapping_code_fence(text: str) -> str:
    """Remove a fence that wraps the whole answer, leaving the content untouched.

    `str.strip("`")` would remove characters rather than the fence, so an entry ending in an
    inline code span (`` - Handle `None` in `parse()` ``) loses its closing backtick and the
    corrupted line is committed to CHANGELOG.md.

    The function is idempotent, because the answer is stripped once when the model replies and
    again before the changelog is built. Whether a trailing fence is a wrapper or the closer of a
    code block the entry ends with comes down to whether the rest of the text is balanced: a
    leftover fence either unterminates the block, or leaves a wrapper behind that then swallows
    the rest of CHANGELOG.md when the file is committed.
    """
    # Line-structured rewrite of the earlier whole-text regex: the closing
    # fence can only be the last line and the opening fence only the first,
    # so there is no per-position rescanning — linear in the input size at
    # every shape.
    lines = text.split("\n")

    first = lines[0].strip() if lines else ""
    open_match = _FENCE_LINE_RE.match(first) if first else None
    if open_match and len(lines) >= 2:
        body_lines = lines[1:]
        had_open = True
    else:
        body_lines = lines
        had_open = False

    if not body_lines:
        return text
    last = body_lines[-1]
    # A bare fence only: a trailing line such as "```python" is content, not a
    # wrapper closer, and dropping it would lose the last line of the entry.
    close_match = re.fullmatch(r"`{3,}", last.strip()) if last.strip() else None
    # No closing fence → no wrapper at all (the old regex required
    # `\n?`{3,}+\s*\Z` after the body and simply did not match otherwise).
    if not close_match:
        return text

    body = "\n".join(body_lines[:-1])
    body_fences = len(_FENCE_LINE_RE.findall(body))
    # Balance check, mirroring the old regex: with an opening fence consumed
    # the trailing fence closes the wrapper; without one it closes it only
    # when the body's own fences are balanced — otherwise the trailing fence
    # closes an unterminated inner block and the whole text is kept verbatim.
    if had_open or body_fences % 2 == 0:
        return body
    return text
