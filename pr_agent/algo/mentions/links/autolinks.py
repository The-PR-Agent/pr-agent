import re
import string
import unicodedata
from bisect import bisect_left

from ..blocks import line_flags
from .brackets import TICKS_RE, open_bracket_spans

_GITLAB_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,31}://[^\s<>`()\[\]\"'*~|\\]+")
_GITHUB_URL_RE = re.compile(r"(?<![A-Za-z])(?i:https?)://(?=\S)|(?<![^\s*_~(])www\.")
_ANGLE_RE = re.compile(r"(?<!\\)<(?i:https?|mailto):[^\x00-\x20<>\x7f]*>")
_TAIL_RE = re.compile(r"[^\s<>`()\[\]\"'*~|\\]*")
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


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    code_by_start, boundaries, html_starts = line_flags(text)
    masked = _CONTAINER_PREFIX_RE.sub(lambda prefix: " " * len(prefix.group()), text)
    ticks: dict[int, list[int]] = {}
    for run in TICKS_RE.finditer(masked):
        ticks.setdefault(len(run.group()), []).append(run.start())

    spans = []
    stops = set(boundaries)
    run_start = None
    for offset, is_code in [*code_by_start.items(), (len(text) + 1, True)]:
        raw = is_code or offset in html_starts
        if run_start is not None and (raw or offset in stops):
            spans += open_bracket_spans(masked, run_start, offset - 1, ticks)
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
