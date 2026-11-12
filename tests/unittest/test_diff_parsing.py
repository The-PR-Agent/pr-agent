import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.diff_parsing import parse_unified_diff, reconstruct_base_file_checked

MODIFY_DIFF = """diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
"""

ADD_DIFF = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+hello
+world
"""

DELETE_DIFF = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index 4444444..0000000
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
"""

RENAME_DIFF = """diff --git a/old.py b/renamed.py
similarity index 100%
rename from old.py
rename to renamed.py
"""


def test_parse_modify():
    files = parse_unified_diff(MODIFY_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.filename == "foo.py"
    assert f.edit_type == EDIT_TYPE.MODIFIED
    assert f.old_filename is None
    assert "line2-changed" in f.patch
    assert (f.num_plus_lines, f.num_minus_lines) == (1, 1)


def test_parse_add():
    f = parse_unified_diff(ADD_DIFF)[0]
    assert f.filename == "new.py"
    assert f.edit_type == EDIT_TYPE.ADDED
    assert (f.num_plus_lines, f.num_minus_lines) == (2, 0)


def test_parse_delete():
    f = parse_unified_diff(DELETE_DIFF)[0]
    assert f.filename == "gone.py"
    assert f.edit_type == EDIT_TYPE.DELETED
    assert (f.num_plus_lines, f.num_minus_lines) == (0, 1)


def test_parse_rename():
    f = parse_unified_diff(RENAME_DIFF)[0]
    assert f.filename == "renamed.py"
    assert f.edit_type == EDIT_TYPE.RENAMED
    assert f.old_filename == "old.py"
    assert (f.num_plus_lines, f.num_minus_lines) == (0, 0)


_PATCH = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
"""

HEAD = "line1\nline2-changed\nline3\n"
BASE = "line1\nline2\nline3\n"


def test_reconstruct_base_success():
    assert reconstruct_base_file_checked(HEAD, _PATCH) == BASE


def test_reconstruct_base_drift_returns_none():
    drifted_head = "completely\ndifferent\ncontent\n"
    assert reconstruct_base_file_checked(drifted_head, _PATCH) is None


# --- new tests for reconstruct_base_file_checked ---

_MULTI_HUNK_PATCH = """--- a/bar.py
+++ b/bar.py
@@ -1,3 +1,3 @@
 alpha
-beta
+beta-new
 gamma
@@ -5,3 +5,3 @@
 delta
-epsilon
+epsilon-new
 zeta
"""

_MULTI_HUNK_HEAD = "alpha\nbeta-new\ngamma\n\ndelta\nepsilon-new\nzeta\n"
_MULTI_HUNK_BASE = "alpha\nbeta\ngamma\n\ndelta\nepsilon\nzeta\n"


def test_reconstruct_base_multi_hunk():
    """Base is correctly reconstructed across two hunks."""
    result = reconstruct_base_file_checked(_MULTI_HUNK_HEAD, _MULTI_HUNK_PATCH)
    assert result == _MULTI_HUNK_BASE


_NO_TRAILING_NL_PATCH = """--- a/noeol.py
+++ b/noeol.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
"""

_NO_TRAILING_NL_HEAD = "line1\nline2-changed\nline3"  # no trailing newline
_NO_TRAILING_NL_BASE = "line1\nline2\nline3"  # no trailing newline expected


def test_reconstruct_base_no_trailing_newline():
    """Result has no trailing newline when head has none."""
    result = reconstruct_base_file_checked(_NO_TRAILING_NL_HEAD, _NO_TRAILING_NL_PATCH)
    assert result == _NO_TRAILING_NL_BASE
    assert not result.endswith("\n")


# A diff that added a file (base was empty); reversed: head is the added content,
# base must be exactly "" — never "\n" — so downstream extend_patch() correctly
# treats the original file as non-existent.
_ADD_FILE_PATCH = """--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+hello
+world
"""

_ADD_FILE_HEAD = "hello\nworld\n"


def test_reconstruct_base_add_to_empty():
    """Reversing an add-file patch yields a truly empty base, not a lone newline."""
    result = reconstruct_base_file_checked(_ADD_FILE_HEAD, _ADD_FILE_PATCH)
    # Even though head ends with "\n", an empty base must stay "" (no trailing
    # newline appended) so it is falsy for extend_patch().
    assert result == ""


# --- hunks with no head lines (target length 0) ---

_ZERO_CONTEXT_DELETE_PATCH = """--- a/f.txt
+++ b/f.txt
@@ -3,2 +2,0 @@ b
-c
-d
"""


def test_reconstruct_base_zero_context_deletion():
    # `git diff -U0` numbers a pure deletion by the head line before it (`+2,0`),
    # so the removed lines belong after head line 2, not before it.
    assert reconstruct_base_file_checked("a\nb\ne\n", _ZERO_CONTEXT_DELETE_PATCH) == "a\nb\nc\nd\ne\n"


# Verbatim `git diff -U0` output for the base/head pair in the test below.
_ZERO_CONTEXT_MIXED_PATCH = """--- a/f.txt
+++ b/f.txt
@@ -1,0 +2 @@ a
+x
@@ -3 +3,0 @@ b
-c
@@ -5,0 +6,2 @@ e
+y
+z
"""


def test_reconstruct_base_zero_context_mixed_hunks():
    base = "a\nb\nc\nd\ne\n"
    head = "a\nx\nb\nd\ne\ny\nz\n"
    assert reconstruct_base_file_checked(head, _ZERO_CONTEXT_MIXED_PATCH) == base


_EMPTIED_FILE_PATCH = """--- a/f.txt
+++ b/f.txt
@@ -1,2 +0,0 @@
-a
-b
"""


def test_reconstruct_base_file_emptied():
    # Every line removed but the file kept: `+0,0` must not be read as out of bounds.
    # The removed lines are not marked with "\ No newline at end of file", so the
    # base still ended with a newline even though the empty head does not.
    assert reconstruct_base_file_checked("", _EMPTIED_FILE_PATCH) == "a\nb\n"


def test_reconstruct_base_single_line_file_emptied():
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +0,0 @@\n-hello\n"
    assert reconstruct_base_file_checked("", patch) == "hello\n"


def test_reconstruct_base_newline_only_file_emptied():
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +0,0 @@\n-\n"
    assert reconstruct_base_file_checked("", patch) == "\n"


def test_reconstruct_base_file_emptied_without_a_trailing_newline():
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +0,0 @@\n"
        "-hello\n\\ No newline at end of file\n"
    )
    assert reconstruct_base_file_checked("", patch) == "hello"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\f", "\v", "\x1c", "\r"])
def test_reverse_patch_preserves_non_lf_characters_inside_physical_lines(separator):
    head = f"first{separator}continued\nnew\nlast\n"
    patch = ("--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n"
             f" first{separator}continued\n-old\n+new\n last\n")

    assert reconstruct_base_file_checked(head, patch) == f"first{separator}continued\nold\nlast\n"


def test_reverse_patch_preserves_a_non_lf_separator_outside_the_hunk():
    head = "first\u2028continued\nunchanged\nnew\n"
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -3 +3 @@\n-old\n+new\n"

    assert reconstruct_base_file_checked(head, patch) == "first\u2028continued\nunchanged\nold\n"


def test_reverse_patch_does_not_strip_multiple_carriage_returns_from_line_content():
    head = "first\r\r\nnew\n"
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n first\r\r\n-old\n+new\n"

    assert reconstruct_base_file_checked(head, patch) == "first\r\nold\n"


def test_reconstruct_base_without_trailing_newline_when_the_patch_adds_one():
    # Git marks the side that lacks the final newline with "\ No newline at end of file";
    # here the base lacked it, so the base must not take the head's trailing newline.
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n"
        " a\n-b\n\\ No newline at end of file\n+b\n"
    )

    assert reconstruct_base_file_checked("a\nb\n", patch) == "a\nb"


def test_reconstruct_base_with_trailing_newline_when_the_patch_removes_it():
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n"
        " a\n-b\n+b\n\\ No newline at end of file\n"
    )

    assert reconstruct_base_file_checked("a\nb", patch) == "a\nb\n"


def test_reconstruct_base_keeps_a_missing_newline_shared_by_both_sides():
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n"
        "-a\n+A\n b\n\\ No newline at end of file\n"
    )

    assert reconstruct_base_file_checked("A\nb", patch) == "a\nb"


def test_reconstruct_base_when_neither_side_ends_with_a_newline():
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n"
        " a\n-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n"
    )

    assert reconstruct_base_file_checked("a\nB", patch) == "a\nb"
