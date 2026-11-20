"""Publish-boundary firewall for model-authored markdown fields (#4999).

The input side got hostile-repo hardening; this is the output side. Model prose is
published as-is today, so text the model quoted from attacker-controlled inputs —
or composed under their influence — can render as live objects under the bot's
identity: tracking images, forged ``<!-- pr-agent:... -->`` control-plane markers,
and bidirectional overrides that make a finding render differently from how it
reads.

The firewall runs on the model-authored fields themselves, before any assembly,
so markup the bot's own templates add (tables, ``<details>`` sections) is never in
scope. Three rules:

1. HTML comments are stripped from prose, across lines: a comment that spans
   lines is as forgeable as a single-line one, and any ``<!--`` that survives
   stripping (an unclosed or rebuilt opener) is neutralized with a backslash,
   the same convention the finding-state markers use (#4778). Identity markers
   are appended by code after the firewall runs, so the control plane and model
   output never share a channel.
2. Every syntax object in prose — images, reference images, links, raw HTML
   tags, bare and ``www.`` URLs — is backslash-escaped in place, regardless of
   origin. Byte provenance cannot prove intent: the model can compose a beacon
   from scratch, so nothing that renders as an object survives on trust. The
   quote stays visible and readable, the syntax goes inert, and no backticks
   are inserted, so existing inline code spans cannot be reshuffled. Objects
   already inside an inline code span are inert and left untouched.
3. Invisible and bidirectional characters are removed from prose, including
   numeric character references that decode to them (``&#8238;``).

Everything inside a code fence stays byte-identical: fenced quoting is the
legitimate channel for hostile text, and it is already inert. Structure follows
the CommonMark rules where it changes safety: a fence closes only on a delimiter
matching the opening one's character and length with nothing after it, a backtick
fence's info string may not contain backticks (so ```` ```x``` ```` opens
nothing), and a fence line inside an open HTML block is not a fence at all —
the block's raw markup keeps getting the prose rules until a blank line ends it.

The pass is idempotent: escaped text no longer matches any rule, so a field that
reaches the firewall twice cannot be double-escaped.
"""

from __future__ import annotations

import re
from bisect import bisect_right

# Bidi embedding/override/isolate controls and direction marks, zero-width chars,
# BOM, word joiner.
_INVISIBLE_CHARACTERS = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u202a-\u202e\u2066-\u2069]"
)
_INVISIBLE_CODEPOINTS = frozenset(
    (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF,
     0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)

# Numeric character references that decode to an invisible character; GitHub
# renders &#8238; as the bidi override itself.
_CHARACTER_REFERENCE = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")

# A fence line per the CommonMark rules: up to three leading spaces, at least
# three backticks or tildes, then the info string. A backtick fence's info string
# must not contain backticks; a closing fence may not carry anything but spaces.
_FENCE_LINE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)$")
_BACKTICK_RUN = re.compile(r"`+")

# An HTML block opener that stays unescaped in the output: a tag name with no
# closing bracket anywhere on the line (complete tags are objects and get
# escaped, which dissolves their blocks). The block then runs until a blank line.
_HTML_BLOCK_OPENER = re.compile(r" {0,3}<[a-zA-Z][a-zA-Z0-9-]*(?![^>]*>)")
_BLANK_LINE = re.compile(r"[ \t]*")

_TAG_PREFIX = re.compile(r"\s*/?[a-zA-Z]")

_URL_PREFIXES = ("https://", "http://", "www.")

# Characters whose backslash escape keeps the object from rendering as a link,
# image, tag, or autolink while the text itself stays readable. The colon breaks
# bare-URL autolinking and the dot breaks the www. autolink prefix; both still
# render as the plain character.
_ESCAPE_TARGETS = str.maketrans(
    {"!": "\\!", "[": "\\[", "]": "\\]", "(": "\\(", ")": "\\)", "<": "\\<", ":": "\\:", ".": "\\."}
)


class _LineIndex:
    """Precomputed line boundaries, so per-anchor lookups stay logarithmic.

    A plain rfind per anchor is linear in the position, which goes quadratic on
    one long hostile line; the index is built once per scanned text.
    """

    def __init__(self, text: str):
        self.text = text
        self.breaks = [i for i, character in enumerate(text) if character == "\n"]

    def start(self, position: int) -> int:
        index = bisect_right(self.breaks, position - 1)
        return self.breaks[index - 1] + 1 if index else 0

    def end(self, position: int) -> int:
        index = bisect_right(self.breaks, position)
        return self.breaks[index] if index < len(self.breaks) else len(self.text)


def _active_object_spans(text: str) -> list[tuple[int, int]]:
    """Spans of the renderable objects in one linear pass per family.

    A regex alternation over ``[`` or ``<`` goes quadratic on unbalanced input (a
    50k-character diff line of stray brackets costs seconds), so each family is
    anchored on its closing shape instead: ``](`` / ``][`` walk back to the nearest
    bracket, ``<`` walks forward to ``>`` or the line end, and URL prefixes extend
    to the next whitespace. Backward walks stay between consecutive anchors, so the
    total work is linear in the text length.
    """
    spans = []
    lines = _LineIndex(text)
    # Inline and reference links/images.
    for anchor_pair, closer in (("](", ")"), ("][", "]")):
        floor = 0
        anchor = text.find(anchor_pair, 0)
        while anchor != -1:
            bracket = text.rfind("[", max(floor, lines.start(anchor)), anchor)
            escaped_bracket = bracket > 0 and text[bracket - 1] == "\\"
            if bracket != -1 and not escaped_bracket and "]" not in text[bracket + 1:anchor]:
                start = bracket - 1 if bracket > 0 and text[bracket - 1] == "!" else bracket
                close = text.find(closer, anchor + 2)
                if close != -1 and "\n" not in text[anchor + 2:close]:
                    spans.append((start, close + 1))
            floor = anchor
            anchor = text.find(anchor_pair, anchor + 2)
    # Raw HTML tags. A "<" that a previous pass already escaped is left alone, so
    # the firewall is idempotent.
    anchor = text.find("<", 0)
    while anchor != -1:
        close = text.find(">", anchor)
        already_escaped = anchor > 0 and text[anchor - 1] == "\\"
        if (
            not already_escaped
            and close != -1
            and close < lines.end(anchor)
            and _TAG_PREFIX.match(text, anchor + 1)
        ):
            spans.append((anchor, close + 1))
            anchor = text.find("<", close + 1)
        else:
            anchor = text.find("<", anchor + 1)
    # Bare and www URLs.
    for prefix in _URL_PREFIXES:
        anchor = text.find(prefix, 0)
        while anchor != -1:
            line_end = lines.end(anchor)
            if anchor == 0 or not (text[anchor - 1].isalnum() or text[anchor - 1] in "./-"):
                end = anchor + len(prefix)
                while end < line_end and not text[end].isspace():
                    end += 1
                spans.append((anchor, end))
            anchor = text.find(prefix, anchor + len(prefix))
    # Leftmost wins; at the same start, the longer object wins.
    spans.sort(key=lambda span: (span[0], -span[1]))
    kept = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            continue
        kept.append(span)
    return kept


def _strip_html_comments(segment: str) -> str:
    """Remove complete ``<!-- -->`` comments in one linear scan.

    A lazy regex over a rebuilt string would leave reassembled openers behind
    (``<!-<!---->-`` strips to a fresh ``<!--``), so this scans once and then
    neutralizes whatever openers remain, backslashing the ``<`` the same way the
    finding-state marker code does.
    """
    pieces = []
    position = 0
    while True:
        start = segment.find("<!--", position)
        if start == -1:
            pieces.append(segment[position:])
            break
        pieces.append(segment[position:start])
        end = segment.find("-->", start + 4)
        if end == -1:
            # An unclosed opener swallows nothing; keep scanning after it.
            position = start + 4
            continue
        position = end + 3
    return "".join(pieces).replace("<!--", "<\\!--")


def _strip_invisible(segment: str) -> str:
    """Drop invisible characters and the character references that decode to them."""
    segment = _INVISIBLE_CHARACTERS.sub("", segment)

    def drop_if_invisible(match):
        digits = match.group(1)
        codepoint = int(digits[1:], 16) if digits[0] in "xX" else int(digits)
        return "" if codepoint in _INVISIBLE_CODEPOINTS else match.group()

    return _CHARACTER_REFERENCE.sub(drop_if_invisible, segment)


def _opening_fence(line: str):
    """Return (character, length) when the line opens a fence, else None.

    Per CommonMark, a backtick fence's info string cannot contain backticks, so
    ```` ```x``` ```` opens nothing and the line stays prose.
    """
    match = _FENCE_LINE.match(line)
    if match is None:
        return None
    _indent, delimiter, info = match.groups()
    if delimiter[0] == "`" and "`" in info:
        return None
    return delimiter[0], len(delimiter)


def _closing_fence(line: str, opening_character: str, opening_length: int) -> bool:
    match = _FENCE_LINE.match(line)
    if match is None:
        return False
    _indent, delimiter, suffix = match.groups()
    return delimiter[0] == opening_character and len(delimiter) >= opening_length and suffix.strip(" \t") == ""


def _inline_code_intervals(line: str) -> list[tuple[int, int]]:
    """Intervals of the line covered by an inline code span.

    Follows the CommonMark delimiter rule closely enough for the decision at
    hand: a backtick run of length N is closed by the next unused run of exactly
    N. One pass, pairing through a per-length queue, stays linear in the number
    of runs.
    """
    intervals = []
    open_runs = {}
    for match in _BACKTICK_RUN.finditer(line):
        length = len(match.group())
        waiting = open_runs.get(length)
        if waiting:
            intervals.append((waiting.pop(), match.end()))
        else:
            open_runs[length] = [match.start()]
    return intervals


def _escape_active_objects(line: str) -> str:
    """Escape every syntax object in the line, in place."""
    inert_intervals = _inline_code_intervals(line)
    spans = [
        (start, end)
        for start, end in _active_object_spans(line)
        # Objects already inside an inline code span render literally; leave them be.
        if not any(begin <= start and end <= finish for begin, finish in inert_intervals)
    ]
    pieces = []
    position = 0
    for start, end in spans:
        pieces.append(line[position:start])
        pieces.append(line[start:end].translate(_ESCAPE_TARGETS))
        position = end
    pieces.append(line[position:])
    return "".join(pieces)


def firewall_model_output(markdown: str) -> str:
    """Apply the three publish rules to a model-authored markdown field."""
    if not isinstance(markdown, str) or not markdown:
        return markdown
    lines = markdown.split("\n")
    result = []
    open_fence = None  # (character, length) of the fence currently protecting a block
    html_block = False  # inside an unescaped HTML block: no fences, prose rules stay on
    prose_buffer = []
    for line in lines:
        if open_fence is not None:
            if _closing_fence(line, open_fence[0], open_fence[1]):
                open_fence = None
            result.append(line)
            continue
        if html_block:
            if _BLANK_LINE.fullmatch(line) is not None:
                html_block = False
            prose_buffer.append(line)
            continue
        fence = _opening_fence(line)
        if fence is not None:
            _flush_prose(prose_buffer, result)
            open_fence = fence
            result.append(line)
            continue
        if _HTML_BLOCK_OPENER.match(line):
            html_block = True
        prose_buffer.append(line)
    _flush_prose(prose_buffer, result)
    return "\n".join(result)


def _flush_prose(buffer: list[str], result: list[str]) -> None:
    """Apply the prose rules to a run of unfenced lines as one segment.

    One segment at a time, so multiline HTML comments cannot straddle the buffer,
    and so line structure is preserved for everything else. Invisible characters
    drop first, so a zero-width space hiding inside an opener cannot smuggle a
    comment past the scan. The buffer is consumed: a later flush must not re-emit
    an already-flushed segment.
    """
    if not buffer:
        return
    segment = "\n".join(buffer)
    buffer.clear()
    segment = _strip_invisible(segment)
    segment = _strip_html_comments(segment)
    segment = "\n".join(_escape_active_objects(line) for line in segment.split("\n"))
    result.extend(segment.split("\n"))
