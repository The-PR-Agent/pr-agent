import re
import string
from bisect import bisect_left

_SPACE = " \t\n\v\f\r"
TICKS_RE = re.compile(r"`+")
_SPACES_RE = re.compile(r"[ \t\n\v\f\r]*")
_ANGLE_DEST_RE = re.compile(r"<(?:\\[\s\S]|[^\\<>\n])*>")
_TITLE_RE = re.compile(r"\"(?:[^\"]|(?<=\\)\")*\"|'(?:[^']|(?<=\\)')*'|\((?:[^()]|(?<=\\)[()])*\)")
_INLINE_TAG_RE = re.compile(
    r"<(?:[A-Za-z][\w.+-]*:[^\s<>]*|[^\s<>@]+@[^\s<>]+"
    r"|/?[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*+\s*+/?)>"
)
_RAW_HTML = (
    (re.compile(r"<!--"), "-->"),
    (re.compile(r"<!\[CDATA\["), "]]>"),
    (re.compile(r"<\?"), "?>"),
    (re.compile(r"<![A-Za-z]"), ">"),
)


def _bare_destination_end(text: str, pos: int, end: int) -> int | None:
    start = pos
    depth = 0
    while pos < end:
        char = text[pos]
        if char == "\\" and pos + 1 < end and text[pos + 1] in string.punctuation:
            pos += 1
        elif char == "(":
            depth += 1
            if depth > 32:
                return None
        elif char == ")":
            if not depth:
                break
            depth -= 1
        elif char in _SPACE:
            if pos == start:
                return None
            break
        pos += 1
    return pos if pos < end and not depth else None


def _link_tail_end(text: str, pos: int, end: int) -> int | None:
    pos = _SPACES_RE.match(text, pos + 1, end).end()
    angle = _ANGLE_DEST_RE.match(text, pos, end) if text.startswith("<", pos, end) else None
    dest_end = angle.end() if angle else _bare_destination_end(text, pos, end)
    if dest_end is None:
        return None
    pos = _SPACES_RE.match(text, dest_end, end).end()
    title = _TITLE_RE.match(text, pos, end) if pos > dest_end else None
    if title:
        pos = _SPACES_RE.match(text, title.end(), end).end()
    return pos + 1 if text.startswith(")", pos, end) else None


def _raw_html_end(text: str, pos: int, end: int, last: dict[str, int]) -> int | None:
    tag = _INLINE_TAG_RE.match(text, pos, end)
    if tag:
        return tag.end()
    for opener, closer in _RAW_HTML:
        start = opener.match(text, pos, end)
        if start and last[closer] >= start.end():
            return text.find(closer, start.end(), end) + len(closer)
    return None


def _spills(text: str, start: int, stop: int, end: int, last: dict[str, int]) -> bool:
    if text.find("`", start, stop) != -1 and last["`"] >= stop:
        return True
    pos = text.find("<", start + 1, stop)
    while pos != -1:
        if (_raw_html_end(text, pos, end, last) or 0) > stop:
            return True
        pos = text.find("<", pos + 1, stop)
    return False


def open_bracket_spans(text: str, start: int, end: int, ticks: dict[int, list[int]]) -> list[tuple[int, int]]:
    spans = []
    last = {closer: text.rfind(closer, start, end) for closer in ("`", *(closer for _, closer in _RAW_HTML))}
    depth = 0
    lax = False
    opened = pos = start
    while pos < end:
        before = depth
        char = text[pos]
        step = pos + 1
        if char == "\\" and step < end and text[step] in string.punctuation:
            step += 1
        elif char == "`" and not lax:
            step = TICKS_RE.match(text, pos, end).end()
            runs = ticks.get(step - pos, [])
            closer = bisect_left(runs, step)
            if closer < len(runs) and runs[closer] < end:
                step = runs[closer] + step - pos
        elif char == "<" and not lax:
            html_end = _raw_html_end(text, pos, end, last)
            lax = bool(html_end) and _spills(text, pos, html_end, end, last)
            if html_end and not lax:
                depth += text.count("[", pos, html_end)
                step = html_end
        elif char == "[":
            depth += 1
        elif char == "]" and depth and not lax:
            depth -= 1
            tail_end = _link_tail_end(text, step, end) if text.startswith("(", step, end) else None
            lax = bool(tail_end) and _spills(text, step, tail_end, end, last)
            if tail_end and not lax:
                depth += text.count("[", step, tail_end)
                step = tail_end

        if not before and depth:
            opened = pos
        elif before and not depth:
            spans.append((opened, pos))
        pos = step
    if depth:
        spans.append((opened, end))
    return spans
