"""A bare `#N` is a ticket only when it reads as a reference, not as markup, a colour or a fragment.

Fixes #4196: `#N` inside a closed HTML comment, as the whole text of a link, glued to a word
on either side, or inside a Tailwind arbitrary value (`bg-[#121212]`) must not become a ticket.
"""
import pytest

from pr_agent.tools.ticket_pr_compliance_check import extract_ticket_links_from_pr_description

REPO = "org/repo"
BASE = "https://github.com"


def _links(description):
    return extract_ticket_links_from_pr_description(description, REPO, BASE)


@pytest.mark.parametrize("description", [
    "<!-- Example: Closes #1234. See also #3456. -->",
    "<!--\nFixes #1234\n-->",
])
def test_references_inside_html_comments_are_ignored(description):
    assert _links(description) == []


def test_a_link_whose_text_is_only_a_hash_number_is_ignored():
    description = ('<a href="https://redirect.github.com/vercel/vercel/issues/6211">#6211</a>')
    assert _links(description) == []


def test_a_markdown_link_whose_text_is_only_a_hash_number_is_ignored():
    assert _links("See [#6211](https://example.com/vercel/vercel/issues/6211)") == []


@pytest.mark.parametrize("description", ["issue#12", "colour #0a0a0a", "value #0a"])
def test_a_hash_number_glued_to_a_word_is_ignored(description):
    assert _links(description) == []


@pytest.mark.parametrize("description", [
    'class="bg-[#121212]"',
    "dark:bg-[#0a0a0a] and bg-[#121212]",
])
def test_tailwind_arbitrary_values_are_ignored(description):
    assert _links(description) == []


@pytest.mark.parametrize("description, number", [
    ("Fixes #42", "42"),
    ("(#42)", "42"),
    ("see [#42]", "42"),
    ("Fixes [#42] (crash on start)", "42"),
    ("Related: [#42] [#43]", "42"),
    ("<!-- old text --> Fixes #42", "42"),
    ('<a href="https://example.com/x">#42</a> fixes #7', "7"),
])
def test_real_references_are_still_extracted(description, number):
    links = _links(description)
    assert f"{BASE}/{REPO}/issues/{number}" in links
