"""Anchor an inline comment on the line the model actually named."""
import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import find_line_number_of_relevant_line_in_file


def build(head_lines):
    head = "\n".join(head_lines)
    patch = f"@@ -0,0 +1,{len(head_lines)} @@\n" + "\n".join("+" + line for line in head_lines)
    return [FilePatchInfo(base_file="", head_file=head, patch=patch, filename="a.py",
                          edit_type=EDIT_TYPE.MODIFIED)]


def test_prefer_the_exact_line_over_an_earlier_line_that_contains_it():
    """An earlier line that merely contains the text must not win over an exact match."""
    lines = ["        raise HTTPException(status_code=500, detail='x')",
             "        pass",
             "        raise HTTPException("]

    _, absolute = find_line_number_of_relevant_line_in_file(build(lines), "a.py",
                                                           "        raise HTTPException(")

    assert absolute == 3


def test_prefer_the_first_exact_line_when_the_text_repeats():
    """With several exact matches, keep the existing first-match behaviour."""
    lines = ["    assert calls", "    x = 1", "    assert calls"]

    _, absolute = find_line_number_of_relevant_line_in_file(build(lines), "a.py", "    assert calls")

    assert absolute == 1


def test_fall_back_to_a_containing_line_when_no_exact_match_exists():
    """Keep the substring fallback, which is what makes a truncated model line usable."""
    lines = ["total = calculate(x, y)", "pass"]

    _, absolute = find_line_number_of_relevant_line_in_file(build(lines), "a.py", "calculate(x")

    assert absolute == 1


def test_ignore_an_exact_match_on_a_deleted_line():
    """A '-' line is not in the post-image, so it must not be anchored to."""
    head = "kept = 1"
    patch = "@@ -1,2 +1,1 @@\n-removed = 0\n kept = 1"
    files = [FilePatchInfo(base_file="removed = 0\nkept = 1", head_file=head, patch=patch,
                           filename="a.py", edit_type=EDIT_TYPE.MODIFIED)]

    position, _ = find_line_number_of_relevant_line_in_file(files, "a.py", "removed = 0")

    assert position == -1


@pytest.mark.parametrize("relevant_line", ["not in the file at all"])
def test_report_not_found_for_a_line_the_patch_does_not_contain(relevant_line):
    """Keep reporting 'not found' rather than guessing."""
    result = find_line_number_of_relevant_line_in_file(build(["a = 1"]), "a.py", relevant_line)

    assert result == (-1, -1)
