"""Parse a patch without crashing on a '@@' line that is not a unified hunk header."""
from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extract_hunk_lines_from_patch,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

COMBINED = "@@@ -1,2 -1,2 +1,2 @@@\n- a\n +b\n  c"
NORMAL = "@@ -1,2 +1,2 @@\n ctx\n-old\n+new"


def _file():
    return FilePatchInfo(base_file="", head_file="", patch="", filename="m.py",
                         edit_type=EDIT_TYPE.MODIFIED)


def test_decouple_skips_a_combined_diff_header_without_raising():
    """Skip a combined/merge diff header rather than raising AttributeError."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(COMBINED, _file())

    assert "m.py" in out


def test_decouple_still_renders_a_normal_hunk():
    """Keep valid hunks unaffected by the guard."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(NORMAL, _file())

    assert "__new hunk__" in out
    assert "+new" in out


def test_a_valid_hunk_after_an_invalid_header_is_still_rendered():
    """Keep the rest of the patch when one bad header is skipped."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(COMBINED + "\n" + NORMAL, _file())

    assert "+new" in out


def test_extract_hunk_lines_skips_a_combined_diff_header():
    """Degrade in the selection helper rather than swallowing an AttributeError."""
    full, selected = extract_hunk_lines_from_patch(COMBINED, "m.py", 1, 1, "right")

    assert "m.py" in full
    assert selected == ""


def test_extract_hunk_lines_still_selects_from_a_normal_hunk():
    """Keep valid hunks unaffected by the guard."""
    _, selected = extract_hunk_lines_from_patch(NORMAL, "m.py", 2, 2, "right")

    assert "+new" in selected


def test_a_skipped_hunk_does_not_leak_into_the_next_one():
    """Drop the body of a skipped hunk, so it cannot be flushed with a negative start line."""
    out = decouple_and_convert_to_hunks_with_lines_numbers(COMBINED + "\n" + NORMAL, _file())

    assert "+b" not in out
    assert "-1 " not in out
    assert "+new" in out
