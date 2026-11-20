import re
import string
import unicodedata
from bisect import bisect_left

from .blocks import line_flags

_GITLAB_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,31}://[^\s<>`()\[\]\"'*~|\\]+")
_GITHUB_URL_RE = re.compile(r"(?<![A-Za-z])(?i:https?)://(?=\S)|(?<![^\s*_~(])www\.")
_ANGLE_RE = re.compile(r"(?<!\\)<(?i:https?|mailto):[^\x00-\x20<>\x7f]*>")
_TAIL_RE = re.compile(r"[^\s<>`()\[\]\"'*~|\\]*")
_SPACE = " \t\n\v\f\r"
_CONTAINER_PREFIX_RE = re.compile(r"^[ \t>]+", re.MULTILINE)
_TICKS_RE = re.compile(r"`+")
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


def _is_host_char(char: str) -> bool:
    return not (char.isspace() or char in string.punctuation or unicodedata.category(char).startswith("P"))


def _domain_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and (text[end] in "._-" or _is_host_char(text[end])):
        end += 1
    return end


def _is_valid_domain(domain: str) -> bool:
    return _is_host_char(domain[:1] or " ") and "_" not in "".join(domain.split(".")[-2:])


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


def _open_bracket_spans(text: str, start: int, end: int, ticks: dict[int, list[int]]) -> list[tuple[int, int]]:
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
            step = _TICKS_RE.match(text, pos, end).end()
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


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    code_by_start, boundaries, html_starts = line_flags(text)
    masked = _CONTAINER_PREFIX_RE.sub(lambda prefix: " " * len(prefix.group()), text)
    ticks: dict[int, list[int]] = {}
    for run in _TICKS_RE.finditer(masked):
        ticks.setdefault(len(run.group()), []).append(run.start())

    spans = []
    stops = set(boundaries)
    run_start = None
    for offset, is_code in [*code_by_start.items(), (len(text) + 1, True)]:
        raw = is_code or offset in html_starts
        if run_start is not None and (raw or offset in stops):
            spans += _open_bracket_spans(masked, run_start, offset - 1, ticks)
            run_start = None
        if run_start is None and not raw:
            run_start = offset
    return spans


def gitlab_autolinks(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _GITLAB_URL_RE.finditer(text)]


def github_autolinks(text: str) -> list[tuple[int, int]]:
    spans = [match.span() for match in _ANGLE_RE.finditer(text)]
    brackets = _bracket_spans(text)
    bracket_starts = [start for start, _ in brackets]
    covered = 0
    for match in _GITHUB_URL_RE.finditer(text):
        if match.start() < covered:
            continue
        inside = bisect_left(bracket_starts, match.start() + 1) - 1
        if inside >= 0 and match.start() < brackets[inside][1]:
            continue
        domain = match.start() if match.group().startswith("www") else match.end()
        covered = _domain_end(text, domain)
        if _is_valid_domain(text[domain:covered]):
            covered = _TAIL_RE.match(text, match.end()).end()
            spans.append((match.start(), covered))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged
