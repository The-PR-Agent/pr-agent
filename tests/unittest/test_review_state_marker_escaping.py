"""The persistent state marker must stay a comment whatever a finding says.

The marker is an HTML comment wrapping JSON. HTML comments do not nest and end at the first
`-->` (or `--!>`), so a finding that quotes an arrow - a mermaid edge, an XML comment, prose -
would close the marker early and spill the rest of the JSON into the rendered review.
"""
from html.parser import HTMLParser

import pytest

from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    serialize_review_state,
)

REVIEW = "## PR Reviewer Guide 🔍\n\n<!-- pr-agent:review:full -->\n\n<table><tr><td>body</td></tr></table>"


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)


def _visible(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return "".join(parser.chunks)


def _state_for(body: str):
    finding = {"path": "docs/flow.md", "body": body, "line_start": 3, "line_end": 3}
    return reconcile_review_findings(None, [finding], allow_resolution=False, head_sha="abc123").state


@pytest.mark.parametrize("body", [
    "**Possible Issue**\n\nThe edge `A --> B` is drawn twice.",
    "The comment `<!-- keep -->` is duplicated.",
    "An abrupt close --!> also ends a comment.",
    "Arrows everywhere: --> --> -->",
])
def test_the_marker_stays_hidden(body):
    rendered = append_review_state(REVIEW, _state_for(body))

    visible = _visible(rendered)
    assert "schema_version" not in visible
    assert "finding_id" not in visible


@pytest.mark.parametrize("body", [
    "**Possible Issue**\n\nThe edge `A --> B` is drawn twice.",
    "The comment `<!-- keep -->` is duplicated.",
    "An abrupt close --!> also ends a comment.",
])
def test_the_finding_round_trips_unchanged(body):
    rendered = append_review_state(REVIEW, _state_for(body))

    parsed = parse_review_state(rendered)

    assert parsed.valid and parsed.present
    # Compare whitespace-insensitively: this test is about the escaping surviving the round
    # trip, not about how normalize_finding stores line breaks.
    assert " ".join(parsed.state["findings"][0]["body"].split()) == " ".join(body.split())


def test_a_finding_without_an_arrow_is_untouched():
    """Control: the ordinary payload keeps its exact serialisation."""
    state = _state_for("**Possible Issue**\n\nThe retry loop never ends.")

    marker = serialize_review_state(state)

    assert "\\u003e" not in marker
    assert parse_review_state(f"{REVIEW}\n\n{marker}").valid
