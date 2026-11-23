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
2. Every syntax object in prose — images, links, reference definitions, raw
   HTML tags, and URLs of every spelling — is backslash-escaped in place,
   regardless of origin. Byte provenance cannot prove intent: the model can
   compose a beacon from scratch, so nothing that renders as an object survives
   on trust. The quote stays visible and readable, the syntax goes inert, and
   no backticks are inserted, so existing inline code spans cannot be
   reshuffled. Objects already inside an inline code span are inert and left
   untouched.
3. Invisible and bidirectional characters are removed from prose, including
   numeric character references that decode to them (``&#8238;``).

Everything inside a code fence stays byte-identical: fenced quoting is the
legitimate channel for hostile text, and it is already inert. Which lines are
fenced follows CommonMark, because every place the firewall's model of markdown
disagrees with the renderer's is a bypass:

- a fence closes only on a delimiter matching the opening one's character and
  length with nothing after it, and a backtick fence's info string may not
  contain backticks;
- a fence line inside an HTML block is not a fence. The blank-line-terminated
  kinds end at a blank line as written: a line holding only an invisible
  character is not blank to the renderer, even though the prose rules empty it.
  Processing instructions, declarations, CDATA sections, and ``<pre>``,
  ``<script>``, ``<style>``, ``<textarea>`` blocks are read to the end of the
  field. CommonMark ends those on ``?>``, ``>``, ``]]>`` or a closing tag; this
  takes the wider reading on purpose, since the prose rules are safe to apply
  to raw markup and a fence wrongly honoured inside one is a bypass;
- a backslash-escaped backtick does not open or close a code span, and a
  backslash is only an escape when the run before the character is odd
  (``\\\\<img>`` leaves the tag live, ``\\<img>`` does not).

Escaped text no longer matches any rule, so a second pass leaves it alone. That
holds for the whole field only where the pass changed no block structure: it
escapes an unescaped HTML opener, which can turn a later line from "inside an
HTML block" into a real fence. Each field is firewalled once per run.
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
_BACKSLASH_RUN = re.compile(r"\\+")

# HTML block starts that run to the end of the field: a processing instruction, a
# declaration, or a CDATA section (CommonMark HTML block types 3, 4 and 5). An
# HTML comment (type 2) is deliberately not here: the firewall strips comments
# itself, and a comment opener that opened a to-the-end block would keep every
# later fence closed.
_HTML_BLOCK_TO_END = re.compile(r" {0,3}(<\?|<!\[CDATA\[|<![A-Za-z])", re.IGNORECASE)
_RAW_TAG_BLOCK = re.compile(r" {0,3}<(?:script|pre|style|textarea)(?:[ \t>]|$)")
# A link reference definition line: "[label]: destination", with an optional title.
# Destination and title exclude their own closing character, so no branch can scan
# past it and backtrack.
_REFERENCE_DEFINITION = re.compile(
    r" {0,3}\[[^\]\n]+\]:[ \t]*(?:<[^>\n]*>|[^\s\n]+)"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?[ \t]*$"
)
# A reference-style image or link with no destination in brackets: it takes its
# URL from a definition, so it is an object whether or not the definition is here.
_COLLAPSED_REFERENCE = re.compile(r"!?\[[^\]\n]+\](?![\[(])")
_HTML_BLOCK_OPENER = re.compile(r" {0,3}<[a-zA-Z][a-zA-Z0-9-]*(?![^>]*>)")

_TAG_PREFIX = re.compile(r"\s*/?[a-zA-Z]")

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


def _positions(text: str, needle: str) -> list[int]:
    """All occurrences of ``needle``, in one linear scan (for bisect lookups)."""
    found = []
    position = text.find(needle)
    while position != -1:
        found.append(position)
        position = text.find(needle, position + len(needle))
    return found


def _first_at_or_after(positions: list[int], bound: int):
    """The first position >= bound, or None. ``positions`` must be sorted."""
    index = bisect_right(positions, bound - 1)
    return positions[index] if index < len(positions) else None


def _escaped_positions(text: str) -> frozenset[int]:
    """Positions of characters a backslash escapes, in one linear pass.

    A backslash escapes the next character only when the run of backslashes
    before it is odd; an even run is itself escaped and leaves the character
    live. Computing the set once keeps every escape check O(1), which is what
    keeps the object scan linear on a flood of backslashes.
    """
    escaped = set()
    for run in _BACKSLASH_RUN.finditer(text):
        if len(run.group()) % 2 == 1:
            end = run.end()
            if end < len(text):
                escaped.add(end)
    return frozenset(escaped)


def _active_object_spans(text: str) -> list[tuple[int, int]]:
    """Spans of the renderable objects, linear per family on hostile input.

    A regex alternation over ``[`` or ``<`` goes quadratic on unbalanced input (a
    50k-character diff line of stray brackets costs seconds), so each family is
    anchored on its closing shape instead. Lookups go through precomputed
    position lists and one escape set, so no step rescans the text.
    """
    spans = []
    lines = _LineIndex(text)
    escaped = _escaped_positions(text)
    # Inline and reference links/images.
    for anchor_pair, closer in (("](", ")"), ("][", "]")):
        closer_positions = _positions(text, closer)
        floor = 0
        anchor = text.find(anchor_pair, 0)
        while anchor != -1:
            bracket = text.rfind("[", max(floor, lines.start(anchor)), anchor)
            if (
                bracket != -1
                and bracket not in escaped
                and text.find("]", bracket + 1, anchor) == -1
            ):
                bang = bracket > 0 and text[bracket - 1] == "!" and bracket - 1 not in escaped
                start = bracket - 1 if bang else bracket
                close = _first_at_or_after(closer_positions, anchor + 2)
                if close is not None and close < lines.end(anchor):
                    spans.append((start, close + 1))
            floor = anchor
            anchor = text.find(anchor_pair, anchor + 2)
    # Raw HTML tags, closed or not: an open "<img" followed by "src=" on the next
    # line still renders, so the escape happens at the "<" whatever follows.
    anchor = text.find("<", 0)
    while anchor != -1:
        if anchor not in escaped and _TAG_PREFIX.match(text, anchor + 1):
            close = text.find(">", anchor)
            end = close + 1 if close != -1 and close < lines.end(anchor) else anchor + 1
            spans.append((anchor, end))
            anchor = end if end > anchor + 1 else text.find("<", anchor + 1)
        else:
            anchor = text.find("<", anchor + 1)
    # URL spellings: bare, www, and protocol-relative ("//host"). The character
    # before must not continue a word, so "https://" does not also match at its
    # "//" and "a//b" is left alone.
    for prefix in ("https://", "http://", "www.", "//"):
        anchor = text.find(prefix, 0)
        while anchor != -1:
            line_end = lines.end(anchor)
            before = text[anchor - 1] if anchor > 0 else ""
            starts_token = not before or not (before.isalnum() or before in "_-/:")
            if starts_token:
                end = anchor + len(prefix)
                while end < line_end and not text[end].isspace() and text[end] not in ">)]\"'":
                    end += 1
                # The "//" itself is not escaped, so a URL the pass already handled
                # still matches here; the backslashes it gained mark it as done.
                if "\\" not in text[anchor:end]:
                    spans.append((anchor, end))
            anchor = text.find(prefix, anchor + len(prefix))
    # Raw-markup openers ("<?php", "<!DOCTYPE", "<![CDATA[") that are not tags.
    spans.extend(
        (match.start(1), match.end(1))
        for match in _HTML_BLOCK_TO_END.finditer(text)
        if match.start(1) not in escaped
    )
    # Link reference definitions, one match per line.
    spans.extend(
        (match.start(), match.end())
        for match in _REFERENCE_DEFINITION.finditer(text)
        if match.start() not in escaped
    )
    # Reference-style images and links without an inline destination.
    spans.extend(
        (match.start(), match.end())
        for match in _COLLAPSED_REFERENCE.finditer(text)
        if match.start() not in escaped
    )
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
    finding-state marker code does. Closer positions are precomputed once, so a
    flood of openers with one distant closer costs bisects, not rescans.
    """
    closer_positions = _positions(segment, "-->")
    pieces = []
    position = 0
    while True:
        start = segment.find("<!--", position)
        if start == -1:
            pieces.append(segment[position:])
            break
        pieces.append(segment[position:start])
        end = _first_at_or_after(closer_positions, start + 4)
        if end is None:
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

    A backtick run of length N is closed by the next unused run of exactly N,
    and a run whose first backtick is backslash-escaped is no delimiter at all:
    `` \\`![x](url)` `` opens nothing, so the image in it stays an object. One
    pass through the runs keeps it linear in their number.
    """
    intervals = []
    open_runs = {}
    escaped = _escaped_positions(line)
    for match in _BACKTICK_RUN.finditer(line):
        if match.start() in escaped:
            continue
        length = len(match.group())
        waiting = open_runs.get(length)
        if waiting:
            intervals.append((waiting.pop(), match.end()))
        else:
            open_runs[length] = [match.start()]
    return intervals


def _escape_active_objects(line: str) -> str:
    """Escape every syntax object in the line, in place.

    Span-versus-code-span containment walks both sorted lists with a running
    prefix maximum, so a line carrying thousands of objects and thousands of
    code spans stays linear instead of crossing every pair.
    """
    intervals = sorted(_inline_code_intervals(line))
    pieces = []
    position = 0
    interval_index = 0
    covered_until = -1  # farthest end among the code spans that started so far
    for start, end in _active_object_spans(line):
        while interval_index < len(intervals) and intervals[interval_index][0] <= start:
            begin, finish = intervals[interval_index]
            covered_until = max(covered_until, finish)
            interval_index += 1
        # Objects already inside an inline code span render literally; leave them be.
        if covered_until >= end:
            continue
        pieces.append(line[position:start])
        pieces.append(line[start:end].translate(_ESCAPE_TARGETS))
        position = end
    pieces.append(line[position:])
    return "".join(pieces)


def _ends_html_block(line: str) -> bool:
    """Whether the line terminates a blank-line-terminated HTML block.

    Judged on the line as written, because that is what the renderer sees. A line
    holding only an invisible character or an entity such as ``&#x200F;`` is not
    blank to the renderer even though the prose strips empty it, so it must not
    end the block here either.
    """
    return line.strip(" \t") == ""


def firewall_model_output(markdown: str, *, fenced_ok: bool = True) -> str:
    """Apply the three publish rules to a model-authored markdown field.

    ``fenced_ok=False`` treats the whole field as prose: use it for fields the
    templates render inside their own HTML blocks (``<ul>``, ``<details>``),
    where a fence line in the field is not a fence on GitHub and everything in
    it renders as raw markup.
    """
    if not isinstance(markdown, str) or not markdown:
        return markdown
    lines = markdown.split("\n")
    result = []
    open_fence = None  # (character, length) of the fence currently protecting a block
    html_block = False  # inside a blank-line-terminated HTML block: prose rules stay on
    raw_block = False  # inside a script/pre/raw block: the rest of the field is raw markup
    embedded = not fenced_ok  # a template-embedded field is prose throughout, blank lines included
    prose_buffer = []
    for line in lines:
        if embedded:
            prose_buffer.append(line)
            continue
        if open_fence is not None:
            if _closing_fence(line, open_fence[0], open_fence[1]):
                open_fence = None
            result.append(line)
            continue
        if raw_block:
            prose_buffer.append(line)
            continue
        if html_block:
            if _ends_html_block(line):
                html_block = False
            prose_buffer.append(line)
            continue
        fence = _opening_fence(line)
        if fence is not None:
            _flush_prose(prose_buffer, result)
            open_fence = fence
            result.append(line)
            continue
        if _HTML_BLOCK_TO_END.match(line) or _RAW_TAG_BLOCK.match(line):
            # These blocks do not end at a blank line, so no fence inside them
            # is a fence; the prose rules keep running to the end of the field.
            raw_block = True
        elif _HTML_BLOCK_OPENER.match(line):
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
