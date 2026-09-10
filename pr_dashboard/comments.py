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
    """Extract findings from a review comment, across every findings_layout and gfm support."""
    findings: list[Finding] = []
    lines = body.splitlines()
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
        if not location:
            # A non-gfm_supported "expanded" layout puts the location on the line right after
            # the title (render_focus_area_issue joins them with a single "\n", not "<br>"); a
            # "details" layout finding legitimately has no location anywhere.
            for next_line in lines[index + 1:]:
                if next_line.strip():
                    location = _FILE_AND_LINES.search(next_line)
                    break
        findings.append(_finding_from_location(title, location) if location else Finding(title=title))
    return findings
