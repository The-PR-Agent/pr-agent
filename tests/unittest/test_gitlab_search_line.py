"""A relevant_file missing from the diff must report "not found" instead of raising UnboundLocalError."""
import re
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers.gitlab_provider import GitLabProvider

PATCH = "@@ -1,2 +1,2 @@\n line1\n+added\n-line2\n"
DIFF_FILES = [
    FilePatchInfo(base_file="a.py", head_file="a.py", patch=PATCH,
                  filename="a.py", edit_type=EDIT_TYPE.MODIFIED),
]


def _provider(diff_files):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
    provider.get_diff_files = lambda: list(diff_files)
    provider.max_comment_chars = 65000
    return provider


def test_search_line_reports_not_found_for_file_absent_from_diff():
    provider = _provider(DIFF_FILES)

    edit_type, found, source_line_no, target_file, target_line_no = provider.search_line("missing.py", "+added")

    assert found is False
    assert (source_line_no, target_line_no, target_file) == (0, 0, None)
    assert edit_type == "addition"


def test_search_line_reports_not_found_for_empty_diff():
    provider = _provider([])

    assert provider.search_line("a.py", "+added")[1] is False


def test_search_line_still_resolves_a_present_file():
    provider = _provider(DIFF_FILES)

    edit_type, found, source_line_no, target_file, target_line_no = provider.search_line("a.py", "+added")

    assert found is True
    assert target_file.filename == "a.py"
    assert (edit_type, source_line_no, target_line_no) == ("addition", 2, 3)


def test_search_line_uses_the_first_matching_file():
    duplicate = FilePatchInfo(base_file="a.py", head_file="a.py", patch=PATCH,
                              filename="a.py", edit_type=EDIT_TYPE.MODIFIED)
    provider = _provider(DIFF_FILES + [duplicate])
    provider.find_in_file = MagicMock(return_value=("addition", True, 1, DIFF_FILES[0], 1))

    assert provider.search_line("a.py", "+added")[3] is DIFF_FILES[0]
    provider.find_in_file.assert_called_once_with(DIFF_FILES[0], "+added")


def test_publish_inline_comment_skips_absent_file_instead_of_raising():
    provider = _provider(DIFF_FILES)
    sent = []
    provider.send_inline_comment = lambda *args: sent.append(args) or False

    provider.publish_inline_comment("body", "missing.py", "+added")

    # send_inline_comment still runs, and bails out because found is False
    assert len(sent) == 1
    assert sent[0][2] is False


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\f", "\v", "\x1c", "\r"])
def test_search_line_keeps_non_lf_characters_in_the_source_line(separator):
    patch = f"@@ -1 +1 @@\n-old{separator}continued\n+new{separator}continued\n"
    file = FilePatchInfo("", "", patch, "a.py")
    result = _provider([file]).search_line("a.py", f"+new{separator}continued")

    assert result[1] is True
    assert (result[0], result[2], result[4]) == ("addition", 2, 2)


@pytest.mark.parametrize("header", ["@@ malformed @@", "@@@ -1,1 -1,1 +1,1 @@@"])
def test_unsupported_hunk_body_does_not_reuse_previous_line_cursors(header):
    file = FilePatchInfo("", "", f"@@ -1 +1 @@\n context\n{header}\n+unsupported\n", "a.py")

    assert _provider([file]).search_line("a.py", "+unsupported")[1] is False


def test_search_line_resumes_at_a_valid_hunk_after_an_unsupported_one():
    patch = "@@ malformed @@\n+unsupported\n@@ -10 +20 @@\n-old\n+new\n"
    result = _provider([FilePatchInfo("", "", patch, "a.py")]).search_line("a.py", "+new")

    assert result[1] is True
    assert (result[2], result[4]) == (11, 21)


@pytest.mark.parametrize("needle, patch", [
    ("b/a.py", "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"),
    ("No newline", "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"),
])
def test_diff_metadata_cannot_be_selected_as_source(needle, patch):
    provider = _provider([FilePatchInfo("", "", patch, "a.py")])

    assert provider.search_line("a.py", needle)[1] is False


@pytest.mark.parametrize("source", ["", None, False, 7])
def test_missing_source_line_reports_not_found_instead_of_selecting_or_crashing(source):
    provider = _provider(DIFF_FILES)

    assert provider.search_line("a.py", source)[1] is False
    assert provider.find_in_file(DIFF_FILES[0], source)[1] is False


@pytest.mark.parametrize("source, patch, expected_cursor", [
    ("call()", "@@ -1,2 +1,2 @@\n other_call()\n call()\n", 3),
    ("+new", "@@ -0,0 +1,2 @@\n+new_suffix\n+new\n", 3),
    ("+", "@@ -0,0 +1,2 @@\n+nonblank\n+\n", 3),
])
def test_exact_source_match_takes_priority_over_an_earlier_substring(source, patch, expected_cursor):
    result = _provider([FilePatchInfo("", "", patch, "a.py")]).search_line("a.py", source)

    assert result[1] is True
    assert result[4] == expected_cursor
