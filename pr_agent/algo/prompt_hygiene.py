import re
from dataclasses import dataclass

from pr_agent.log import get_logger

_COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_DETAILS_TAG_RE = re.compile(r"<(/?)details\b([^>]*)>", re.IGNORECASE)
_DETAILS_OPEN_RE = re.compile(r"(?<![\w-])open(?![\w-])", re.IGNORECASE)
_INVISIBLE_RE = re.compile(
    "[\u00ad\u061c\u200b\u200c\u200e\u200f\u2060\ufeff\u202a-\u202e"
    "\u2066-\u206f\U000e0000-\U000e007f\ufe00-\ufe0f]"
)
_ZWJ = "\u200d"
_VS16 = "\ufe0f"
_EMOJI_BASE = r"[\U0001f000-\U0001faff\u2600-\u27bf\u00a9\u00ae\u2122]"
_EMOJI_SEQUENCE_RE = re.compile(
    rf"(?:{_EMOJI_BASE}{_VS16}?(?:{_ZWJ}{_EMOJI_BASE}{_VS16}?)*|[0-9#*]{_VS16}?\u20e3)"
)
_EMOJI_PLACEHOLDER_RE = re.compile(r"\x02(\d+)\x03")


@dataclass(frozen=True)
class HiddenContentCounts:
    comments: int = 0
    details: int = 0
    invisible: int = 0

    def any(self) -> bool:
        return bool(self.comments or self.details or self.invisible)


def _strip_collapsed_details(text: str) -> tuple[str, int]:
    spans = []
    stack = []
    for match in _DETAILS_TAG_RE.finditer(text):
        if match.group(1):
            if not stack:
                continue
            start, collapsed, parent_collapsed = stack.pop()
            if collapsed and not parent_collapsed:
                spans.append((start, match.end()))
            continue
        collapsed = _DETAILS_OPEN_RE.search(match.group(2)) is None
        parent_collapsed = any(frame[1] for frame in stack)
        stack.append((match.start(), collapsed, parent_collapsed))
    for start, collapsed, parent_collapsed in stack:
        if collapsed and not parent_collapsed:
            spans.append((start, len(text)))
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return text, len(spans)


def _strip_invisible(text: str) -> tuple[str, int]:
    emojis = []

    def hold(match: re.Match) -> str:
        emojis.append(match.group(0))
        return f"\x02{len(emojis) - 1}\x03"

    held = _EMOJI_SEQUENCE_RE.sub(hold, text)
    removed = 0

    def drop(match: re.Match) -> str:
        nonlocal removed
        removed += 1
        return ""

    held = _INVISIBLE_RE.sub(drop, held)
    stray_zwj = held.count(_ZWJ)
    held = held.replace(_ZWJ, "")
    removed += stray_zwj
    return _EMOJI_PLACEHOLDER_RE.sub(lambda match: emojis[int(match.group(1))], held), removed


def strip_hidden_prompt_content(text: str) -> tuple[str, HiddenContentCounts]:
    if not isinstance(text, str):
        return text, HiddenContentCounts()
    comments = 0

    def drop_comment(match: re.Match) -> str:
        nonlocal comments
        comments += 1
        return ""

    text = _COMMENT_RE.sub(drop_comment, text)
    text, details = _strip_collapsed_details(text)
    text, invisible = _strip_invisible(text)
    return text, HiddenContentCounts(comments, details, invisible)


def sanitize_prompt_text(text: str) -> str:
    try:
        cleaned, counts = strip_hidden_prompt_content(text)
    except Exception as error:
        get_logger().warning("Kept prompt text after sanitizer failure", artifact={"error": str(error)})
        return text
    if counts.any():
        get_logger().info(
            "Stripped hidden content from prompt text",
            artifact={"comments": counts.comments, "details": counts.details, "invisible": counts.invisible},
        )
    return cleaned
