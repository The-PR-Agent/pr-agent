"""A line starting with '@@' that is not a unified hunk header must not crash parsing."""
from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extract_hunk_lines_from_patch)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

COMBINED = "@@@ -1,2 -1,2 +1,2 @@@\n- a\n +b\n  c"
NORMAL = "@@ -1,2 +1,2 @@\n ctx\n-old\n+new"


def _file():
    return FilePatchInfo(base_file="", head_file="", patch="", filename="m.py",
                         edit_type=EDIT_TYPE.MODIFIED)


def test_decouple_skips_a_combined_diff_header_without_raising():
    """A combined/merge diff header must be skipped, not raise AttributeError."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(COMBINED, _file())

    assert "m.py" in out


def test_decouple_still_renders_a_normal_hunk():
    """Valid hunks are unaffected by the guard."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(NORMAL, _file())

    assert "__new hunk__" in out
    assert "+new" in out


def test_a_valid_hunk_after_an_invalid_header_is_still_rendered():
    """Skipping one bad header must not discard the rest of the patch."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(COMBINED + "\n" + NORMAL, _file())

    assert "+new" in out


def test_extract_hunk_lines_skips_a_combined_diff_header():
    """The selection helper must degrade rather than swallow an AttributeError."""
    full, selected = extract_hunk_lines_from_patch(COMBINED, "m.py", 1, 1, "right")

    assert "m.py" in full
    assert selected == ""


def test_extract_hunk_lines_still_selects_from_a_normal_hunk():
    """Valid hunks are unaffected by the guard."""
    _, selected = extract_hunk_lines_from_patch(NORMAL, "m.py", 2, 2, "right")

    assert "+new" in selected
