import re
import string
import unicodedata
from bisect import bisect_left, bisect_right

from ..blocks import line_flags
from .brackets import TICKS_RE, open_bracket_spans

_GITLAB_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,31}://[^\s<>`()\[\]\"'*~|\\]+")
_GITHUB_URL_RE = re.compile(r"(?<![A-Za-z])(?i:https?)://(?=\S)|(?<![^\s*_~(])www\.")
_ANGLE_RE = re.compile(r"(?<!\\)<(?i:https?|mailto):[^\x00-\x20<>\x7f]*>")
_TAIL_RE = re.compile(r"[^\s<>`()\[\]\"'*~|\\]*")
_EXTENT_RE = re.compile(r"[^ \t\n\r<]*")
_HTML_TAG_RE = re.compile(r"<[A-Za-z/!?](?:=[ \t\n\f\r]*(?:\"[^\"]*\"|'[^']*')|[^>])*+>?")
_CONTAINER_PREFIX_RE = re.compile(r"^[ \t>]+", re.MULTILINE)


def _is_host_char(char: str) -> bool:
    return not (char.isspace() or char in string.punctuation or unicodedata.category(char).startswith("P"))


def _domain_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and (text[end] in "._-" or _is_host_char(text[end])):
        end += 1
    return end


def _is_valid_domain(domain: str) -> bool:
    return _is_host_char(domain[:1] or " ") and "_" not in "".join(domain.split(".")[-2:])


def _url_ends(text: str) -> dict[int, int]:
    ends = {}
    extent_end = 0
    for match in _GITHUB_URL_RE.finditer(text):
        domain = match.start() if match.group().startswith("www") else match.end()
        if _is_valid_domain(text[domain:_domain_end(text, domain)]):
            if match.end() >= extent_end:
                extent_end = _EXTENT_RE.match(text, match.end()).end()
            ends[match.start()] = extent_end
    return ends


def _html_text_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans = []
    for tag in _HTML_TAG_RE.finditer(text, start, end):
        spans.append((start, tag.start()))
        start = tag.end()
    return [*spans, (start, end)]


def _unlinked_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    code_by_start, boundaries, html_starts = line_flags(text)
    masked = _CONTAINER_PREFIX_RE.sub(lambda prefix: " " * len(prefix.group()), text)
    ticks: dict[int, list[int]] = {}
    for run in TICKS_RE.finditer(masked):
        ticks.setdefault(len(run.group()), []).append(run.start())
    urls = _url_ends(masked)

    spans = []
    extents = []
    stops = set(boundaries)
    run_start = html_start = None
    for offset, is_code in [*code_by_start.items(), (len(text) + 1, True)]:
        html = offset in html_starts
        if run_start is not None and (is_code or html or offset in stops):
            cells = boundaries[bisect_right(boundaries, run_start):bisect_left(boundaries, offset - 1)]
            cell_start = run_start
            for cell_end in [*cells, offset - 1]:
                found, linked = open_bracket_spans(masked, cell_start, cell_end, ticks, urls)
                spans += found
                extents += linked
                cell_start = cell_end
            run_start = None
        if html_start is not None and not html:
            spans += _html_text_spans(text, html_start, offset - 1)
            html_start = None
        if run_start is None and not (is_code or html):
            run_start = offset
        if html_start is None and html:
            html_start = offset
    return spans, extents


def gitlab_autolinks(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    return [match.span() for match in _GITLAB_URL_RE.finditer(text)], []


def github_autolinks(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    spans = [match.span() for match in _ANGLE_RE.finditer(text)]
    unlinked, extents = _unlinked_spans(text)
    unlinked_starts = [start for start, _ in unlinked]
    covered = 0
    for match in _GITHUB_URL_RE.finditer(text):
        if match.start() < covered:
            continue
        inside = bisect_left(unlinked_starts, match.start() + 1) - 1
        if inside >= 0 and match.start() < unlinked[inside][1]:
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
    return merged, extents
