import re

_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ATX_RE = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_THEMATIC_RE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_SETEXT_RE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
_QUOTE_RE = re.compile(r" {0,3}>")
_LIST_RE = re.compile(r"( *)([-+*]|\d{1,9}[.)])( +|$)")
_TABLE_CELL_RE = re.compile(r":?-+:?")
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


def _html_block_ends(line: str) -> tuple[str, ...] | None:
    for start, ends in _HTML_BLOCK_STARTS:
        if start.match(line):
            return ends
    return None


def _is_table_delimiter(line: str) -> bool:
    if "|" not in line:
        return False
    cells = line.strip()
    cells = cells.removeprefix("|")
    cells = cells.removesuffix("|")
    parts = cells.split("|")
    if len(parts) < 2:
        return False
    return all(_TABLE_CELL_RE.fullmatch(part.strip()) for part in parts)


def line_flags(text: str) -> tuple[dict[int, bool], list[int], set[int]]:
    code_by_start: dict[int, bool] = {}
    html_starts: set[int] = set()
    boundaries: list[int] = []
    fence_char = None
    fence_size = 0
    html_ends: tuple[str, ...] | None = None
    indented_code = False
    list_indents: list[int] = []
    in_table = False
    prev_blank = True
    prev_paragraph = False
    boundary_next = False
    prev_start = None
    offset = 0

    for raw_line in text.split("\n"):
        line = raw_line.removesuffix("\r")
        stripped = line.strip()
        is_code = False
        boundary = boundary_next
        boundary_next = False
        paragraph = False
        in_html = html_ends is not None

        if in_html:
            boundary = True
            if html_ends and any(end in line.lower() for end in html_ends):
                html_ends = None
                boundary_next = True
            elif not html_ends and not stripped:
                html_ends = None
        elif fence_char is not None:
            is_code = True
            fence = _FENCE_RE.match(line)
            if (fence and fence.group(1)[0] == fence_char and len(fence.group(1)) >= fence_size
                    and not line[fence.end():].strip()):
                fence_char = None
                fence_size = 0
        else:
            fence = _FENCE_RE.match(line)
            if fence and not (fence.group(1)[0] == "`" and "`" in line[fence.end():]):
                marker = fence.group(1)
                fence_char, fence_size = marker[0], len(marker)
                is_code = True
                boundary = True

        if not in_html and not is_code and fence_char is None:
            indent = 0
            for char in line:
                if char == " ":
                    indent += 1
                elif char == "\t":
                    indent += 4 - (indent % 4)
                else:
                    break

            if stripped:
                while list_indents and indent < list_indents[-1]:
                    list_indents.pop()
            base_indent = list_indents[-1] if list_indents else 0
            relative_indent = indent - base_indent

            if indented_code:
                if not stripped:
                    is_code = True
                elif relative_indent >= 4:
                    is_code = True
                else:
                    indented_code = False

            if (not is_code and not indented_code and stripped and prev_blank
                    and relative_indent >= 4):
                indented_code = True
                is_code = True
                boundary = True

            html_block = None
            if not is_code and stripped and relative_indent <= 3:
                html_block = _html_block_ends(line.lstrip(" \t"))
            if html_block is not None:
                in_html = True
                boundary = True
                in_table = False
                if not html_block:
                    html_ends = html_block
                elif any(end in line.lstrip(" \t")[1:].lower() for end in html_block):
                    boundary_next = True
                else:
                    html_ends = html_block
            elif not is_code:
                list_match = _LIST_RE.match(line)
                is_list_item = bool(list_match) and relative_indent <= 3
                single_line_block = False
                if not stripped:
                    boundary = True
                    in_table = False
                elif _is_table_delimiter(line):
                    boundary = True
                    in_table = True
                    if prev_start is not None and (not boundaries or boundaries[-1] != prev_start):
                        boundaries.append(prev_start)
                elif in_table and "|" in line and not (
                        _ATX_RE.match(line) or _QUOTE_RE.match(line) or is_list_item):
                    boundary = True
                else:
                    in_table = False
                    single_line_block = bool(
                        _ATX_RE.match(line) or _THEMATIC_RE.match(line)
                        or (prev_paragraph and _SETEXT_RE.match(line)))
                    if single_line_block:
                        boundary = True
                        boundary_next = True
                    elif _QUOTE_RE.match(line):
                        boundary = True
                    else:
                        paragraph = True
                        if is_list_item:
                            boundary = True

                if is_list_item and not single_line_block:
                    leading = len(list_match.group(1))
                    marker = list_match.group(2)
                    spaces = len(list_match.group(3))
                    content_indent = leading + len(marker) + (spaces if 1 <= spaces <= 4 else 1)
                    list_indents.append(content_indent)

        code_by_start[offset] = is_code
        if in_html:
            html_starts.add(offset)
        if boundary:
            boundaries.append(offset)
        prev_blank = not stripped
        prev_paragraph = paragraph
        prev_start = offset
        offset += len(raw_line) + 1

    return code_by_start, boundaries, html_starts
