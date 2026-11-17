import re
from bisect import bisect_left, bisect_right

from .blocks import line_flags

# Mentions follow an ASCII boundary (Ruby \w) and entity decoding; leading underscores can open emphasis
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])_*(?:@|&#0{0,5}64;|&#[xX]0{0,4}40;|&commat;)(?=[\w-])")
_ZERO_WIDTH_SPACE = "\u200b"
# Autolinked URLs are never scanned for mentions; the span stops at Markdown delimiters
_AUTOLINK_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,31}://[^\s<>`()\[\]\"'*~|\\]+")
_TICK_RUN_RE = re.compile(r"`+")


def _autolink_spans(text: str) -> tuple[list[int], list[int]]:
    spans = [(match.start(), match.end()) for match in _AUTOLINK_RE.finditer(text)]
    return [start for start, _ in spans], [end for _, end in spans]


def _in_autolink(position: int, url_spans: tuple[list[int], list[int]]) -> bool:
    starts, ends = url_spans
    span_index = bisect_right(starts, position) - 1
    return span_index >= 0 and position < ends[span_index]


def neutralize_mentions(text: str) -> str:
    if not text:
        return text

    tick_runs: dict[int, list[int]] = {}
    for run in _TICK_RUN_RE.finditer(text):
        tick_runs.setdefault(len(run.group()), []).append(run.start())

    code_by_start, boundaries, html_starts = line_flags(text)
    url_spans = _autolink_spans(text)

    output = []
    index = 0
    inline_ticks = 0
    line_start = True
    line_offset = 0

    while index < len(text):
        if text[index] == "\n":
            output.append("\n")
            index += 1
            line_start = True
            line_offset = index
            continue

        if line_start and not inline_ticks and code_by_start.get(index):
            line_end = text.find("\n", index)
            if line_end < 0:
                line_end = len(text)
            output.append(text[index:line_end])
            index = line_end
            line_start = False
            continue

        if text[index] == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            ticks = end - index
            if not inline_ticks:
                backslashes = 0
                while index - backslashes > 0 and text[index - backslashes - 1] == "\\":
                    backslashes += 1
                if backslashes % 2:
                    output.append("`")
                    index += 1
                    line_start = False
                    continue

                runs = tick_runs.get(ticks)
                # Backticks inside a raw HTML block are literal text, not code spans
                if runs and line_offset not in html_starts:
                    closer_index = bisect_left(runs, end)
                    boundary_index = bisect_left(boundaries, end)
                    boundary = boundaries[boundary_index] if boundary_index < len(boundaries) else len(text)
                    if closer_index < len(runs) and runs[closer_index] < boundary:
                        inline_ticks = ticks
            elif ticks == inline_ticks:
                inline_ticks = 0
            output.append(text[index:end])
            index = end
            line_start = False
            continue

        if inline_ticks:
            char = text[index]
            output.append(char)
            index += 1
            line_start = char == "\n"
            if line_start:
                line_offset = index
            continue

        next_special = index + 1
        while next_special < len(text) and text[next_special] not in "@`\n":
            next_special += 1
        search_start = index
        while search_start and text[search_start - 1] == "_":
            search_start -= 1
        copied = index
        for mention in _MENTION_RE.finditer(text, search_start, next_special):
            if _in_autolink(mention.start(), url_spans):
                continue
            output.append(text[copied:mention.end()])
            output.append(_ZERO_WIDTH_SPACE)
            copied = mention.end()
        output.append(text[copied:next_special])
        index = next_special
        if index < len(text) and text[index] == "\n":
            output.append("\n")
            index += 1
            line_start = True
            line_offset = index
        else:
            line_start = False

    return "".join(output)
