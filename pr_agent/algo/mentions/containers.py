import re

QUOTE = None
THEMATIC_RE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE_RE = re.compile(r" {0,3}> ?")
_ITEM_RE = re.compile(r"( {0,3})([-+*]|\d{1,9}[.)])( +|$)")


def indent_of(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def continue_containers(containers: list[int | None], line: str, empty_tail: bool) -> tuple[int, str]:
    rest = line
    for depth, width in enumerate(containers):
        if width is QUOTE:
            marker = _QUOTE_RE.match(rest)
            if not marker:
                return depth, rest
            rest = rest[marker.end():]
        elif indent_of(rest) >= width:
            rest = rest[width:]
        elif not rest.strip() and not (empty_tail and depth == len(containers) - 1):
            rest = ""
        else:
            return depth, rest
    return len(containers), rest


def open_containers(rest: str, interrupting: bool) -> tuple[list[int | None], str]:
    opened: list[int | None] = []
    while not THEMATIC_RE.match(rest):
        quote = _QUOTE_RE.match(rest)
        if quote:
            opened.append(QUOTE)
            rest = rest[quote.end():]
            interrupting = False
            continue

        item = _ITEM_RE.match(rest)
        if not item:
            break
        leading, marker, spaces = item.groups()
        empty = not rest[item.end():].strip()
        if interrupting and (empty or (marker[0].isdigit() and int(marker[:-1]) != 1)):
            break

        width = len(leading) + len(marker) + 1 if empty or len(spaces) > 4 else item.end()
        opened.append(width)
        rest = "" if empty else rest[width:]
        interrupting = False
    return opened, rest
