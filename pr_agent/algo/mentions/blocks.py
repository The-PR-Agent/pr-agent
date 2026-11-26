import re
from bisect import bisect_left

from .containers import QUOTE, THEMATIC_RE, continue_containers, indent_of, open_containers

_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ATX_RE = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_SETEXT_RE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
_TABLE_CELL_RE = re.compile(r":?-+:?")
_TABLE_PIPE_RE = re.compile(r"(?<!\\)\|")
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
    "dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|"
    "header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|"
    "param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
# CommonMark HTML block starts; an empty end tuple closes the block at a blank line
_HTML_BLOCK_STARTS = (
    (re.compile(r"<(?:pre|script|style|textarea)(?:[\s>]|$)", re.IGNORECASE),
     ("</pre>", "</script>", "</style>", "</textarea>")),
    (re.compile(r"<!--"), ("-->",)),
    (re.compile(r"<\?"), ("?>",)),
    (re.compile(r"<![A-Za-z]"), (">",)),
    (re.compile(r"<!\[CDATA\["), ("]]>",)),
    (re.compile(rf"</?(?:{_HTML_BLOCK_TAGS})(?:[\s>]|/>|$)", re.IGNORECASE), ()),
)
_TAG_LINE_RE = re.compile(
    r"(?:<[A-Za-z][A-Za-z0-9-]*+(?:[ \t\v\f]++[A-Za-z_:][A-Za-z0-9_.:-]*+(?:[ \t\v\f]*+=[ \t\v\f]*+"
    r"(?:[^ \t\v\f\"'=<>`]++|'[^']*+'|\"[^\"]*+\"))?+)*+[ \t\v\f]*+/?>|</[A-Za-z][A-Za-z0-9-]*+[ \t\v\f]*+>)[ \t\f]*$"
)


def _html_block_ends(line: str, interrupting: bool) -> tuple[str, ...] | None:
    for start, ends in _HTML_BLOCK_STARTS:
        if start.match(line):
            return ends
    return () if not interrupting and _TAG_LINE_RE.match(line) else None


def _table_cells(line: str) -> list[str]:
    return _TABLE_PIPE_RE.split(line.strip().removeprefix("|").removesuffix("|"))


def _cell_cuts(line: str, offset: int) -> list[int]:
    return [offset + pipe.start() for pipe in _TABLE_PIPE_RE.finditer(line)]


def _delimiter_cells(line: str) -> int:
    cells = _table_cells(line) if "|" in line else []
    return len(cells) if cells and all(_TABLE_CELL_RE.fullmatch(cell.strip()) for cell in cells) else 0


def _opens_fence(line: str) -> re.Match | None:
    fence = _FENCE_RE.match(line)
    if fence and not (fence.group(1)[0] == "`" and "`" in line[fence.end():]):
        return fence
    return None


def _starts_html(line: str, interrupting: bool) -> tuple[str, ...] | None:
    return _html_block_ends(line.lstrip(" "), interrupting) if indent_of(line) <= 3 else None


def _continues_paragraph(line: str) -> bool:
    return bool(line.strip()) and not (
        _opens_fence(line) or _ATX_RE.match(line) or THEMATIC_RE.match(line)
        or _starts_html(line, False) is not None)


def line_flags(text: str) -> tuple[dict[int, bool], list[int], set[int]]:
    code_by_start: dict[int, bool] = {}
    html_starts: set[int] = set()
    boundaries: list[int] = []
    containers: list[int | None] = []
    quotes: list[int] = []
    fence_char = None
    fence_size = 0
    html_ends: tuple[str, ...] | None = None
    indented_code = False
    in_table = table_visited = False
    empty_item = False
    paragraph = False
    header = ""
    boundary_next = False
    prev_start = None
    offset = 0

    for raw_line in text.split("\n"):
        line = raw_line.removesuffix("\r").expandtabs(4)
        is_code = False
        in_html = False
        cuts = []
        boundary = boundary_next
        boundary_next = False
        depth, rest = continue_containers(containers, quotes, line, empty_item)
        leaf_continues = depth == len(containers)
        empty_item = empty_item and leaf_continues and not rest.strip()

        if leaf_continues and fence_char is not None:
            is_code = True
            fence = _FENCE_RE.match(rest)
            if (fence and fence.group(1)[0] == fence_char and len(fence.group(1)) >= fence_size
                    and not rest[fence.end():].strip()):
                fence_char = None
        elif leaf_continues and html_ends is not None:
            in_html = boundary = True
            if html_ends and any(end in rest.lower() for end in html_ends):
                html_ends = None
                boundary_next = True
            elif not html_ends and not rest.strip():
                html_ends = None
        else:
            # containers
            opened, rest = open_containers(rest, paragraph and leaf_continues)
            if opened:
                empty_item = opened[-1] is not QUOTE and not rest.strip()
            lazy = not leaf_continues and not opened and paragraph and _continues_paragraph(rest)
            if opened or not (leaf_continues or lazy):
                del containers[depth:]
                del quotes[bisect_left(quotes, depth):]
                quotes.extend(depth + index for index, width in enumerate(opened) if width is QUOTE)
                containers.extend(opened)
                fence_char = html_ends = None
                indented_code = in_table = paragraph = False
                boundary = True

            # leaf
            stripped = rest.strip()
            indent = indent_of(rest)
            fence = _opens_fence(rest)
            html_block = _starts_html(rest, paragraph) if stripped else None
            if lazy:
                pass
            elif indented_code and (not stripped or indent >= 4):
                is_code = True
            elif stripped and indent >= 4 and not paragraph:
                indented_code = is_code = boundary = True
            elif fence:
                indented_code = paragraph = False
                fence_char, fence_size = fence.group(1)[0], len(fence.group(1))
                is_code = boundary = True
            elif html_block is not None:
                indented_code = paragraph = in_table = False
                in_html = boundary = True
                if html_block and any(end in rest.lstrip(" ")[1:].lower() for end in html_block):
                    boundary_next = True
                else:
                    html_ends = html_block
            else:
                indented_code = False
                single_line_block = bool(
                    _ATX_RE.match(rest) or THEMATIC_RE.match(rest) or (paragraph and _SETEXT_RE.match(rest)))
                delimiter = _delimiter_cells(rest) if paragraph and indent <= 3 and not table_visited else 0
                if not stripped:
                    boundary = True
                    in_table = paragraph = False
                elif delimiter and delimiter == len(_table_cells(header)):
                    boundary = in_table = True
                    paragraph = False
                    if prev_start is not None and (not boundaries or boundaries[-1] != prev_start):
                        boundaries.append(prev_start)
                    boundaries += _cell_cuts(text[prev_start:offset - 1], prev_start)
                elif in_table and not single_line_block:
                    boundary = True
                    cuts = _cell_cuts(raw_line, offset)
                elif single_line_block:
                    boundary = boundary_next = True
                    in_table = paragraph = False
                else:
                    table_visited = paragraph and (table_visited or delimiter > 0)
                    in_table = False
                    paragraph = True
                    header = rest

        code_by_start[offset] = is_code
        if in_html:
            html_starts.add(offset)
        if boundary:
            boundaries.append(offset)
        boundaries += cuts
        prev_start = offset
        offset += len(raw_line) + 1

    return code_by_start, boundaries, html_starts
