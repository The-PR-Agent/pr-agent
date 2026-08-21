"""Tolerate empty input in the line-number lookup, which six providers share."""
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import find_line_number_of_relevant_line_in_file


def _file(patch):
    return FilePatchInfo(base_file="", head_file="", patch=patch, filename="m.py",
                         edit_type=EDIT_TYPE.MODIFIED)


def test_an_empty_relevant_line_with_an_empty_patch_does_not_raise():
    """Report not-found instead of raising IndexError on an empty relevant line."""
    assert find_line_number_of_relevant_line_in_file([_file("")], "m.py", "") == (-1, -1)


def test_an_empty_relevant_line_with_a_real_patch_does_not_raise():
    """Report not-found for an empty relevant line even when the patch has content."""
    position, absolute = find_line_number_of_relevant_line_in_file(
        [_file("@@ -1,2 +1,2 @@\n ctx\n+added")], "m.py", "")

    assert isinstance(position, int)
    assert isinstance(absolute, int)


def test_a_real_relevant_line_is_still_found():
    """Keep the existing behaviour for a normal lookup."""
    position, absolute = find_line_number_of_relevant_line_in_file(
        [_file("@@ -1,2 +1,2 @@\n ctx\n+added")], "m.py", "+added")

    assert position != -1
    assert absolute != -1


def test_no_diff_files_returns_not_found():
    """Report not-found for an empty file list."""
    assert find_line_number_of_relevant_line_in_file([], "m.py", "+added") == (-1, -1)


def test_an_empty_relevant_line_does_not_match_a_real_patch_line():
    """Report not-found rather than matching the first line, since "" is in every line."""
    patch = "@@ -1,2 +1,2 @@\n ctx\n+added\n"

    assert find_line_number_of_relevant_line_in_file([_file(patch)], "m.py", "") == (-1, -1)
