import pytest

from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extract_hunk_lines_from_patch,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("section", ["", " # no newline at end of file"])
def test_eof_marker_text_in_source_preserves_content_and_line_ranges(newline, section):
    header = f"@@ -10,3 +20,3 @@{section}"
    context = " # No newline at end of file"
    removed = '-old("no newline at end of file")'
    added = '+new("NO NEWLINE AT END OF FILE")'
    patch = newline.join([header, context, removed, added, " tail()"])

    rendered = decouple_and_convert_to_hunks_with_lines_numbers(patch, None)

    assert rendered == (
        f"\n{header}\n__new hunk__\n20 {context}\n21 {added}\n22  tail()"
        f"\n__old hunk__\n{context}\n{removed}\n tail()"
    )
    for side, start, changed in [("left", 10, removed), ("right", 20, f"{removed}\n{added}")]:
        for offset, expected in enumerate([context, changed, " tail()"]):
            full_patch, selected = extract_hunk_lines_from_patch(
                patch, "example.py", start + offset, start + offset, side,
            )
            assert header in full_patch
            assert context in full_patch
            assert removed in full_patch
            assert added in full_patch
            assert selected == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_actual_eof_markers_are_skipped_but_prefixed_source_is_preserved(newline):
    marker = r"\ No newline at end of file"
    removed, added = f"-{marker}", f"+{marker}"
    header = "@@ -1 +1 @@"
    patch = newline.join([header, removed, marker, added, marker])

    rendered = decouple_and_convert_to_hunks_with_lines_numbers(patch, None)

    assert rendered == f"\n{header}\n__new hunk__\n1 {added}\n__old hunk__\n{removed}"
    for side, expected in [("left", removed), ("right", f"{removed}\n{added}")]:
        full_patch, selected = extract_hunk_lines_from_patch(patch, "example.txt", 1, 1, side)
        assert full_patch.splitlines()[-2:] == [removed, added]
        assert selected == expected
