"""Merging chunked reviews must render the same shapes the single-call review accepts.

`security_concerns` and `insights_from_user_answers` are declared as strings, but a model
enumerating several findings answers with a list; `convert_to_markdown_v2` has rendered that
since #2899. The chunk merger still stringified the value, so a chunked review printed a
Python list repr into the PR.
"""
import pytest

from pr_agent.algo.review_merge import merge_review_chunks
from pr_agent.algo.utils import is_value_no


def _merged(*chunk_values, field="security_concerns"):
    return merge_review_chunks([{"review": {field: value}} for value in chunk_values])["review"][field]


def test_a_list_from_one_chunk_is_rendered_as_text():
    merged = _merged(["SQL injection in the query builder"], "No")

    assert "SQL injection in the query builder" in merged
    assert "['" not in merged and "']" not in merged


def test_entries_from_several_chunks_are_unioned():
    merged = _merged(["SQL injection in the query builder"], ["Missing CSRF token"])

    assert "SQL injection in the query builder" in merged
    assert "Missing CSRF token" in merged
    assert "['" not in merged


def test_a_multi_entry_list_becomes_bullets():
    merged = _merged(["First concern", "Second concern"], "No")

    assert "- First concern" in merged
    assert "- Second concern" in merged


def test_a_mapping_is_rendered_as_text():
    merged = _merged({"auth": "token logged in plaintext"}, "No")

    assert "token logged in plaintext" in merged
    assert "{'" not in merged


@pytest.mark.parametrize("chunks", [
    (["No"], "No"),
    (["No"], ["No"]),
    ("No", "No"),
    ([], ""),
    ([None], None),
])
def test_nothing_reported_merges_to_a_value_the_renderer_suppresses(chunks):
    """`is_value_no` only recognises a string, so the merged value must be one."""
    merged = _merged(*chunks)

    assert is_value_no(merged), f"the review would render a concern reading {merged!r}"


def test_plain_strings_are_unchanged():
    """Control: the shape the prompt asks for keeps its exact behaviour."""
    assert _merged("SQL injection", "No") == "SQL injection"
    assert _merged("No", "No") == "No"


def test_two_different_strings_are_still_unioned():
    merged = _merged("First concern", "Second concern")

    assert merged == "First concern\n\nSecond concern"


@pytest.mark.parametrize("field", ["security_concerns", "insights_from_user_answers"])
def test_both_findings_fields_flatten(field):
    merged = _merged(["A finding"], "No", field=field)

    assert "A finding" in merged
    assert "['" not in merged
