"""
Focused unit tests for Markdown / parser / ticket-visible output helpers.

These tests document current behavior of helper seams that render
user-visible Markdown / HTML for review output, ticket compliance,
TODO sections, and PR description ticket extraction. The goal is to
lock in branches that are not already covered by:

- tests/unittest/test_convert_to_markdown.py
- tests/unittest/test_parse_code_suggestion.py
- tests/unittest/test_extract_issue_from_branch.py
- tests/unittest/test_pr_description.py

Assertions intentionally key off structural markers (emoji, header
text, anchor/href substrings, list bullets) rather than full golden
strings to remain robust against trivial whitespace changes.
"""

from unittest.mock import Mock

import pytest

from pr_agent.algo.utils import (
    convert_to_markdown_v2,
    emphasize_header,
    format_todo_item,
    format_todo_items,
    is_value_no,
    parse_code_suggestion,
    process_can_be_split,
    ticket_markdown_logic,
)
from pr_agent.tools.pr_description import insert_br_after_x_chars
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_ticket_links_from_pr_description,
    find_jira_tickets,
)

# ---------------------------------------------------------------------------
# is_value_no / emphasize_header
# ---------------------------------------------------------------------------


class TestIsValueNo:
    @pytest.mark.parametrize(
        "value",
        ["No", "no", "NONE", " false ", "", None, 0, [], {}],
    )
    def test_truthy_no_values(self, value):
        assert is_value_no(value) is True

    @pytest.mark.parametrize("value", ["yes", "Yes", "true", "maybe", "123"])
    def test_other_values_are_not_no(self, value):
        assert is_value_no(value) is False


class TestEmphasizeHeader:
    def test_html_emphasis_with_colon(self):
        out = emphasize_header("Header: details continue here")
        # First segment is wrapped in <strong> and split with <br>.
        assert out.startswith("<strong>Header:</strong>")
        assert "<br>" in out
        assert "details continue here" in out

    def test_markdown_only_emphasis_with_colon(self):
        out = emphasize_header("Header: rest", only_markdown=True)
        assert out.startswith("**Header:**")
        # Newline-separated rest of text (no <br>).
        assert "\n rest" in out
        assert "<br>" not in out

    def test_reference_link_html(self):
        out = emphasize_header(
            "Header: rest", reference_link="https://example.com/x"
        )
        assert "<a href='https://example.com/x'>Header:</a>" in out
        assert out.startswith("<strong>")

    def test_reference_link_markdown(self):
        out = emphasize_header(
            "Header: rest",
            only_markdown=True,
            reference_link="https://example.com/x",
        )
        assert "[**Header:**](https://example.com/x)" in out

    def test_no_colon_returns_unchanged(self):
        text = "Plain text without a delimiter"
        assert emphasize_header(text) == text


# ---------------------------------------------------------------------------
# convert_to_markdown_v2 — branches not covered elsewhere
# ---------------------------------------------------------------------------


class TestConvertToMarkdownV2Branches:
    def test_empty_review_returns_empty(self):
        # When the review dict exists but is missing, output is empty.
        assert convert_to_markdown_v2({}).strip() == ""
        assert convert_to_markdown_v2({"review": None}).strip() == ""

    def test_incremental_review_header_and_note(self):
        out = convert_to_markdown_v2(
            {"review": {"security_concerns": "No"}},
            incremental_review="2 commits",
        )
        assert "Incremental PR Reviewer Guide" in out
        assert "Review for commits since previous PR-Agent review 2 commits" in out

    def test_relevant_tests_yes_branch_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"relevant_tests": "Yes"}}
        )
        assert "<strong>PR contains tests</strong>" in out
        assert "<table>" in out and "</table>" in out

    def test_relevant_tests_yes_branch_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"relevant_tests": "Yes"}}, gfm_supported=False
        )
        assert "### 🧪 PR contains tests" in out
        assert "<table>" not in out

    def test_relevant_tests_no_branch_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"relevant_tests": "No"}}, gfm_supported=False
        )
        assert "### 🧪 No relevant tests" in out

    def test_security_concerns_with_details_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"security_concerns": "SQL injection: details follow"}}
        )
        assert "<strong>Security concerns</strong>" in out
        # emphasize_header wraps the part before ':' in <strong>.
        assert "<strong>SQL injection:</strong>" in out

    def test_security_concerns_with_details_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"security_concerns": "SQL injection: details"}},
            gfm_supported=False,
        )
        assert "### 🔒 Security concerns" in out
        assert "**SQL injection:**" in out

    def test_key_issues_no_major_issues_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"key_issues_to_review": "No"}}
        )
        assert "<strong>No major issues detected</strong>" in out

    def test_key_issues_no_major_issues_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"key_issues_to_review": "No"}}, gfm_supported=False
        )
        assert "### ⚡ No major issues detected" in out

    def test_key_issues_possible_bug_header_softened(self):
        mock_provider = Mock()
        mock_provider.get_line_link.return_value = "https://example.com/diff"
        out = convert_to_markdown_v2(
            {
                "review": {
                    "key_issues_to_review": [
                        {
                            "relevant_file": "src/x.py",
                            "issue_header": "possible bug",
                            "issue_content": "may explode",
                            "start_line": 1,
                            "end_line": 2,
                        }
                    ]
                }
            },
            git_provider=mock_provider,
        )
        # 'possible bug' is rewritten to the less alarming 'Possible Issue'.
        assert "Possible Issue" in out
        assert "possible bug" not in out

    def test_key_issues_without_provider_renders_strong_header(self):
        out = convert_to_markdown_v2(
            {
                "review": {
                    "key_issues_to_review": [
                        {
                            "relevant_file": "src/x.py",
                            "issue_header": "Code Smell",
                            "issue_content": "long",
                            "start_line": 1,
                            "end_line": 2,
                        }
                    ]
                }
            }
        )
        # No reference link → plain <strong> header, no anchor.
        assert "<strong>Code Smell</strong>" in out
        assert "<a href=" not in out

    def test_estimated_effort_non_numeric_prefix(self):
        # "3, because ..." → only the leading integer is used.
        out = convert_to_markdown_v2(
            {"review": {"estimated_effort_to_review_[1-5]": "3, because of churn"}}
        )
        assert "Estimated effort to review</strong>: 3 🔵🔵🔵⚪⚪" in out

    def test_estimated_effort_invalid_value_is_skipped(self):
        # Completely unparsable value falls through `continue` and is omitted.
        out = convert_to_markdown_v2(
            {"review": {"estimated_effort_to_review_[1-5]": "not-a-number"}}
        )
        assert "Estimated effort to review" not in out

    def test_can_be_split_single_item_renders_no_themes(self):
        out = convert_to_markdown_v2(
            {
                "review": {
                    "can_be_split": [
                        {"relevant_files": ["a.py"], "title": "Only one"}
                    ]
                }
            }
        )
        assert "<strong>No multiple PR themes</strong>" in out

    def test_can_be_split_empty_renders_no_themes(self):
        out = convert_to_markdown_v2({"review": {"can_be_split": []}})
        assert "<strong>No multiple PR themes</strong>" in out

    def test_default_branch_unknown_key_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"some_other_field": "interesting value"}}
        )
        # Fallback formatting capitalizes & joins with ': '.
        assert "<strong>Some other field</strong>: interesting value" in out

    def test_default_branch_unknown_key_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"some_other_field": "interesting value"}},
            gfm_supported=False,
        )
        assert "###  Some other field: interesting value" in out

    def test_todo_sections_no_value_gfm(self):
        out = convert_to_markdown_v2({"review": {"todo_sections": "No"}})
        assert "<strong>No TODO sections</strong>" in out
        assert "✅" in out

    def test_todo_sections_no_value_non_gfm(self):
        out = convert_to_markdown_v2(
            {"review": {"todo_sections": "No"}}, gfm_supported=False
        )
        assert "### ✅ No TODO sections" in out

    def test_todo_sections_list_with_provider_gfm(self):
        provider = Mock()
        provider.get_line_link.return_value = "https://example.com/L10"
        out = convert_to_markdown_v2(
            {
                "review": {
                    "todo_sections": [
                        {
                            "relevant_file": "src/x.py",
                            "line_number": 10,
                            "content": "finish refactor",
                        }
                    ]
                }
            },
            git_provider=provider,
        )
        assert "<strong>TODO sections</strong>" in out
        assert "<ul>" in out
        assert "<a href='https://example.com/L10'>src/x.py [10]</a>" in out
        assert "finish refactor" in out


# ---------------------------------------------------------------------------
# ticket_markdown_logic — covers branches the existing single-ticket test
# in test_convert_to_markdown.py does not (mixed, not-compliant, partial,
# empty list, non-gfm rendering).
# ---------------------------------------------------------------------------


class TestTicketMarkdownLogic:
    @pytest.fixture(autouse=True)
    def _cleanup_extra_statistics(self):
        """``ticket_markdown_logic`` writes ``config.extra_statistics`` as a
        side effect; snapshot and restore it so these tests don't leak
        ``compliance_level`` state into other tests sharing the settings
        singleton.
        """
        from tests.unittest._settings_helpers import (
            restore_settings,
            snapshot_settings,
        )

        snapshot = snapshot_settings(["config.extra_statistics"])
        try:
            yield
        finally:
            restore_settings(snapshot)

    def _ticket(self, **overrides):
        base = {
            "ticket_url": "https://example.com/ticket/42",
            "ticket_requirements": "- R1\n",
            "fully_compliant_requirements": "",
            "not_compliant_requirements": "",
            "requires_further_human_verification": "",
        }
        base.update(overrides)
        return base

    def test_not_a_list_returns_unchanged(self):
        # Defensive branch: non-list values are ignored.
        out = ticket_markdown_logic("🎫", "PREFIX", "not-a-list", True)
        assert out == "PREFIX"

    def test_empty_list_still_renders_header_without_compliance_emoji(self):
        # Current behavior: even with an empty ticket list the gfm branch
        # appends a header row, but with an empty compliance emoji and no
        # body. This documents the quirk so a future refactor that skips
        # the header in this case will surface as a deliberate change.
        out = ticket_markdown_logic("🎫", "PREFIX", [], True)
        assert out.startswith("PREFIX")
        assert "Ticket compliance analysis" in out
        # No compliance emoji is rendered after the heading text.
        assert "Ticket compliance analysis **" in out

    def test_not_compliant_only_renders_red_x(self):
        tickets = [
            self._ticket(not_compliant_requirements="- broken\n")
        ]
        out = ticket_markdown_logic("🎫", "", tickets, True)
        assert "Ticket compliance analysis ❌" in out
        assert "Not compliant" in out
        assert "Non-compliant requirements:" in out

    def test_partially_compliant_renders_orange_diamond(self):
        tickets = [
            self._ticket(
                fully_compliant_requirements="- ok\n",
                not_compliant_requirements="- broken\n",
            )
        ]
        out = ticket_markdown_logic("🎫", "", tickets, True)
        assert "Ticket compliance analysis 🔶" in out
        assert "Partially compliant" in out
        # Both sections are rendered.
        assert "Compliant requirements:" in out
        assert "Non-compliant requirements:" in out

    def test_mixed_full_and_not_compliant_renders_partial(self):
        tickets = [
            self._ticket(fully_compliant_requirements="- ok\n"),
            self._ticket(
                ticket_url="https://example.com/ticket/43",
                not_compliant_requirements="- broken\n",
            ),
        ]
        out = ticket_markdown_logic("🎫", "", tickets, True)
        # Mix of Fully compliant + Not compliant ⇒ overall Partially compliant 🔶.
        assert "Ticket compliance analysis 🔶" in out
        # Both ticket id slugs are rendered.
        assert "[42](https://example.com/ticket/42)" in out
        assert "[43](https://example.com/ticket/43)" in out

    def test_requires_further_human_verification_marks_pr_code_verified(self):
        tickets = [
            self._ticket(
                fully_compliant_requirements="- ok\n",
                requires_further_human_verification="- check infra\n",
            )
        ]
        out = ticket_markdown_logic("🎫", "", tickets, True)
        assert "PR Code Verified" in out
        assert "Requires further human verification:" in out
        # All tickets verified ⇒ green check.
        assert "Ticket compliance analysis ✅" in out

    def test_ticket_with_no_requirements_renders_header_only(self):
        # Tickets that have neither compliant nor non-compliant requirements
        # are skipped in the per-ticket loop, but the gfm branch still
        # emits an (empty-body) header row. This documents that current
        # behavior — no compliance level or per-ticket detail is shown.
        tickets = [self._ticket()]
        out = ticket_markdown_logic("🎫", "", tickets, True)
        assert "Ticket compliance analysis" in out
        # No per-ticket body rendered.
        assert "https://example.com/ticket/42" not in out

    def test_non_gfm_renders_markdown_heading(self):
        tickets = [self._ticket(fully_compliant_requirements="- ok\n")]
        out = ticket_markdown_logic("🎫", "", tickets, gfm_supported=False)
        assert out.startswith("### 🎫 Ticket compliance analysis ✅")
        assert "<tr>" not in out


# ---------------------------------------------------------------------------
# process_can_be_split — direct helper tests for edge inputs.
# ---------------------------------------------------------------------------


class TestProcessCanBeSplit:
    def test_empty_value_returns_no_themes(self):
        out = process_can_be_split("🔀", [])
        assert "No multiple PR themes" in out

    def test_single_element_list_returns_no_themes(self):
        out = process_can_be_split(
            "🔀", [{"title": "only one", "relevant_files": ["a.py"]}]
        )
        assert "No multiple PR themes" in out

    def test_multiple_themes_render_details(self):
        out = process_can_be_split(
            "🔀",
            [
                {"title": "Refactor", "relevant_files": ["a.py", "b.py"]},
                {"title": "Fix", "relevant_files": ["c.py"]},
            ],
        )
        assert "<details><summary>" in out
        # Each theme title is rendered.
        assert "<b>Refactor</b>" in out
        assert "<b>Fix</b>" in out
        # Relevant files are bullet-listed.
        assert "- a.py" in out
        assert "- b.py" in out
        assert "- c.py" in out


# ---------------------------------------------------------------------------
# format_todo_item / format_todo_items
# ---------------------------------------------------------------------------


class TestFormatTodoItem:
    def _provider(self, link="https://example.com/L5"):
        p = Mock()
        p.get_line_link.return_value = link
        return p

    def test_gfm_with_content_uses_anchor(self):
        out = format_todo_item(
            {"relevant_file": "src/a.py", "line_number": 5, "content": "do it"},
            self._provider(),
            gfm_supported=True,
        )
        assert "<a href='https://example.com/L5'>src/a.py [5]</a>: do it" == out

    def test_non_gfm_with_content_uses_markdown_link(self):
        out = format_todo_item(
            {"relevant_file": "src/a.py", "line_number": 5, "content": "do it"},
            self._provider(),
            gfm_supported=False,
        )
        assert out == "[src/a.py [5]](https://example.com/L5): do it"

    def test_empty_content_returns_only_file_ref(self):
        out = format_todo_item(
            {"relevant_file": "src/a.py", "line_number": 5, "content": ""},
            self._provider(),
            gfm_supported=True,
        )
        assert out.endswith("src/a.py [5]</a>")
        assert ":" not in out.split("</a>")[-1]  # no trailing ": "

    def test_no_reference_link_plain_file_ref(self):
        out = format_todo_item(
            {"relevant_file": "src/a.py", "line_number": 5, "content": "x"},
            self._provider(link=""),
            gfm_supported=True,
        )
        # Falsy reference_link → no anchor tag.
        assert "<a href" not in out
        assert out == "src/a.py [5]: x"


class TestFormatTodoItems:
    def _provider(self):
        p = Mock()
        p.get_line_link.return_value = "https://example.com/L1"
        return p

    def test_single_item_gfm_wraps_in_paragraph(self):
        out = format_todo_items(
            {"relevant_file": "f.py", "line_number": 1, "content": "x"},
            self._provider(),
            gfm_supported=True,
        )
        assert out.startswith("<p>") and out.rstrip().endswith("</p>")

    def test_single_item_non_gfm_uses_bullet(self):
        out = format_todo_items(
            {"relevant_file": "f.py", "line_number": 1, "content": "x"},
            self._provider(),
            gfm_supported=False,
        )
        assert out.startswith("- ")

    def test_list_truncates_to_max_items_gfm(self):
        items = [
            {"relevant_file": f"f{i}.py", "line_number": i, "content": "x"}
            for i in range(10)
        ]
        out = format_todo_items(items, self._provider(), gfm_supported=True)
        # MAX_ITEMS is 5 — only the first five files appear, the rest are dropped.
        for i in range(5):
            assert f"f{i}.py" in out
        for i in range(5, 10):
            assert f"f{i}.py" not in out
        assert out.count("<li>") == 5

    def test_list_truncates_to_max_items_non_gfm(self):
        items = [
            {"relevant_file": f"f{i}.py", "line_number": i, "content": "x"}
            for i in range(7)
        ]
        out = format_todo_items(items, self._provider(), gfm_supported=False)
        # Counts bullet rows.
        assert out.count("\n- ") + (1 if out.startswith("- ") else 0) == 5


# ---------------------------------------------------------------------------
# parse_code_suggestion — gfm branch with relevant_line is not exercised
# by tests/unittest/test_parse_code_suggestion.py.
# ---------------------------------------------------------------------------


class TestParseCodeSuggestionGfm:
    def test_relevant_line_with_markdown_link(self):
        suggestion = {
            "relevant_file": "src/app.py",
            "suggestion": "Use a constant",
            "relevant_line": "[`foo = 1`](https://example.com/diff#L10)",
        }
        out = parse_code_suggestion(suggestion, gfm_supported=True)
        assert out.startswith("<table>")
        assert "<tr><td>relevant file</td><td>src/app.py</td></tr>" in out
        assert "<strong>" in out and "Use a constant" in out
        assert "<a href='https://example.com/diff#L10'>" in out
        assert out.rstrip().endswith("<hr>")

    def test_relevant_line_without_link(self):
        suggestion = {
            "relevant_file": "src/app.py",
            "suggestion": "Use a constant",
            "relevant_line": "`foo = 1`",
        }
        out = parse_code_suggestion(suggestion, gfm_supported=True)
        # No "](" link delimiter → no anchor, just the (leading-backtick
        # stripped) literal line.
        assert "<a href=" not in out
        assert "<tr><td>relevant line</td>" in out
        assert "foo = 1" in out

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "parse_code_suggestion only left-strips a leading backtick from "
            "relevant_line; the trailing backtick is not stripped. This xfail "
            "encodes the desired symmetric stripping behavior."
        ),
    )
    def test_relevant_line_strips_both_backticks(self):
        suggestion = {
            "relevant_file": "src/app.py",
            "suggestion": "Use a constant",
            "relevant_line": "`foo = 1`",
        }
        out = parse_code_suggestion(suggestion, gfm_supported=True)
        assert "<td>foo = 1</td>" in out

    def test_falls_back_to_non_gfm_when_no_relevant_line(self):
        # Without 'relevant_line', the function takes the non-gfm code path
        # even when gfm_supported=True.
        suggestion = {"suggestion": "S", "description": "D"}
        out = parse_code_suggestion(suggestion, gfm_supported=True)
        assert "<table>" not in out
        assert "**suggestion:**" in out
        assert "**description:**" in out


# ---------------------------------------------------------------------------
# insert_br_after_x_chars — edges around its very short-circuit branches.
# ---------------------------------------------------------------------------


class TestInsertBrAfterXChars:
    def test_empty_returns_empty_string(self):
        assert insert_br_after_x_chars("") == ""
        assert insert_br_after_x_chars(None) == ""

    def test_short_text_returned_unchanged(self):
        text = "short text"
        assert insert_br_after_x_chars(text) == text

    def test_long_text_inserts_br(self):
        text = "word " * 30  # well over default x=70
        out = insert_br_after_x_chars(text)
        assert "<br>" in out

    def test_bullet_list_starts_with_li(self):
        text = (
            "- first bullet with a fair amount of text "
            "that should clearly exceed the seventy character limit\n"
            "- second bullet"
        )
        out = insert_br_after_x_chars(text)
        assert "<li>" in out


# ---------------------------------------------------------------------------
# Ticket extraction from PR description / Jira ticket detection.
# ---------------------------------------------------------------------------


class TestFindJiraTickets:
    def test_finds_standard_jira_id(self):
        assert "PROJ-123" in find_jira_tickets("Fixes PROJ-123 today")

    def test_finds_jira_via_url(self):
        text = "See https://company.atlassian.net/browse/ABC-9 for details"
        tickets = find_jira_tickets(text)
        assert "ABC-9" in tickets

    def test_no_match_returns_empty(self):
        assert find_jira_tickets("nothing here") == []

    def test_short_uppercase_prefix_not_matched(self):
        # Requires at least 2 uppercase letters; single-letter prefixes ignored.
        assert find_jira_tickets("A-1 should not match") == []

    def test_deduplicates_repeated_tickets(self):
        tickets = find_jira_tickets("PROJ-1 PROJ-1 PROJ-1")
        assert tickets == ["PROJ-1"]


class TestExtractTicketLinksFromPRDescription:
    def test_full_url_extracted(self):
        desc = "Closes https://github.com/foo/bar/issues/7 and more"
        out = extract_ticket_links_from_pr_description(desc, "foo/bar")
        assert "https://github.com/foo/bar/issues/7" in out

    def test_shorthand_owner_repo_issue(self):
        desc = "See foo/bar#42 for context"
        out = extract_ticket_links_from_pr_description(
            desc, "foo/bar", base_url_html="https://github.com"
        )
        assert "https://github.com/foo/bar/issues/42" in out

    def test_hash_only_uses_repo_path(self):
        desc = "Fixes #5"
        out = extract_ticket_links_from_pr_description(desc, "foo/bar")
        assert "https://github.com/foo/bar/issues/5" in out

    def test_hash_only_requires_repo_path(self):
        desc = "Fixes #5"
        # Without repo_path, '#5'-only references cannot be resolved.
        out = extract_ticket_links_from_pr_description(desc, "")
        assert out == []

    def test_hash_only_rejects_long_numbers(self):
        desc = "Fixes #12345 (5 digits, looks like a code, not an issue)"
        out = extract_ticket_links_from_pr_description(desc, "foo/bar")
        assert out == []

    def test_results_capped_at_three(self):
        desc = " ".join(f"foo/bar#{i}" for i in range(1, 8))
        out = extract_ticket_links_from_pr_description(desc, "foo/bar")
        assert len(out) == 3

    def test_base_url_trailing_slash_is_stripped(self):
        desc = "See foo/bar#1"
        out = extract_ticket_links_from_pr_description(
            desc, "foo/bar", base_url_html="https://ghe.example.com/"
        )
        assert out == ["https://ghe.example.com/foo/bar/issues/1"]
