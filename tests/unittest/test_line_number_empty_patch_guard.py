"""The line-number lookup is shared by six providers and must tolerate empty input."""
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import find_line_number_of_relevant_line_in_file


def _file(patch):
    return FilePatchInfo(base_file="", head_file="", patch=patch, filename="m.py",
                         edit_type=EDIT_TYPE.MODIFIED)


def test_an_empty_relevant_line_with_an_empty_patch_does_not_raise():
    """Indexing relevant_line_in_file[0] used to raise IndexError here."""
    assert find_line_number_of_relevant_line_in_file([_file("")], "m.py", "") == (-1, -1)


def test_an_empty_relevant_line_with_a_real_patch_does_not_raise():
    """The same guard must hold when the patch has content."""
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
    """An empty file list still reports 'not found'."""
    assert find_line_number_of_relevant_line_in_file([], "m.py", "+added") == (-1, -1)
