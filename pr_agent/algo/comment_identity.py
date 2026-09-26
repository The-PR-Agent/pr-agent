from __future__ import annotations

import string
from enum import Enum
from typing import Iterable

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


class PRReviewHeader(str, Enum):
    REGULAR = "## PR Reviewer Guide"
    INCREMENTAL = "## Incremental PR Reviewer Guide"


class PRReviewIdentity(str, Enum):
    REGULAR = "<!-- pr-agent:review:full -->"
    INCREMENTAL = "<!-- pr-agent:review:incremental -->"


class PRCodeSuggestionsHeader(str, Enum):
    SUMMARY = "## PR Code Suggestions ✨"


class PRCodeSuggestionsIdentity(str, Enum):
    SUMMARY = "<!-- pr-agent:improve:summary -->"
    NO_SUGGESTIONS = "<!-- pr-agent:improve:no-suggestions -->"
    UNANCHORED = "<!-- pr-agent:improve:unanchored -->"


class PRDescriptionHeader(str, Enum):
    DIAGRAM_WALKTHROUGH = "Diagram Walkthrough"
    FILE_WALKTHROUGH = "File Walkthrough"


_ALL_COMMENT_IDENTITIES = (
    PRReviewIdentity.REGULAR.value,
    PRReviewIdentity.INCREMENTAL.value,
    PRCodeSuggestionsIdentity.SUMMARY.value,
    PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
    PRCodeSuggestionsIdentity.UNANCHORED.value,
)
_REVIEW_IDENTITY_HEADER_LINES = 5
_MARKDOWN_PUNCTUATION_ESCAPE_TABLE = str.maketrans(
    {character: f"\\{character}" for character in string.punctuation}
)


def _get_configured_heading(setting_name: str, default_heading: str) -> str:
    configured_heading = get_settings().get(setting_name)
    if (
        not isinstance(configured_heading, str)
        or not configured_heading.strip()
        or configured_heading.splitlines() != [configured_heading]
    ):
        get_logger().warning(
            f"Invalid {setting_name}; using the default heading"
        )
        configured_heading = default_heading
    return configured_heading.strip()


def format_pr_review_header(incremental: bool = False) -> str:
    """Return the visible review heading while keeping identity out of presentation."""
    default_heading = PRReviewHeader.REGULAR.value.removeprefix("## ")
    heading = _get_configured_heading("pr_reviewer.review_heading", default_heading)
    incremental_prefix = "Incremental " if incremental else ""
    return f"## {incremental_prefix}{heading} 🔍"


def format_pr_code_suggestions_header(markdown_level: int = 2) -> str:
    """Return the visible suggestions heading while keeping identity out of presentation."""
    default_heading = (
        PRCodeSuggestionsHeader.SUMMARY.value
        .removeprefix("## ")
        .removesuffix(" ✨")
    )
    heading = _get_configured_heading(
        "pr_code_suggestions.suggestions_heading",
        default_heading,
    )
    markdown_prefix = "#" * markdown_level
    return f"{markdown_prefix} {heading} ✨"


def format_pr_questions_header(*, escape_markdown: bool = True) -> str:
    """Return the visible heading for top-level /ask answers."""
    heading = _get_configured_heading("pr_questions.ask_heading", "Ask")
    if escape_markdown:
        heading = heading.translate(_MARKDOWN_PUNCTUATION_ESCAPE_TABLE)
    return f"### **{heading}** ❓"


def hidden_marker_forms(identity: str) -> tuple[str, ...]:
    """Return both stored forms of a known comment identity."""
    for marker in _ALL_COMMENT_IDENTITIES:
        reference = f"[{marker[5:-4]}]: https://github.com/The-PR-Agent/pr-agent"
        if identity in (marker, reference):
            return marker, reference
    return (identity,)


def render_hidden_marker(identity: str, git_provider=None) -> str:
    """Use a link reference on providers that escape HTML comments."""
    forms = hidden_marker_forms(identity)
    supports_html = getattr(git_provider, "supports_html_comment_markers", lambda: True)
    return forms[-1] if supports_html() is False else forms[0]


def comment_matches_identity(body: str, identity: str) -> bool:
    """Match hidden markers only as exact lines near the top; legacy headers as prefixes."""
    if not isinstance(body, str) or not isinstance(identity, str) or not identity:
        return False
    forms = hidden_marker_forms(identity)
    if identity.startswith("<!--") or len(forms) > 1:
        return any(
            line.strip() in forms
            for line in body.splitlines()[:_REVIEW_IDENTITY_HEADER_LINES]
        )
    return body.startswith(identity)


def comment_matches_any_identity(body: str, identities: Iterable[str]) -> bool:
    return any(comment_matches_identity(body, identity) for identity in identities)


def comment_carries_other_identity(body: str, identity_marker: str | None) -> bool:
    """Return whether the comment carries a different hidden identity."""
    return comment_matches_any_identity(
        body,
        [identity for identity in _ALL_COMMENT_IDENTITIES if identity not in hidden_marker_forms(identity_marker)],
    )


def get_pr_review_comment_identifiers(*, full: bool, incremental: bool) -> tuple[str, ...]:
    """Return stable markers followed by legacy visible prefixes for migration."""
    identifiers = []
    if full:
        identifiers.extend((PRReviewIdentity.REGULAR.value, PRReviewHeader.REGULAR.value))
    if incremental:
        identifiers.extend((PRReviewIdentity.INCREMENTAL.value, PRReviewHeader.INCREMENTAL.value))
    return tuple(identifiers)


def add_comment_identity(pr_comment: str, identity_marker: str | None, git_provider=None) -> str:
    """Insert a hidden identity after the visible heading without changing rendered output."""
    if not pr_comment or not identity_marker or comment_matches_identity(pr_comment, identity_marker):
        return pr_comment
    identity_marker = render_hidden_marker(identity_marker, git_provider)
    heading, separator, remainder = pr_comment.partition("\n\n")
    if not separator:
        return f"{pr_comment.rstrip()}\n\n{identity_marker}"
    return f"{heading}\n\n{identity_marker}\n\n{remainder}"


def add_pr_review_identity(pr_comment: str, identity_marker: str | None, git_provider=None) -> str:
    return add_comment_identity(pr_comment, identity_marker, git_provider)


def as_review_text(value) -> str:
    """Flatten a review field the model returned as a list or mapping into readable text.

    The prompt asks for a single string, but a model enumerating several findings commonly
    answers with a list or a mapping. Rendering those is preferable to losing the review.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        value = [f"{key}: {item}" for key, item in value.items()]
    if isinstance(value, (list, tuple, set)):
        entries = [as_review_text(item) for item in value]
        return "\n".join(f"- {entry}" for entry in entries if entry)
    return str(value).strip()


def emphasize_header(text: str, only_markdown=False, reference_link=None) -> str:
    try:
        # Find the first occurrence of ": ".
        colon_position = text.find(": ")

        # Wrap everything before the colon in <strong> tags.
        if colon_position != -1:
            # Wrap the portion up to and including the colon.
            if only_markdown:
                if reference_link:
                    transformed_string = (
                        f"[**{text[:colon_position + 1]}**]({reference_link})\n"
                        + text[colon_position + 1:]
                    )
                else:
                    transformed_string = f"**{text[:colon_position + 1]}**\n" + text[colon_position + 1:]
            else:
                if reference_link:
                    transformed_string = (
                        f"<strong><a href='{reference_link}'>{text[:colon_position + 1]}</a></strong><br>"
                        + text[colon_position + 1:]
                    )
                else:
                    transformed_string = (
                        "<strong>" + text[:colon_position + 1] + "</strong>" + "<br>" + text[colon_position + 1:]
                    )
        else:
            # Return the original string when no colon exists.
            transformed_string = text

        return transformed_string
    except Exception as e:
        get_logger().exception(f"Failed to emphasize header: {e}")
        return text
