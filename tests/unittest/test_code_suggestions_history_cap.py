"""Keep the persistent history cap from discarding entries it should not.

History entries are stored with a "✅ " prefix once their suggestions are applied. The
entry count below handles both forms, but the truncation that enforces the cap searched
for the unticked form only, so it either matched a newer entry and wiped the whole
history, or found nothing and chopped the closing tag off the last entry.
"""
import re
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.comment_identity import PRCodeSuggestionsIdentity
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

NAME = "suggestions"
CAP = 2


def _entry(ticked, commit, tag):
    tick = "✅ " if ticked else ""
    return (
        f"<details><summary>{tick}{NAME.capitalize()} up to commit {commit}</summary>\n"
        f"<br><table><tr><td>{tag}</td></tr></table>\n\n</details>\n"
    )


def _body(history_entries):
    return (
        "## Suggestions\n\n"
        f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n\n"
        "<!-- aaa1111 -->\n\n"
        "Latest suggestions up to commit aaa1111\n\n"
        "<table><tr><td>current</td></tr></table>\n\n___\n\n"
        "#### Previous suggestions\n"
        + "".join(history_entries)
    )


def _update(history_entries, cap=CAP):
    existing = MagicMock()
    existing.body = _body(history_entries)
    provider = MagicMock()
    provider.get_issue_comments.return_value = [existing]
    provider.get_issue_comments_newest_first.return_value = [existing]
    provider.get_comment_url.return_value = "https://example.test/comment/1"
    provider.get_latest_commit_url.return_value = "https://example.test/commit/deadbee"

    result = PRCodeSuggestions.publish_persistent_comment_with_history(
        provider,
        "## Suggestions\n\n<table><tr><td>newest</td></tr></table>",
        initial_header="## Suggestions",
        name=NAME,
        identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
        max_previous_comments=cap,
    )
    assert result is existing
    return provider.edit_comment.call_args.args[1]


def _history_tags(body):
    """Return the row tags of the retained history entries, newest first.

    The latest suggestions are moved into the history as the newest entry, so every
    result starts with the "current" row that _body() put above the history header.
    """
    history = body.split("#### Previous suggestions")[1]
    return re.findall(r"<td>(\w+)</td>", history)


def _assert_well_formed(body):
    assert body.count("<details>") == body.count("</details>")
    assert "</detail>" not in body.replace("</details>", "")


@pytest.mark.parametrize(
    "entries,expected",
    [
        pytest.param([(False, "c1", "mid"), (False, "c0", "old")], ["current", "mid"], id="all-unticked"),
        pytest.param([(False, "c1", "mid"), (True, "c0", "old")], ["current", "mid"], id="oldest-applied"),
        pytest.param([(True, "c1", "mid"), (True, "c0", "old")], ["current", "mid"], id="all-applied"),
        pytest.param(
            [(False, "c2", "new"), (False, "c1", "mid"), (True, "c0", "old")],
            ["current", "new", "mid"],
            id="oldest-applied-with-three",
        ),
    ],
)
def test_cap_drops_only_the_oldest_history_entry(entries, expected):
    """The cap must remove exactly one entry, whichever form the entries use."""
    body = _update([_entry(*e) for e in entries])

    assert _history_tags(body) == expected
    _assert_well_formed(body)


def test_applied_oldest_entry_is_still_counted_against_the_cap():
    """An applied entry counts, so the cap still fires when every entry is applied."""
    body = _update([_entry(True, "c1", "mid"), _entry(True, "c0", "old")])

    assert "old" not in _history_tags(body)
    _assert_well_formed(body)


def test_history_is_not_wiped_when_the_oldest_entry_is_applied():
    """Regression: the unticked-only match used to match the newest entry and drop all."""
    body = _update([_entry(False, "c1", "mid"), _entry(True, "c0", "old")])

    assert _history_tags(body) == ["current", "mid"]
    _assert_well_formed(body)


def test_closing_tag_is_not_chopped_when_every_entry_is_applied():
    """Regression: rfind returned -1, so [:-1] turned </details> into </detail>."""
    body = _update([_entry(True, "c1", "mid"), _entry(True, "c0", "old")])

    assert body.rstrip().endswith("</details>")
    _assert_well_formed(body)


def test_unticked_history_still_truncates_as_before():
    """The unticked-only path that already worked must keep working unchanged."""
    body = _update([_entry(False, "c1", "mid"), _entry(False, "c0", "old")])

    assert _history_tags(body) == ["current", "mid"]
    assert "up to commit c0" not in body
    _assert_well_formed(body)


def test_history_is_untouched_below_the_cap():
    """Below the cap nothing is dropped, applied or not."""
    body = _update([_entry(True, "c1", "only")], cap=2)

    assert _history_tags(body) == ["current", "only"]
    _assert_well_formed(body)
