"""
Tests for malformed @@ hunk headers in decouple_and_convert_to_hunks_with_lines_numbers().

Verifies that:
1. A malformed @@ line does not crash the function.
2. Content from valid hunks before and after a malformed @@ is preserved.
3. When a malformed @@ is the last @@-starting line, the preceding hunk is
   still finalized correctly.
"""

from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.config_loader import get_settings

get_settings(use_context=False).set("CONFIG.CLI_MODE", True)


class _FakeFile:
    """Minimal file object expected by the function under test."""

    def __init__(self, filename="test_file.py"):
        self.filename = filename


class TestMalformedHunkHeader:
    def test_malformed_hunk_does_not_crash(self):
        """A patch whose only @@ line is malformed should not raise."""
        patch = "@@ THIS IS NOT A VALID HUNK HEADER @@\n+added line\n-removed line"
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        assert isinstance(result, str)

    def test_valid_hunk_before_malformed_is_preserved(self):
        """Content from a valid hunk that precedes a malformed @@ must appear in output."""
        patch = (
            "@@ -1,3 +1,4 @@ section\n"
            " context\n"
            "+added_line\n"
            " more_context\n"
            "@@ MALFORMED @@ not a real header\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        # The valid hunk's added line must be present
        assert "+added_line" in result
        assert "__new hunk__" in result

    def test_malformed_hunk_between_two_valid_hunks(self):
        """A malformed @@ between two valid hunks must not drop either hunk's content."""
        patch = (
            "@@ -1,3 +1,4 @@ first_section\n"
            " ctx1\n"
            "+add1\n"
            " ctx2\n"
            "@@ GARBAGE @@ not valid\n"
            "@@ -10,2 +11,3 @@ second_section\n"
            " ctx3\n"
            "+add2\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        assert "+add1" in result, "First hunk content was dropped"
        assert "+add2" in result, "Second hunk content was dropped"

    def test_trailing_malformed_hunk_does_not_drop_last_valid(self):
        """When a malformed @@ is the LAST @@-line, the previous hunk must still be finalized."""
        patch = (
            "@@ -5,3 +5,4 @@ my_func\n"
            " existing_line\n"
            "+new_line\n"
            " another_existing\n"
            "@@ INVALID HEADER\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        # The valid hunk before the malformed trailing @@ must be present
        assert "+new_line" in result
        assert "__new hunk__" in result
        # The malformed header text should NOT appear as a hunk header in the output
        assert "INVALID HEADER" not in result

    def test_line_numbers_correct_with_malformed_between(self):
        """Line numbers from valid hunks are correct even when a malformed @@ sits between them."""
        patch = (
            "@@ -1,2 +1,3 @@ header1\n"
            " line_a\n"
            "+line_b\n"
            "@@ NOT_VALID\n"
            "@@ -10,2 +11,3 @@ header2\n"
            " line_c\n"
            "+line_d\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        # First hunk starts at new-line 1: "1  line_a", "2 +line_b"
        assert "1  line_a" in result
        assert "2 +line_b" in result
        # Second hunk starts at new-line 11: "11  line_c", "12 +line_d"
        assert "11  line_c" in result
        assert "12 +line_d" in result

    def test_only_malformed_hunks_returns_file_header_only(self):
        """A patch with ONLY malformed @@ lines should return just the file header."""
        patch = (
            "@@ BROKEN1 @@\n"
            "+orphan_add\n"
            "@@ BROKEN2 @@\n"
            "-orphan_del\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        # No valid hunk was ever parsed, so no __new hunk__ / __old hunk__ sections
        assert "__new hunk__" not in result
        assert "__old hunk__" not in result

    def test_deletion_only_hunk_before_malformed(self):
        """A hunk with only deletions before a malformed @@ is still finalized."""
        patch = (
            "@@ -1,3 +1,2 @@ section\n"
            " context\n"
            "-removed_line\n"
            "@@ NOT VALID @@\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        assert "-removed_line" in result
        assert "__old hunk__" in result

    def test_orphan_lines_after_malformed_not_joined_to_next_hunk(self):
        """Orphan lines between a malformed @@ and the next valid @@ must be discarded.

        Without clearing the buffers unconditionally on every @@ line, orphan
        lines collected after a malformed @@ (where match=None) leak into the
        next valid hunk because prev_match is None so the flush block is
        skipped and the buffers are never reset.
        """
        patch = (
            "@@ -1,3 +1,4 @@ first_section\n"
            " ctx1\n"
            "+add1\n"
            " ctx2\n"
            "@@ MALFORMED @@ not a real header\n"
            "+orphan_line_should_be_discarded\n"
            "-orphan_del_should_be_discarded\n"
            "@@ -10,2 +11,3 @@ second_section\n"
            " ctx3\n"
            "+add2\n"
        )
        result = decouple_and_convert_to_hunks_with_lines_numbers(patch, _FakeFile())
        # Valid hunk content must be present
        assert "+add1" in result, "First hunk content was dropped"
        assert "+add2" in result, "Second hunk content was dropped"
        # Orphan lines must NOT appear in any hunk
        assert "orphan_line_should_be_discarded" not in result, (
            "Orphan line after malformed @@ leaked into the next hunk"
        )
        assert "orphan_del_should_be_discarded" not in result, (
            "Orphan deletion after malformed @@ leaked into the next hunk"
        )
