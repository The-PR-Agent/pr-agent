"""Publish-boundary firewall for model-authored fields (#4999).

The input side got hostile-repo hardening; this is the output side. Model text is
published as is, so text the model quoted from attacker-controlled inputs — or
composed under their influence — can render as live objects under the bot's
identity: tracking images, forged ``<!-- pr-agent:... -->`` control-plane markers,
and bidirectional overrides that make a finding render differently from how it
reads.

The firewall runs on each model-authored field, before any assembly, so markup the
bot's own templates add (tables, ``<details>`` sections) is never in scope. Three
rules:

1. HTML comments are stripped from prose, across lines: a comment that spans
   lines is as forgeable as a single-line one, and any ``<!--`` that survives
   stripping (an unclosed or rebuilt opener) is neutralized with a backslash,
   the same convention the finding-state markers use (#4778). Identity markers
   are appended by code after the firewall runs, so the control plane and model
   output never share a channel. Code is published as it stands, and the control
   plane reads the comment body as text, code blocks included, so there the
   opener of a PR-Agent marker is neutralized and nothing else is touched.
2. Every syntax object is escaped in place, regardless of origin:

   - every ``[`` and ``]`` outside a code span. Nothing is paired. A link, an
     image, a reference definition and a footnote all need a live bracket, so a
     line break inside the brackets, nesting, and ``[x]: url`` definitions need
     no rules of their own;
   - every ``<`` that can open raw HTML or an autolink (``<tag``, ``</tag``,
     ``<!``, ``<?``), at the ``<`` itself, so a tag whose attributes continue on
     the next line is covered as well;
   - every bare ``www.``, ``http://``, ``https://`` and ``ftp://``, by escaping
     its ``:`` and ``.``, which breaks the autolink prefix and still renders as
     the plain characters.

   Byte provenance cannot prove intent: the model can compose a beacon from
   scratch, so nothing that renders as an object survives on trust. The quote
   stays visible and readable, and no backticks are inserted or removed, so
   existing code spans are not reshuffled. Objects inside an inline code span are
   inert and left untouched.
3. Invisible and bidirectional characters are removed, including character
   references that decode to them (``&#8238;``, ``&lrm;``), and so are the two
   control characters that are whitespace to some of the renderer's rules and
   not to others. A carriage return is read as the line end the renderer takes
   it for.

Which rules can matter depends on where the template renders the field, so the
caller names that place (``context``) and what precedes the field's first line
(``after``):

- ``context="markdown"``: the field is parsed as markdown. Code blocks are
  published as they stand: quoting hostile text in a block of code is the
  legitimate channel for it, and it is inert already.
- ``context="html_block"``: the first line of the field shares a line with a
  template tag that opens an HTML block (``<li>``, ``<tr><td>``). Up to the
  field's first blank line the text is raw HTML there: markdown is not parsed,
  and a backslash is not an escape, so ``\\<img>`` is still a live tag. ``<`` is
  entity-encoded instead, and nothing else needs a rule. After the blank line
  the ``markdown`` rules apply.
- ``context="html_inline"``: the first line shares a line with a template tag
  that opens an HTML block in one branch of the template and not in another (the
  key-issue header). Both readings are covered: ``<`` is entity-encoded wherever
  it stands, including inside backticks, and the ``markdown`` rules apply outside
  code spans. What follows the first blank line is the same under both readings
  only while the lines before it are plain paragraph text. Anything else there,
  a list item, a quote or a fence, may stay open across that line under one
  reading, so the whole field then gets the prose rules and no code span.
- ``context="escaped_block"``: the template HTML-escapes the field itself up to
  its first blank line, and parses what follows as markdown.
- ``context="escaped"``: the template HTML-escapes the field itself; only rule 3
  applies.
- ``after="blank"``: a blank line precedes the field, so its first line begins a
  block and may open a fence.
- ``after="markup"``: template markup, and no text of another field, precedes
  the field on the same line. Its first line is paragraph text whatever it looks
  like, and an empty first line is only a line break.
- ``after="item"``: a list marker precedes the field on the same line, so its
  first line is the first line of a list item.
- ``after="maybe"``: the template puts a blank line before the field in one of
  its branches and markup on the same line in another. Nothing is guessed: a
  first line that would read differently under the two leaves the whole field to
  the prose rules, fences included, and its first paragraph without a code span.
- ``continued=True``: another model field precedes this one on the same line.
  Nothing in the first paragraph is taken for a code span, since the other
  field's backticks may still be open.

Block structure, where code is and where a paragraph ends, is read off the
published text, after rules 1 and 3, since a line they empty ends a paragraph or
a list item, and with it a code block inside that item. It is read as the
renderer reads it, because each place the two disagree leaves either a live
object or a stray backslash:

- A code span lies inside one paragraph, and the paragraph is cut where the
  renderer cuts it: at a blank line, and at a line that begins another block,
  such as a quote, a heading, a fence or a list item that may interrupt it.
  Block quotes and list items are followed through their markers. A quoted
  paragraph runs on from one ``>`` line to the next; a blank line closes a
  paragraph, a quote and an item that is still empty, and nothing else; and a
  paragraph line outside every open container goes on with that paragraph and
  closes nothing.
- A fence, and an indented code block, lasts as long as the renderer keeps it:
  to its closing fence, or to the end of the container it sits in. A blank line
  inside a list item does not end it, and a closing fence is measured in
  columns, tabs included. Each line is judged as the renderer gets it: a line
  outside the container as it stands is published as prose, rules 1 and 3
  first, and what they take out may move it inside the container, where the
  renderer keeps the block open.
- Wherever the structure cannot be followed, nothing is taken for code from
  there on: at a table, whose rows are cut at their pipes before any inline
  parsing; at a tab in a line's indentation or in the markers of a quote or a
  list item, whose width depends on the column it stands in. Such a field is
  picked up again where the renderer can have nothing open, at a line that
  follows a blank line and starts at column 0, unless a fence has been opened
  since.

The pairing itself is cmark's procedure rather than the specification's, since
GitHub renders with cmark and the two part ways: a backtick a backslash escapes
cannot open a span but does close one, and once a backtick string has found no
closer, the next span of that length is taken to have none (`` `` `a` `b` ``
makes ``a`` code and leaves `` `b` `` as text). A backslash counts by parity: an
odd run escapes the next character, an even one is itself escaped
(``\\\\\\\\<img>`` leaves the tag live, ``\\\\<img>`` does not).

No HTML block can begin in the published text: every ``<`` that opens markup
where a block begins, indentation and quote or list markers aside, is escaped
even where a code span seems to cover it.

Known limits, stated rather than hidden: GitHub turns a bare email address into
a ``mailto:`` link after escapes are resolved, so none of these rules reach it.
Inline code inside a table, or in a field whose structure could not be followed,
shows the escapes. Invisible characters inside a code block are kept, the block
being published as it stands.
"""

from __future__ import annotations

import re
from bisect import bisect_right

CONTEXTS = frozenset({"markdown", "html_block", "html_inline", "escaped_block", "escaped"})
# What may precede a field's first line on its own line.
AFTER = frozenset({"blank", "markup", "item", "maybe"})

# Bidi embedding/override/isolate controls and direction marks, zero-width chars,
# BOM, word joiner; and the two control characters that are whitespace to some
# of the renderer's rules and not to others (a line feed is one after a list
# marker, and plain text to the scanners), so that a rule cannot be picked by
# putting one of them in.
_INVISIBLE_CODEPOINTS = frozenset(
    (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF,
     0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)
_INVISIBLE_CHARACTERS = re.compile("[\v\f\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u202a-\u202e\u2066-\u2069]")
# The same characters written as character references. GitHub renders &#8238; as
# the bidi override itself, and a browser decodes the numeric forms without the
# closing semicolon too, so that semicolon is optional here. The names are the
# ones HTML defines for this set.
_CHARACTER_REFERENCE = re.compile(
    r"&#([xX][0-9a-fA-F]+|[0-9]+);?"
    r"|&(?:NegativeMediumSpace|NegativeThickSpace|NegativeThinSpace|NegativeVeryThinSpace"
    r"|NoBreak|ZeroWidthSpace|lrm|rlm|zwj|zwnj);"
)

# Block starts, each tested on a line with its container prefixes removed.
_ATX_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
_SETEXT_UNDERLINE = re.compile(r"(?:=+|-+)[ \t]*$")
_THEMATIC_BREAK = re.compile(r"([-_*])(?:[ \t]*\1){2,}[ \t]*$")
# A list item that ends the paragraph above it: a bullet, or an ordered item
# numbered 1, then whitespace and content.
_ITEM_ENDS_PARAGRAPH = re.compile(r"(?:[-+*]|0{0,8}1[.)])[ \t]+\S")
# The marker itself, and the whitespace that follows it; what comes next is read
# by ``_item_start``.
_ITEM_MARKER = re.compile(r"(?P<marker>[-+*]|[0-9]{1,9}[.)])")
_ITEM_SPACE = re.compile(r"[ \t]*")
_QUOTE_MARKER = re.compile(r" {0,3}>[ \t]?")
_QUOTE_MARKERS = re.compile(r" {0,3}(?:>[ \t]?)+")

# A fence that opens, per the CommonMark rules: at least three backticks or
# tildes at the place the block begins, then the info string. A backtick fence's
# info string must not contain backticks.
_FENCE_DELIMITER = re.compile(r"`{3,}|~{3,}")
_BACKTICK_RUN = re.compile(r"`+")
_BACKSLASH_RUN = re.compile(r"\\+")
# A backtick string longer than this is never a code span delimiter.
_MAX_BACKTICKS = 80

# What may precede a table row on its line: indentation and block quote markers.
_TABLE_PREFIX = re.compile(r"[\s>]*")
_DELIMITER_ROW_CHARACTERS = frozenset("-:| \t\v\f")
# The opener of a PR-Agent control marker, where code is published as it stands.
_CONTROL_MARKER_OPENER = re.compile(r"<!--(?=\s*pr-agent)")

# Autolink prefixes, and for each whether a "-./:" right before it keeps the
# prefix out of a link (true for www. only).
_URL_PREFIXES = (("http://", False), ("https://", False), ("ftp://", False), ("www.", True))
_WORD_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


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


def _strip_html_comments(segment: str, *, neutralize: bool = True) -> str:
    """Remove complete ``<!-- -->`` comments in one linear scan.

    A lazy regex over a rebuilt string would leave reassembled openers behind
    (``<!-<!---->-`` strips to a fresh ``<!--``), so this scans once and then
    handles whatever openers remain. Closer positions are precomputed once, so a
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
    joined = "".join(pieces)
    return joined.replace("<!--", "<\\!--") if neutralize else joined


def _strip_invisible(segment: str) -> str:
    """Drop invisible characters and the character references that decode to them."""
    segment = _INVISIBLE_CHARACTERS.sub("", segment)

    def drop_if_invisible(match):
        digits = match.group(1)
        if digits is None:
            return ""
        hexadecimal = digits[0] in "xX"
        # Leading zeros do not change the value: &#0008238; is &#8238;.
        value = (digits[1:] if hexadecimal else digits).lstrip("0") or "0"
        if len(value) > (4 if hexadecimal else 5):
            # Larger than any of these characters, 0xFEFF at most. ``int`` refuses longer decimal strings anyway.
            return match.group()
        codepoint = int(value, 16) if hexadecimal else int(value)
        return "" if codepoint in _INVISIBLE_CODEPOINTS else match.group()

    return _CHARACTER_REFERENCE.sub(drop_if_invisible, segment)


def _opening_fence(line: str, start: int = 0):
    """Return (character, length) when a fence opens at ``start``, else None.

    Per CommonMark, a backtick fence's info string cannot contain backticks, so
    ```` ```x``` ```` opens nothing and the line stays prose. The line is read
    from ``start`` without slicing the rest of it off.
    """
    delimiter = _FENCE_DELIMITER.match(line, start)
    if delimiter is None:
        return None
    run = delimiter.group()
    if run[0] == "`" and line.find("`", delimiter.end()) != -1:
        return None
    return run[0], len(run)


def _leading_spaces(text: str, position: int) -> int:
    end = position
    while text[end : end + 1] == " ":
        end += 1
    return end - position


def _indentation(line: str, position: int) -> tuple[int, int]:
    """The whitespace at ``position`` as the renderer measures it.

    Returns its width in columns, a tab counting to the next stop of four, and
    where the text after it begins. ``position`` is taken for the column the
    whitespace starts in, which holds where no tab comes before it.
    """
    column = position
    end = position
    while line[end : end + 1] in (" ", "\t"):
        column = (column // 4 + 1) * 4 if line[end] == "\t" else column + 1
        end += 1
    return column - position, end


def _thematic_span(line: str) -> tuple[int, int]:
    """First and last position where a thematic-break suffix of the line begins.

    ``(len(line), -1)`` when there is none. A suffix has to be looked at because
    a list marker is only a list marker where no thematic break takes precedence
    (``- - -`` is a rule, ``- - x`` is a list item).
    """
    end = len(line)
    while end and line[end - 1] in " \t":
        end -= 1
    character = line[end - 1 : end]
    if character not in "-*_":
        return len(line), -1
    start = end
    while start and line[start - 1] in (character, " ", "\t"):
        start -= 1
    marks = [position for position in range(start, end) if line[position] == character]
    if len(marks) < 3:
        return len(line), -1
    return marks[0], marks[-3]


def _opens_block(line: str) -> bool:
    """Whether a line at column 0 begins a block that interrupts any paragraph.

    Asked when the block structure is no longer followed, so the answer has to
    hold wherever the open paragraph is. In a quote's line it is the content
    that counts: ``> text`` goes on with a paragraph that is open inside that
    quote, while ``> # heading`` begins a block anywhere.
    """
    markers = _QUOTE_MARKERS.match(line)
    body = line[markers.end() :] if markers else line
    stripped = body.lstrip(" ")
    if len(body) - len(stripped) >= 4 or stripped.startswith("\t"):
        return False
    head = stripped[:1]
    if head == "#":
        return _ATX_HEADING.match(stripped) is not None
    if head in "`~":
        return _opening_fence(stripped) is not None
    if head in "-+*_":
        return _ITEM_ENDS_PARAGRAPH.match(stripped) is not None or _THEMATIC_BREAK.match(stripped) is not None
    return "0" <= head <= "9" and _ITEM_ENDS_PARAGRAPH.match(stripped) is not None


def _leading_items(chain: list) -> int:
    """How many list items stand at the head of ``chain``, before its first quote."""
    for index, token in enumerate(chain):
        if token == "quote":
            return index
    return len(chain)


def _match_chain(
    line: str, chain: list, childless: bool = False, leading: int | None = None
) -> tuple[int, int, bool]:
    """How many of the open containers the line continues, and where it ends.

    ``chain`` holds the open containers, outermost first: ``"quote"`` or the
    content indent of a list item, counted from where the containers around it
    end on the line. ``childless`` says the last of them is an item nothing has
    been put in yet, and ``leading`` may say how many items stand at the head of
    the chain, which spares a walk down it on a blank line. The last value says
    a tab was met in the prefixes, whose width depends on the column it stands in.

    Containers are matched as the renderer matches them: a quote needs its
    marker and an item its indent, except that a blank line lies in every item
    that has content, so a blank line closes paragraphs and quotes and nothing
    else. The line is read where each container starts and never sliced, so a
    line of many markers costs by its length alone.
    """
    if leading is not None and not childless and not line.strip(" \t"):
        # Every item matches a blank line and no quote does, and none of it moves.
        return leading, 0, False
    position = 0
    matched = 0
    width = _leading_spaces(line, position)
    content_end = len(line.rstrip(" \t"))  # where the line's last non-blank character ends
    for depth, token in enumerate(chain):
        if token == "quote":
            marker = _QUOTE_MARKER.match(line, position)
            if marker is None:
                break
            if "\t" in marker.group(0):
                return matched, position, True
            position = marker.end()
            width = _leading_spaces(line, position)
            matched += 1
            continue
        at = position + width
        empty = at >= content_end
        needs_indent = not empty or (childless and depth == len(chain) - 1)
        if line[at : at + 1] == "\t":
            # The tab may reach this item's indent or a deeper one's: neither is read here.
            if needs_indent or childless:
                return matched, position, True
        elif width < token and needs_indent:
            break
        if width >= token:
            position, width = position + token, width - token
        else:
            position, width = len(line), 0
        matched += 1
    return matched, position, False


def _item_start(line: str, start: int, interrupting: bool) -> tuple[int, int, bool, bool] | None:
    """A list item that begins at ``start``, or None.

    Returns the indent of the item's content counted from ``start``, where the
    content of its first line begins, whether that content is blank, and
    whether a tab stands in the marker's whitespace. ``interrupting`` is the
    case of a paragraph open in the item's own container: a bullet, or an item
    numbered 1, must then have content after it, and no other number starts an
    item at all. Five spaces or more after the marker leave the content
    indented; an item that starts with a blank line takes its content one
    column past the marker.
    """
    marker = _ITEM_MARKER.match(line, start)
    if marker is None:
        return None
    length = marker.end("marker") - start
    behind = marker.end("marker")
    if line[behind : behind + 1] not in ("", " ", "\t"):
        return None
    spaces = _ITEM_SPACE.match(line, behind).end() - behind
    content = behind + spaces
    blank = content == len(line)
    digits = marker.group("marker")[:-1]
    if interrupting and (blank or (digits and int(digits) != 1)):
        return None
    tabbed = "\t" in line[behind:content]
    if blank or spaces >= 5:
        return length + 1, start + length + (1 if spaces else 0), blank, tabbed
    return length + spaces, start + length + spaces, False, tabbed


def _parse_line(
    line: str, chain: list, paragraph_open: bool, childless: bool = False, leading: int | None = None
) -> tuple[int, list, str, str]:
    """Match the open containers against a line and parse the containers it opens.

    Returns how many of ``chain`` the line continues, the containers it opens,
    the text after all those prefixes, and what that text is: ``"text"`` (a
    paragraph line), ``"blank"``, ``"start"`` (a block of its own: a heading, a
    fence, a thematic break, a setext underline), ``"code"`` (indented code) or
    ``"untracked"`` (a tab in the prefixes, which is not followed). ``leading``
    goes through to ``_match_chain``.

    Block starts are judged by the CommonMark rule that a block may interrupt a
    paragraph only from inside the paragraph's own container. A list item
    numbered 2 continues the paragraph it sits in, and starts a list when the
    open paragraph lies deeper, in a quote or in another item.

    The line is read in place: the rest of it is cut off once, at the end, and
    not once per marker, so that a line of many nested markers stays linear.
    """
    matched, position, tabbed = _match_chain(line, chain, childless, leading)
    if tabbed:
        return matched, [], "", "untracked"
    opened: list = []
    # The paragraph open above is in the line's own container, or deeper and
    # continued lazily, or there is none. Only in the first case must a block
    # interrupt it.
    here = paragraph_open and matched == len(chain)
    lazy = paragraph_open and matched < len(chain)
    thematic_from, thematic_until = _thematic_span(line)
    content_end = len(line.rstrip(" \t"))  # where the line's last non-blank character ends
    while True:
        width = _leading_spaces(line, position)
        start = position + width
        if start < content_end and line[start] == "\t":
            return matched, opened, "", "untracked"
        if start >= content_end:
            return matched, opened, "", "blank"
        if width >= 4:
            return matched, opened, line[start:], "text" if here or lazy else "code"
        if (
            thematic_from <= start <= thematic_until
            or _ATX_HEADING.match(line, start) is not None
            or (line[start] in "`~" and _opening_fence(line, start) is not None)
            or (here and _SETEXT_UNDERLINE.match(line, start) is not None)
        ):
            return matched, opened, line[start:], "start"
        quote = _QUOTE_MARKER.match(line, start)
        if quote is not None:
            if "\t" in quote.group(0):
                return matched, opened, "", "untracked"
            opened.append("quote")
            position = quote.end()
            here = lazy = False
            continue
        item = _item_start(line, start, here)
        if item is not None:
            indent, content, blank, tabbed = item
            if tabbed:
                return matched, opened, "", "untracked"
            opened.append(width + indent)
            position = content
            if blank:
                return matched, opened, "", "blank"
            here = lazy = False
            continue
        return matched, opened, line[start:], "text"


class _Containers:
    """The containers open at the end of a line, followed as the renderer follows them."""

    def __init__(self, containers=()) -> None:
        self.chain: list = list(containers)  # outermost first, as in ``_match_chain``
        self.leading: int = _leading_items(self.chain)  # the list items at its head
        self.paragraph = False  # whether a paragraph is open at the end of ``chain``
        self.childless = False  # whether the last container is an item nothing has been put in yet

    def read(self, line: str) -> tuple[int, list, str, str]:
        """Read one line into the state; the return value is ``_parse_line``'s."""
        taken = _parse_line(line, self.chain, self.paragraph, self.childless, self.leading)
        matched, opened, _content, kind = taken
        before = self.chain
        # A paragraph line that lies outside some open container goes on with
        # that paragraph and closes nothing.
        lazy = kind == "text" and self.paragraph and matched < len(self.chain) and not opened
        if not lazy and kind != "untracked" and (opened or matched != len(self.chain)):
            # Only the head of the chain can lose its quote, or meet the new items.
            self.chain = self.chain[:matched] + opened
            if matched <= self.leading:
                self.leading = matched + _leading_items(opened)
        self.paragraph = kind == "text"
        # What is known about the last container holds on only over a blank line
        # that closes nothing: any other line puts something in it or ends it.
        if kind == "blank" and opened and opened[-1] != "quote":
            self.childless = True  # an item that starts with a blank line
        elif not (kind == "blank" and not opened and not lazy and matched == len(before)):
            self.childless = False
        return taken


def _possible_delimiter_row(line: str) -> bool:
    """Whether the line could be a table's delimiter row (``---|:-:``).

    Wider than the renderer's rule on purpose: a line taken for a delimiter row
    that the renderer keeps as text only costs the code spans around it.
    """
    body = _TABLE_PREFIX.sub("", line, count=1)
    return bool(body) and set(body) <= _DELIMITER_ROW_CHARACTERS


def _inline_runs(
    lines: list[str], *, joined: bool = False, containers=()
) -> tuple[list[int], set[int]]:
    """Split prose lines into the runs of inline content the renderer pairs backticks in.

    Returns the run of each line, consecutive lines of one run sharing a number,
    and the lines that no code span protects: indented code, and the lines whose
    place in the block structure is not followed here.

    The runs have to be the renderer's, exactly. Reading two lines as one run
    where it reads two pairs two backticks it keeps apart, and reading two where
    it reads one does the same, since the backticks left open by the first line
    then close differently. So wherever the structure is in doubt the line is
    marked unprotected instead of guessed. With ``joined``, the first line is
    paragraph text that goes on with the template's, and never a blank line.
    ``containers`` are the containers open where these lines begin, such as the
    list item a fenced block right before them sits in: a line reads differently
    inside an item than at the top level.
    """
    run_of: list[int] = []
    unprotected: set[int] = set()
    run = 0
    state = _Containers(containers)
    untracked = False  # the containers may be stale: nothing is taken for code until a fresh start
    for index, line in enumerate(lines):
        if index == 0 and joined:
            state.paragraph = True
            run += 1
            run_of.append(run)
            continue
        if untracked:
            # At column 0 a line is outside every container, and a block start
            # there interrupts whatever paragraph is open: either is a fresh start.
            if line[:1] in " \t" or (state.paragraph and not _opens_block(line)):
                run += 1
                unprotected.add(index)
                state.paragraph = True  # a paragraph may have opened with it
                run_of.append(run)
                continue
            state, untracked = _Containers(), False
        was_open = state.paragraph
        matched, opened, _body, kind = state.read(line)
        if kind == "text" and was_open and not opened:
            run_of.append(run)  # the paragraph goes on, in place or lazily
            continue
        run += 1
        run_of.append(run)
        if kind == "untracked":
            unprotected.add(index)
            state.paragraph, untracked = True, True
        elif kind == "code":
            unprotected.add(index)
    return run_of, unprotected


def _pair_backticks(runs: list[tuple[int, int]], openers: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Code spans from the backticks of one run of inline content.

    ``runs`` are the run's backtick strings as plain text sees them, and
    ``openers`` the ones that may open a span: every stretch of backticks that
    no backslash escapes, since the renderer reads an escape before it looks
    for a backtick string.

    This follows the renderer's own procedure (cmark's
    ``scan_to_closing_backticks``), not the specification's, because the two
    part ways twice. The look-ahead for a closer is a plain search for
    backticks, so a backtick a backslash escapes, which cannot open a span,
    does close one. And a look-ahead that reaches the end without finding a
    closer is remembered, along with the last position seen for each length;
    from then on a string is taken to have no closer whenever that remembered
    position lies behind it. So in `` `` `a` `b` `` the span around ``a`` moves
    the remembered position behind the next backtick, and `` `b` `` stays
    literal text. Each run and each opener is passed over at most twice.
    """
    last_seen: dict[int, int] = {}
    ran_out = False
    spans = []
    reached = 0  # the first run a look-ahead has not gone past
    index = 0
    while index < len(openers):
        start, length = openers[index]
        if length <= _MAX_BACKTICKS and not (ran_out and last_seen.get(length, 0) <= start + length):
            while reached < len(runs) and runs[reached][0] < start + length:
                reached += 1
            follower = reached
            while follower < len(runs):
                seen_start, seen_length = runs[follower]
                if seen_length <= _MAX_BACKTICKS:
                    last_seen[seen_length] = seen_start
                if seen_length == length:
                    break
                follower += 1
            if follower < len(runs):
                end = runs[follower][0] + length
                spans.append((start, end))
                index += 1
                while index < len(openers) and openers[index][0] < end:
                    index += 1
                continue
            ran_out = True
        index += 1
    return spans


def _outermost(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The maximal spans, sorted and disjoint: nested ones add no coverage."""
    kept = []
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        if not kept or end > kept[-1][1]:
            kept.append((start, end))
    return kept


def _inside(spans: list[tuple[int, int]], position: int) -> bool:
    """Whether ``position`` lies in one of the sorted, disjoint ``spans``."""
    index = bisect_right(spans, (position, float("inf"))) - 1
    return index >= 0 and spans[index][1] > position


def _code_spans(
    segment: str, escaped: frozenset[int], *, continued: bool, joined: bool = False, containers=()
) -> list[tuple[int, int]]:
    """Intervals of the segment that a code span covers.

    With ``continued``, the first paragraph gets no code span: another field
    precedes it on the same line and may hold an open backtick string, which
    would shift the pairing. ``joined`` and ``containers`` go through to
    ``_inline_runs``.
    """
    lines = segment.split("\n")
    line_start = []
    offset = 0
    for line in lines:
        line_start.append(offset)
        offset += len(line) + 1
    run_of, unprotected = _inline_runs(lines, joined=joined, containers=containers)
    first_run = run_of[0]
    runs: dict[int, list[tuple[int, int]]] = {}
    openers: dict[int, list[tuple[int, int]]] = {}
    for match in _BACKTICK_RUN.finditer(segment):
        start, end = match.span()
        line = bisect_right(line_start, start) - 1
        run = run_of[line]
        if line in unprotected or (continued and run == first_run):
            continue
        runs.setdefault(run, []).append((start, end - start))
        strings = openers.setdefault(run, [])
        position = start
        while position < end:  # a backslash-escaped backtick is a string that opens nothing
            if position in escaped:
                position += 1
                continue
            length = 1
            while position + length < end and position + length not in escaped:
                length += 1
            strings.append((position, length))
            position += length
    spans = []
    for run, run_runs in runs.items():
        spans.extend(_pair_backticks(run_runs, openers[run]))
    return _outermost(spans)


def _opens_markup(text: str, position: int) -> bool:
    """``<`` followed by a tag name, ``/`` and a tag name, ``!`` or ``?``."""
    following = position + 1
    if text[following : following + 1] == "/":
        following += 1
    character = text[following : following + 1]
    return character in ("!", "?") or (character.isascii() and character.isalpha())


def _content_start(line: str, start: int = 0) -> int:
    """Where a block begins in a line: the first position not preceded only by
    indentation, quote markers and list markers, each with the whitespace after it.

    That is where a tag would open an HTML block inside a container. The line
    is read once, in place: each step is fixed by the character it stands on,
    and a marker counts only with a space or a tab after it, at most nine
    digits before its ``.`` or ``)``. A pattern for this would be ambiguous,
    since whitespace after a marker also reads as indentation, and would
    backtrack over every reading of a line such as ``*\\t\\t*\\t\\t...``.
    """
    position = start
    while position < len(line):
        if line[position] in " \t>":
            position += 1
            continue
        marker = _ITEM_MARKER.match(line, position)
        if marker is None or line[marker.end() : marker.end() + 1] not in (" ", "\t"):
            break
        position = marker.end() + 1
    return position


def _url_escapes(segment: str, spans: list[tuple[int, int]]) -> list[int]:
    """Positions of the ``:`` and ``.`` inside bare URLs, outside code spans.

    Each URL is scanned once. A second autolink that starts inside an
    already-handled one has its ``:`` and ``.`` escaped by that same pass, so it
    is skipped rather than rescanned, which keeps a token made of glued URLs
    linear.
    """
    lowered = segment.lower()
    starts = []
    for prefix, stopped_by_punctuation in _URL_PREFIXES:
        found = lowered.find(prefix)
        while found != -1:
            before = segment[found - 1] if found else ""
            if before == "" or (
                before not in _WORD_CHARACTERS and not (stopped_by_punctuation and before in "-./:")
            ):
                starts.append(found)
            found = lowered.find(prefix, found + len(prefix))
    starts.sort()
    positions = []
    handled_until = 0
    for start in starts:
        if start < handled_until or _inside(spans, start):
            continue
        end = start
        while end < len(segment) and not segment[end].isspace() and segment[end] not in "`<":
            end += 1
        positions.extend(position for position in range(start, end) if segment[position] in ":.")
        handled_until = end
    return positions


def _object_escapes(segment: str, spans: list[tuple[int, int]], *, tags: bool) -> list[int]:
    """Positions to put a backslash before: the objects outside code spans."""
    escaped = _escaped_positions(segment)
    positions = []
    # Where a block begins on the line being read. A tag inside a code span is
    # asked for it once per tag, so the line is read once per line, not once per tag.
    line_end = 0
    block_at = 0

    def starts_block(position: int) -> bool:
        nonlocal line_end, block_at
        if position >= line_end:
            following = segment.find("\n", position)
            line_end = len(segment) if following == -1 else following + 1
            block_at = _content_start(segment, segment.rfind("\n", 0, position) + 1)
        return position <= block_at

    for character in ("[", "]", "<") if tags else ("[", "]"):
        position = segment.find(character)
        while position != -1:
            if position not in escaped and (
                not _inside(spans, position)
                or (character == "<" and starts_block(position))
            ):
                if character != "<" or _opens_markup(segment, position):
                    positions.append(position)
            position = segment.find(character, position + 1)
    positions.extend(_url_escapes(segment, spans))
    return sorted(positions)


def _insert_backslashes(text: str, positions: list[int]) -> str:
    pieces = []
    consumed = 0
    for position in positions:
        pieces.append(text[consumed:position])
        pieces.append("\\")
        consumed = position
    pieces.append(text[consumed:])
    return "".join(pieces)


def _published_lines(lines: list[str]) -> list[list]:
    """The lines as the renderer will see them once comments are gone.

    Invisible characters and HTML comments are removed, and a comment that
    spans a line break takes the break with it, so its last line joins the line
    it began on. Each entry is ``[first raw line, last raw line, text]``. The
    scan is the one ``_strip_html_comments`` makes, so the two cannot disagree
    about what a line says.
    """
    text = "\n".join(_strip_invisible(line) for line in lines)
    closers = _positions(text, "-->")
    published: list[list] = []
    parts: list[str] = []  # the current line's text, joined at its end: a long line is not recopied per piece
    current = [0, 0, ""]
    raw = 0
    position = 0

    def keep(piece: str) -> None:
        nonlocal raw, current, parts
        pieces = piece.split("\n")
        parts.append(pieces[0])
        for part in pieces[1:]:  # a line break survives: the published line ends here
            current[2] = "".join(parts)
            published.append(current)
            raw += 1
            current = [raw, raw, ""]
            parts = [part]

    while True:
        opener = text.find("<!--", position)
        if opener == -1:
            keep(text[position:])
            break
        keep(text[position:opener])
        closer = _first_at_or_after(closers, opener + 4)
        if closer is None:
            position = opener + 4  # an unclosed opener swallows nothing but itself
            continue
        # The comment goes, its line breaks with it: what follows joins the line.
        raw += text.count("\n", opener, closer + 3)
        current[1] = raw
        position = closer + 3
    current[2] = "".join(parts)
    published.append(current)
    return published


def _html_head_size(lines: list[str]) -> int:
    """How many of the field's lines the template's HTML block takes.

    That block ends at the field's first blank line, judged on the published
    text, since the prose rules can empty a line or merge two. The count covers
    that blank line whole, closer of a spanning comment included. The field's
    first line never is that line: it shares its line with the template's tag.
    """
    for _first, last, body in _published_lines(lines)[1:]:
        if not body.strip(" \t"):
            return last + 1
    return len(lines)


def _flush_prose(
    lines: list[str],
    result: list[str],
    *,
    continued: bool = False,
    joined: bool = False,
    no_code_spans: bool = False,
    stripped: bool = False,
    in_html: bool = False,
    markdown_rules: bool = True,
    containers=(),
) -> None:
    """Apply the prose rules to a run of unfenced lines as one segment.

    The run is one segment, so a multiline HTML comment cannot straddle two.
    Invisible characters drop first, so a zero-width space hiding inside an
    opener cannot smuggle a comment past the scan. With ``stripped``, rules 1
    and 3 have been applied already, by the scan that split the field.

    ``in_html`` is for a field that starts inside one of the template's HTML
    blocks. A backslash escapes nothing there, so a ``<`` is entity-encoded
    instead, wherever it stands. ``markdown_rules`` says whether the markdown
    ones apply as well: raw HTML parses no markdown, a paragraph with inline
    HTML does. ``joined`` and ``continued`` say what precedes the first line,
    see ``_inline_runs``, and ``containers`` what is open around it. With
    ``no_code_spans``, nothing is taken for code: the run's place in the block
    structure is not known.
    """
    if not lines:
        return
    segment = "\n".join(lines)
    if stripped:
        if not in_html:
            # What the scan left of the openers: those it rebuilt.
            segment = segment.replace("<!--", "<\\!--")
    else:
        segment = _strip_invisible(segment)
        segment = _strip_html_comments(segment, neutralize=not in_html)
    if markdown_rules:
        spans = (
            []
            if no_code_spans
            else _code_spans(
                segment,
                _escaped_positions(segment),
                continued=continued,
                joined=joined,
                containers=containers,
            )
        )
        segment = _insert_backslashes(segment, _object_escapes(segment, spans, tags=not in_html))
    if in_html:
        segment = segment.replace("<", "&lt;")
    result.extend(segment.split("\n"))


def _quiet_markers(line: str) -> str:
    """Keep a line of code from carrying a PR-Agent control marker.

    Code is published as it stands, and the control plane reads its markers off
    the comment body as text, without looking at code blocks. A marker opener
    inside a code block would be read as well as one in prose, so it is
    neutralized with the backslash rule 1 uses. Only openers that name PR-Agent
    are touched: an HTML comment in a code sample is left alone.
    """
    return _CONTROL_MARKER_OPENER.sub("<\\\\!--", line)


def _code_block(
    kind: str, content: str, line: str, first: int, raw: int, blocks: list[list], containers: list
):
    """Take a parsed published line into a block of code, or None when it is not one.

    The line is published as given, marker openers aside. The return value is
    the block's state for ``_field_blocks``: its kind, its fence for a fence,
    the containers it sits in, and how many list items stand at their head.
    """
    if kind == "start" and (fence := _opening_fence(content)) is not None:
        shape = ("fence", fence[0], fence[1])
    elif kind == "code":
        shape = ("code", None, None)
    else:
        return None
    # A code block begins a block of its own, so it ends every container it is not in.
    blocks.append([True, first, raw, [_quiet_markers(line)], False, []])
    return (*shape, containers, _leading_items(containers))


def _field_blocks(lines: list[str], *, joined: bool = False, prose_only: bool = False) -> list[list]:
    """Split the field into the blocks the renderer will see, in order.

    Each entry is ``[inert, first raw line, last raw line, lines to publish,
    lost, containers]``. An inert block is code, a fence or an indented code
    block: it is published as it stands, except for a control marker opener in
    it. It renders as text already, and quoting hostile text in a block of code
    is the legitimate channel for it. Everything else is prose and goes through
    ``_flush_prose``, with ``containers`` the ones open where it begins. A lost
    block is prose whose place in the structure is not known: its lines are
    published after the prose rules with no code span taken for granted.

    The split has to be the renderer's, exactly, so it is read off the text the
    renderer will get: rule 3 first, then the comments of rule 1, since either
    can empty a line, and a line they empty ends a paragraph or a list item, and
    with it a code block inside that item; and since removing a comment can put
    a fence at the start of a line. A comment swallows the line breaks inside
    it, so its last line joins the line it began on. A line that begins with the
    fence delimiter is a fence before anything else is removed from it: what
    follows the delimiter there is the info string, where a comment is no
    comment. With ``joined``, the first line is paragraph text and opens no
    block: the template may put markup before it on the same line.

    Containers are followed as ``_inline_runs`` follows them, so a code block
    inside a quote or a list item lasts exactly as long as that container does:
    the renderer closes it where the container ends, and keeps it over a blank
    line where the container stays open. That too is read off the line as it is
    published: one that leaves the container as it stands is published as prose,
    and what rules 1 and 3 take out of it may move it back in. Where the
    structure cannot be followed, the field is lost from there on: at a table,
    whose rows are cut at their pipes before any inline parsing; at a tab in the
    indentation or in the markers of a quote or a list item, whose width depends
    on the column it stands in. A lost field is picked up again where the
    renderer can have nothing open, at a line that follows a blank line and
    starts at column 0, unless a fence has been opened since, because where that
    closes is not known. ``prose_only`` loses the whole field, for one whose
    first line reads differently under the template's branches.
    """
    if not lines:
        return []
    if prose_only:
        return [[False, 0, len(lines) - 1, None, True, []]]
    texts = [_strip_invisible(line) for line in lines]
    starts = []  # where each raw line begins in ``texts``
    offset = 0
    for line in texts:
        starts.append(offset)
        offset += len(line) + 1
    text = "\n".join(texts)
    # Found once: an opener with no closer behind it must not search for one each time.
    openers = _positions(text, "<!--")
    closers = _positions(text, "-->")
    newlines = _positions(text, "\n")
    blocks: list[list] = []
    state = _Containers()
    inert = None  # ("fence" | "code", character, length, containers, items at their head): published as it stands
    lost = False
    after_blank = False  # in a lost field: a blank line has passed
    fenced = False  # in a lost field: a fence may have been opened
    header_at = -1  # raw line the last prose line began on, the header row of a possible table
    header_text = False  # whether that line has text
    raw = 0

    def prose(first: int, last: int, body, containers: list) -> None:
        if body is None:  # a line of a lost field: flushed as it stands
            if blocks and not blocks[-1][0] and blocks[-1][4]:
                blocks[-1][2] = last
            else:
                blocks.append([False, first, last, None, True, []])
        elif blocks and not blocks[-1][0] and not blocks[-1][4]:
            blocks[-1][2] = last
            blocks[-1][3].append(body)
        else:
            blocks.append([False, first, last, [body], False, list(containers)])

    def without_comments(position: int, raw: int) -> tuple[str, int]:
        """Rule 1 over the published line that starts at ``position``, on raw line ``raw``.

        Returns what is left of the line and the raw line it ends on, since a
        comment takes the line breaks inside it along.
        """
        kept: list[str] = []  # what rule 1 leaves of this published line
        while True:
            opener = _first_at_or_after(openers, position)
            newline = _first_at_or_after(newlines, position)
            if opener is not None and (newline is None or opener < newline):
                kept.append(text[position:opener])
                closer = _first_at_or_after(closers, opener + 4)
                if closer is None:
                    position = opener + 4  # an unclosed opener goes, and nothing with it
                    continue
                raw += text.count("\n", opener, closer + 3)  # the comment takes its line breaks along
                position = closer + 3
                continue
            kept.append(text[position : newline if newline is not None else len(text)])
            return "".join(kept), raw

    while raw < len(lines):
        line = lines[raw]
        if inert is not None:
            # Judged on the line as the renderer gets it. In the block that is the line as it
            # stands; outside it, the line is published as prose, rules 1 and 3 first. What
            # they take out moves what follows it, so that line may lie in the block's
            # containers after all, where the renderer keeps the block open.
            published, last = line, raw
            matched, position, tabbed = _match_chain(line, inert[3], leading=inert[4])
            inside = matched == len(inert[3]) and not tabbed
            if not inside:
                published, last = without_comments(starts[raw], raw)
                matched, position, tabbed = _match_chain(published, inert[3], leading=inert[4])
                inside = matched == len(inert[3]) and not tabbed
            if inside:
                indent, end = _indentation(published, position)
                if inert[0] == "fence":
                    run = len(published) - end - len(published[end:].lstrip(inert[1]))
                    # The closing fence stands at most three columns in and has nothing after it.
                    if indent <= 3 and run >= inert[2] and not published[end + run :].strip(" \t"):
                        inert = None
                elif published.strip(" \t") and indent < 4:
                    inside = False  # indented code goes on over blank lines and indented lines only
            if inside:
                blocks[-1][2] = last
                blocks[-1][3].append(_quiet_markers(published))
                raw = last + 1
                continue
            inert = None  # the container ended, and the code block with it
        first = raw  # the raw line the published line begins on
        position = starts[raw]
        newline = text.find("\n", position)
        whole = text[position : newline if newline != -1 else len(text)]  # comments kept
        taken = (0, [], whole, "text") if first == 0 and joined else _parse_line(
            whole, state.chain, state.paragraph, state.childless, state.leading
        )
        if not lost and taken[3] in ("start", "code") and (
            found := _code_block(taken[3], taken[2], whole, first, raw, blocks, state.chain[: taken[0]] + taken[1])
        ) is not None:
            inert, state.chain = found, found[3]
            state.paragraph, state.childless, state.leading, header_at = False, False, found[4], -1
            raw += 1
            continue
        body, raw = without_comments(position, raw)
        if lost:
            if body.strip(" \t") and after_blank and not fenced and body[:1] not in " \t":
                lost, state, after_blank = False, _Containers(), False
            else:
                after_blank = not body.strip(" \t")
                if not after_blank and _opening_fence(_TABLE_PREFIX.sub("", body, count=1)) is not None:
                    fenced = True
                prose(first, raw, None, [])
                raw += 1
                continue
        opening = state.chain
        if first == 0 and joined:
            state.paragraph = True
            matched, opened, _content, kind = 0, [], body, "text"
        else:
            matched, opened, _content, kind = state.read(body)
        if kind == "untracked":
            lost, after_blank, fenced, header_at = True, False, False, -1
            prose(first, raw, None, [])
        elif header_at >= 0 and header_text and _possible_delimiter_row(body):
            # A table: its header row is the line above, and a row is cut at
            # its pipes before any inline parsing.
            blocks[-1][3].pop()
            blocks[-1][2] = header_at - 1
            if blocks[-1][2] < blocks[-1][1]:
                blocks.pop()
            prose(header_at, raw, None, [])
            lost, after_blank, fenced, header_at = True, False, False, -1
        elif (found := _code_block(kind, _content, body, first, raw, blocks, state.chain)) is not None:
            # Removing a comment can put a fence at the start of a line.
            inert = found
            state.paragraph, state.childless, header_at = False, False, -1
        else:
            prose(first, raw, body, opening)
            # With the template's markup before it on its line, the first line has text
            # whatever it holds, and can be a table's header row.
            header_at = first
            header_text = bool(body.strip(" \t")) or (not body and first == 0 and joined)
        raw += 1
    return blocks


def _firewall_markdown(
    lines: list[str], *, continued: bool = False, joined: bool = False, prose_only: bool = False
) -> list[str]:
    """The markdown rules over the lines of a field: prose in, code through."""
    result: list[str] = []
    for inert, first, last, published, lost, containers in _field_blocks(
        lines, joined=joined, prose_only=prose_only
    ):
        if inert:
            result.extend(published)
        elif lost:
            _flush_prose(lines[first : last + 1], result, no_code_spans=True)
        else:
            # Only a block that begins the field continues another line.
            begins = first == 0
            _flush_prose(
                published,
                result,
                continued=continued and begins,
                joined=joined and begins,
                stripped=True,
                containers=containers,
            )
    return result


def _reads_alike(line: str) -> bool:
    """Whether a first line reads the same as paragraph text and as the start of a block."""
    if not line.strip(" \t"):
        return False  # a line break inside a paragraph, or a blank line ending it
    _matched, opened, _body, kind = _parse_line(line, [], False)
    return kind == "text" and not opened


def _plain_paragraph(lines: list[str]) -> bool:
    """Whether every line is paragraph text at the top level, as the renderer reads it.

    The paragraph is taken for open before the first line. A blank line only
    ends it; anything else makes the answer false.
    """
    for line in lines:
        matched, opened, _content, kind = _parse_line(line, [], True)
        if kind not in ("text", "blank") or matched or opened:
            return False
    return True


def firewall_model_output(
    markdown: str, *, context: str = "markdown", after: str = "blank", continued: bool = False
) -> str:
    """Apply the publish rules to one model-authored field.

    ``context`` names where the template renders the field, ``after`` what
    precedes its first line, and ``continued`` that another model field precedes
    it on that line; all three are spelled out in the module docstring.
    """
    if context not in CONTEXTS:
        raise ValueError(f"Unknown publish context: {context!r}")
    if after not in AFTER:
        raise ValueError(f"Unknown line start: {after!r}")
    if context != "markdown" and after != "blank":
        raise ValueError(f"A {context} field follows a blank line, not {after!r}")
    if not isinstance(markdown, str) or not markdown:
        return markdown
    # The renderer ends a line at a carriage return too, so both are read alike here.
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    if context == "escaped":
        return _strip_invisible(markdown)
    lines = markdown.split("\n")
    if context == "escaped_block":
        # The template escapes the HTML itself up to the field's first blank line.
        head = _html_head_size(lines)
        return "\n".join([*(_strip_invisible(line) for line in lines[:head]), *_firewall_markdown(lines[head:])])
    if context == "markdown":
        prefixed = after == "item"
        if prefixed:
            # The template writes a list marker before the field: let the rules
            # see the line the renderer sees, and take the marker off again.
            lines = ["- " + lines[0], *lines[1:]]
        doubt = after == "maybe" and not _reads_alike(lines[0])
        result = _firewall_markdown(
            lines,
            continued=continued or doubt,
            joined=after in ("markup", "maybe"),
            prose_only=doubt,
        )
        if prefixed and result:
            result[0] = result[0][2:]
        return "\n".join(result)
    # The template's HTML block ends at the field's first blank line.
    head = _html_head_size(lines)
    # In the template's other branch the head is a paragraph, parsed as markdown, and
    # whatever it opens stays open past that line: a fence crosses it, and a list item
    # or a quote holds what follows. Where the head is anything but plain paragraph
    # lines, neither reading can be followed, and nothing is taken for code.
    plain = context != "html_inline" or _plain_paragraph(
        [body for _first, _last, body in _published_lines(lines[:head])[1:]]
    )
    result: list[str] = []
    _flush_prose(
        lines[:head],
        result,
        in_html=True,
        markdown_rules=context == "html_inline",
        joined=True,
        no_code_spans=not plain,
    )
    result.extend(_firewall_markdown(lines[head:], prose_only=not plain))
    return "\n".join(result)
