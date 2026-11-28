"""Helpers for recognizing Markdown code fences.

Kept in its own module so the commands that need it (the changelog and the
description paths) do not import each other.
"""

import re
from collections.abc import Iterator
from io import StringIO

# Fence lines: 3+ backticks, optionally indented. Four-backtick fences are
# standard Markdown for embedding ``` inside a block.
_OPEN_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})")
_CLOSING_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})$")
_QUOTED_CLOSING_FENCE_RE = re.compile(r'(?:^|(?<=[\r\n])) {0,3}(?:`{3,}|~{3,})[ \t]*"(?=[ \t\r\n]|$)')


def fenced_code_ranges(text: str, *, argument_ends: list[int] | None = None) -> Iterator[tuple[int, int]]:
    """Yield closed Markdown fence ranges, optionally within a quoted command argument."""
    closing_quotes = set()
    if argument_ends:
        closing_quotes = {end - 1 for end in argument_ends if end and text[end - 1] == '"'}
        # Positional prose can follow the closing quote before the trailing options.
        if argument_ends[-1] - 1 not in closing_quotes:
            last_quote = None
            for match in _QUOTED_CLOSING_FENCE_RE.finditer(text, 0, argument_ends[-1]):
                last_quote = match.end() - 1
            if last_quote is not None:
                closing_quotes.add(last_quote)
    opener = ""
    start = offset = 0
    for line in StringIO(text, newline=""):
        body = line.rstrip("\r\n")
        stripped = body.lstrip(" ")
        if len(body) - len(stripped) <= 3:
            if opener:
                if stripped.startswith(opener):
                    if closing_quotes and (match := _QUOTED_CLOSING_FENCE_RE.match(body)):
                        quote = offset + match.end() - 1
                        if quote in closing_quotes:
                            # Later fences can belong to a separate quoted option.
                            yield start, quote
                            return
                    if _CLOSING_FENCE_RE.fullmatch(stripped.rstrip(" \t")):
                        yield start, offset + len(body)
                        opener = ""
            elif match := _OPEN_FENCE_RE.match(stripped):
                fence = match["fence"]
                if fence[0] != "`" or "`" not in stripped[match.end():]:
                    opener, start = fence, offset
        offset += len(line)


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

    Both backtick and tilde fences are supported, and a closing fence must use the opening
    character and be at least as long as it (CommonMark), so a shorter line inside a longer
    wrapper stays content.
    """
    # The closing fence can only be the last line and the opening fence only the first,
    # so the cost is linear in the input size.
    lines = text.split("\n")

    first = lines[0].strip() if lines else ""
    open_match = _OPEN_FENCE_RE.match(first) if first else None
    if open_match and len(lines) >= 2:
        opening_fence = open_match.group("fence")
        body_lines = lines[1:]
        had_open = True
    else:
        opening_fence = ""
        body_lines = lines
        had_open = False

    if not body_lines:
        return text
    last = body_lines[-1]
    # A bare fence only: a trailing line such as "```python" is content, not a
    # wrapper closer, and dropping it would lose the last line of the entry.
    close_match = _CLOSING_FENCE_RE.match(last.strip()) if last.strip() else None
    # No closing fence means no wrapper at all.
    if not close_match:
        return text
    closing_fence = close_match.group("fence")
    if had_open and (closing_fence[0] != opening_fence[0]
                     or len(closing_fence) < len(opening_fence)):
        return text
    fence_char = opening_fence[0] if had_open else closing_fence[0]

    body = "\n".join(body_lines[:-1])
    # With an opening fence consumed, the trailing fence closes the wrapper. Without one it
    # closes it only when the body has no unterminated block of the same character; otherwise
    # it closes that inner block and the whole text is kept verbatim.
    if had_open or not _has_unterminated_fence(body, fence_char):
        return body
    return text


def _has_unterminated_fence(body: str, fence_char: str) -> bool:
    """Track open/close state so a shorter fence inside a longer block stays content."""
    active = ""
    for line in body.split("\n"):
        stripped = line.strip()
        if not active:
            match = _OPEN_FENCE_RE.match(stripped)
            if match and match.group("fence")[0] == fence_char:
                active = match.group("fence")
            continue
        match = _CLOSING_FENCE_RE.match(stripped)
        if match:
            candidate = match.group("fence")
            if candidate[0] == active[0] and len(candidate) >= len(active):
                active = ""
    return bool(active)
