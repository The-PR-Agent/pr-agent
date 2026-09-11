"""Recognise and parse the comments PR-Agent posts.

Identification uses the hidden identity markers PR-Agent already embeds in every comment
it publishes, imported from pr_agent rather than re-declared. Neither of the two obvious
alternatives is reliable: comment author fails because a self-hosted fork often posts
under a human token, and visible headings fail because a repository can override them
through pr_reviewer.review_heading. Heading matching survives only as a fallback for
comments posted before the markers existed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pr_agent.algo.utils import (
    _ALL_COMMENT_IDENTITIES,
    PRCodeSuggestionsHeader,
    PRCodeSuggestionsIdentity,
    PRReviewHeader,
    PRReviewIdentity,
)

IDENTITY_MARKERS = tuple(_ALL_COMMENT_IDENTITIES)

# Fallback only, for comments published before identity markers existed. A repository that
# overrode its heading and has no marker cannot be recognised, which is accepted.
_LEGACY_HEADINGS = {
    PRReviewHeader.REGULAR.value: "REVIEW",
    PRReviewHeader.INCREMENTAL.value: "INCREMENTAL_REVIEW",
    PRCodeSuggestionsHeader.SUMMARY.value: "SUGGESTIONS",
}

# The file must be delimited (backtick or <code>...</code>) -- a bare match would also
# accept a dotted word inside a neighbouring <a href> URL (e.g. "github.com" from the link
# render_focus_area_issue always attaches on GitHub), stealing the location from its real
# `<code>` occurrence later on the same line. The end line is optional: render_focus_area_issue
# (pr_agent/algo/utils.py:586) omits the dash entirely for a single-line finding ("L42", not
# "L42-42"), and when absent the end resolves to the same line as the start.
_FILE_AND_LINES = re.compile(
    r"(?:`(?P<file_bt>[\w./\-]+\.\w+)`|<code>(?P<file_code>[\w./\-]+\.\w+)</code>)"
    r"[^\n]*?\[?(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?\]?"
)
# Legacy/hand-authored format: "- **Title** `file` [a-b]". Bold run only -- an unanchored
# capture would swallow the trailing location into the title.
_BOLD_TITLE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\*\*(?P<title>[^*]+?)\*\*")
# render_focus_area_issue's actual output (see pr_agent/algo/utils.py): on a gfm_supported
# provider (GitHub included) a finding's title is HTML <strong>, with no leading list marker
# at all, optionally wrapped in an <a href> link and, for the "details" layout with a
# reference link, further wrapped in <details><summary>.
_HTML_TITLE = re.compile(r"^\s*(?:<details><summary>\s*)?(?:<a\s[^>]*>\s*)?<strong>(?P<title>[^<]+?)</strong>")
# render_focus_area_issue's non-gfm_supported output (e.g. Bitbucket, which never supports
# gfm_markdown): the title is markdown bold, linked as "[**Title**](url)" or bare "**Title**",
# again with no leading list marker.
_MD_LINK_TITLE = re.compile(r"^\s*\[\*\*(?P<title>[^*]+?)\*\*\]\([^)]*\)")
_MD_BOLD_TITLE = re.compile(r"^\s*\*\*(?P<title>[^*]+?)\*\*")
# Last resort: an unadorned bullet line with no bold run, for comments with neither shape.
_PLAIN_TITLE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(?P<title>[^*\n][^\n]*?)\s*$")

# render_focus_area_issue's output lives inside one section of the comment, headed by one of
# these two labels (pr_agent/algo/utils.py's emojis map carries both: "Recommended focus areas
# for review" is what convert_to_markdown_v2 actually renders today, "Key issues to review" is
# kept as a fallback for the label it replaced). Every *other* section of a review comment --
# security concerns, TODO sections, the effort estimate -- can contain its own bold lead-ins and
# bullets, and must never be scanned for findings.
_FOCUS_AREA_MARKERS = ("Recommended focus areas for review", "Key issues to review")
# The non-gfm layout's own section heading is "### {emoji} Recommended focus areas for review",
# followed immediately by a level-4 "#### " sub-heading -- so the next *level-3* heading must be
# matched precisely (not preceded by a 4th '#', which "#### " would otherwise satisfy).
_NEXT_SECTION_HEADING = re.compile(r"(?<!#)###(?!#)\s")

# Mirrored from convert_to_markdown_v2's local `emojis` dict (pr_agent/algo/utils.py). Kept here
# rather than imported because that dict is function-local; TestMirroredSectionLabels guards drift.
_GFM_SECTION_LABELS = frozenset({
    "Can be split",
    "Key issues to review",
    "Recommended focus areas for review",
    "Score",
    "Relevant tests",
    "Focused PR",
    "Relevant ticket",
    "Security concerns",
    "Todo sections",
    "Insights from user's answers",
    "Code feedback",
    "Estimated effort to review [1-5]",
    "Contribution time cost estimate",
    "Ticket compliance check",
    "Risk level",
    "Merge recommendation",
    "Review priority files",
})
# Real GFM section rows open as: <tr><td>{emoji}&nbsp;<strong>{label}</strong>...
_GFM_SECTION_ROW_OPEN = re.compile(
    r"^[^<]*&nbsp;<strong>(?P<label>[^<]+)</strong>"
)


def _is_known_gfm_section_row(after_tr_td: str) -> bool:
    """True when `after_tr_td` (text after `<tr><td>`) opens a known convert_to_markdown_v2 section."""
    match = _GFM_SECTION_ROW_OPEN.match(after_tr_td)
    if not match:
        return False
    label = match.group("label")
    if label in _GFM_SECTION_LABELS:
        return True
    # Renderer rewrites a few keys before emitting <strong> (e.g. drops " [1-5]", uppercases
    # "Todo" to "TODO"). Accept casefold equality or a mirrored key that only adds a " [...]" suffix.
    label_cf = label.casefold()
    for known in _GFM_SECTION_LABELS:
        known_cf = known.casefold()
        if known_cf == label_cf or known_cf.startswith(label_cf + " ["):
            return True
    return False


def _gfm_focus_area_end(body: str, start: int) -> int:
    """Return the index of the real GFM cell close after `start`, or -1.

    convert_to_markdown_v2 closes the focus-area row with `</td></tr>` immediately before
    the next section row or `</table>`. A bare `</td></tr>` or even `</td></tr><tr><td>`
    inside model-generated issue content is not enough: the following `<tr><td>` must open a
    known section label (`{emoji}&nbsp;<strong>{label}</strong>` for a label in
    `_GFM_SECTION_LABELS`). This is hardening, not a proof — a model that emits the exact
    emoji, `&nbsp;`, `<strong>`, and a real section label is indistinguishable from a genuine
    section to any parser. The failure direction is under-reporting (later findings dropped),
    never fabricating findings.
    """
    needle = "</td></tr>"
    row_open = "<tr><td>"
    search_from = start
    while True:
        index = body.find(needle, search_from)
        if index == -1:
            return -1
        after = body[index + len(needle):].lstrip()
        if after.startswith("</table>"):
            return index
        if after.startswith(row_open) and _is_known_gfm_section_row(after[len(row_open):]):
            return index
        search_from = index + len(needle)


def _focus_area_section(body: str) -> Optional[str]:
    """Return the slice of `body` covering only the focus-area/key-issues section, or None."""
    starts = [index for index in (body.find(marker) for marker in _FOCUS_AREA_MARKERS) if index != -1]
    if not starts:
        return None
    start = min(starts)
    table_end = _gfm_focus_area_end(body, start)
    heading_match = _NEXT_SECTION_HEADING.search(body, start)
    ends = [e for e in (table_end, heading_match.start() if heading_match else -1) if e != -1]
    return body[start: min(ends)] if ends else body[start:]


class CommentKind(str, Enum):
    REVIEW = "review"
    INCREMENTAL_REVIEW = "incremental_review"
    SUGGESTIONS = "suggestions"
    OTHER = "other"


@dataclass(frozen=True)
class Finding:
    title: str
    relevant_file: Optional[str] = None
    line_range: Optional[tuple[int, int]] = None


def classify(body: str) -> CommentKind:
    """Return which PR-Agent output a comment body is, or OTHER when it is not ours."""
    if not body:
        return CommentKind.OTHER
    if PRReviewIdentity.INCREMENTAL.value in body:
        return CommentKind.INCREMENTAL_REVIEW
    if PRReviewIdentity.REGULAR.value in body:
        return CommentKind.REVIEW
    if any(marker in body for marker in (
        PRCodeSuggestionsIdentity.SUMMARY.value,
        PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
        PRCodeSuggestionsIdentity.UNANCHORED.value,
    )):
        return CommentKind.SUGGESTIONS
    for heading, kind in _LEGACY_HEADINGS.items():
        if heading in body:
            return CommentKind[kind]
    return CommentKind.OTHER


def is_pr_agent_comment(body: str) -> bool:
    """True when the comment was published by PR-Agent."""
    return classify(body) is not CommentKind.OTHER


def _finding_from_location(title: str, location: re.Match) -> Finding:
    start = int(location.group("start"))
    end_text = location.group("end")
    return Finding(
        title=title,
        relevant_file=location.group("file_bt") or location.group("file_code"),
        line_range=(start, int(end_text) if end_text else start),
    )


def parse_findings(body: str) -> list[Finding]:
    """Extract findings from a review's focus-area section, across every findings_layout and gfm support.

    Only text between the focus-area heading and the end of that section is scanned: a
    comment's other sections (security concerns, TODO sections, ...) can carry their own bold
    lead-ins and bullets, which must never be misread as findings. A comment whose own verdict
    is "no major issues" -- so the heading never appears -- yields no findings.
    """
    section = _focus_area_section(body)
    if section is None:
        return []
    findings: list[Finding] = []
    lines = section.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        title_match = (
            _BOLD_TITLE.match(raw_line)
            or _HTML_TITLE.match(raw_line)
            or _MD_LINK_TITLE.match(raw_line)
            or _MD_BOLD_TITLE.match(raw_line)
            or _PLAIN_TITLE.match(raw_line)
        )
        if not title_match:
            continue
        title = title_match.group("title").strip()
        if not title:
            continue
        # Search past the title itself: a backtick-quoted dotted token inside the title (e.g.
        # "Handle `config.yml` parsing failure") would otherwise be read as the location.
        location = _FILE_AND_LINES.search(raw_line, title_match.end())
        if not location and index + 1 < len(lines):
            # A non-gfm_supported "expanded" layout puts the location on the line immediately
            # after the title (render_focus_area_issue joins them with a single "\n", not
            # "<br>"), and that line is nothing but the location. Check only that one line, and
            # require it to match in full: scanning further ahead, or merely searching within
            # it, risks mistaking a later finding's title or content for this finding's
            # location. A "details" layout finding legitimately has no location anywhere.
            location = _FILE_AND_LINES.fullmatch(lines[index + 1].strip())
        findings.append(_finding_from_location(title, location) if location else Finding(title=title))
    return findings
