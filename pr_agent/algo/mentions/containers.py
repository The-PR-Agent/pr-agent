import re
from bisect import bisect_left

QUOTE = None
THEMATIC_RE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE_RE = re.compile(r" {0,3}> ?")
_ITEM_RE = re.compile(r"( {0,3})([-+*]|\d{1,9}[.)])( +|$)")


def indent_of(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _thematic_from(line: str) -> int:
    tail = line.rstrip(" \t")[-1:]
    if tail not in ("-", "*", "_"):
        return len(line) + 1
    return len(line.rstrip(tail + " \t"))


def continue_containers(
    containers: list[int | None], quotes: list[int], line: str, empty_tail: bool
) -> tuple[int, str]:
    pos = 0
    blank_from = len(line.rstrip())
    last = len(containers) - 1
    for depth, width in enumerate(containers):
        if width is QUOTE:
            marker = _QUOTE_RE.match(line, pos)
            if not marker:
                return depth, line[pos:]
            pos = marker.end()
        elif line.startswith(" " * width, pos):
            pos += width
        elif pos >= blank_from and not (empty_tail and depth == last):
            # blank
            index = bisect_left(quotes, depth)
            if index < len(quotes):
                return quotes[index], ""
            return (last if empty_tail else len(containers)), ""
        else:
            return depth, line[pos:]
    return len(containers), line[pos:]


def open_containers(rest: str, interrupting: bool) -> tuple[list[int | None], str]:
    opened: list[int | None] = []
    pos = 0
    blank_from = len(rest.rstrip())
    thematic_from = _thematic_from(rest)
    while pos < thematic_from or not THEMATIC_RE.match(rest, pos):
        quote = _QUOTE_RE.match(rest, pos)
        if quote:
            opened.append(QUOTE)
            pos = quote.end()
            interrupting = False
            continue

        item = _ITEM_RE.match(rest, pos)
        if not item:
            break
        leading, marker, spaces = item.groups()
        empty = item.end() >= blank_from
        if interrupting and (empty or (marker[0].isdigit() and int(marker[:-1]) != 1)):
            break

        width = len(leading) + len(marker) + 1 if empty or len(spaces) > 4 else item.end() - pos
        opened.append(width)
        pos = len(rest) if empty else pos + width
        interrupting = False
    return opened, rest[pos:]
