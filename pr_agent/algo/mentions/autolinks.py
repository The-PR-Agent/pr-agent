import re
import string
import unicodedata

_GITLAB_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,31}://[^\s<>`()\[\]\"'*~|\\]+")
_GITHUB_URL_RE = re.compile(r"(?<![A-Za-z])(?i:https?)://(?=\S)|(?<![^\s*_~(])www\.")
_ANGLE_RE = re.compile(r"<(?i:https?|mailto):[^\x00-\x20<>\x7f]*>")
_TAIL_RE = re.compile(r"[^\s<>`()\[\]\"'*~|\\]*")


def _is_host_char(char: str) -> bool:
    return not (char.isspace() or char in string.punctuation or unicodedata.category(char).startswith("P"))


def _domain_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and (text[end] in "._-" or _is_host_char(text[end])):
        end += 1
    return end


def _is_valid_domain(domain: str) -> bool:
    return _is_host_char(domain[:1] or " ") and "_" not in "".join(domain.split(".")[-2:])


def gitlab_autolinks(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _GITLAB_URL_RE.finditer(text)]


def github_autolinks(text: str) -> list[tuple[int, int]]:
    spans = [match.span() for match in _ANGLE_RE.finditer(text)]
    covered = 0
    for match in _GITHUB_URL_RE.finditer(text):
        if match.start() < covered:
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
