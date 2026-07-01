from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.diff_parsing import parse_unified_diff, reconstruct_base_file

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


def test_parse_add():
    f = parse_unified_diff(ADD_DIFF)[0]
    assert f.filename == "new.py"
    assert f.edit_type == EDIT_TYPE.ADDED


def test_parse_delete():
    f = parse_unified_diff(DELETE_DIFF)[0]
    assert f.filename == "gone.py"
    assert f.edit_type == EDIT_TYPE.DELETED


def test_parse_rename():
    f = parse_unified_diff(RENAME_DIFF)[0]
    assert f.filename == "renamed.py"
    assert f.edit_type == EDIT_TYPE.RENAMED
    assert f.old_filename == "old.py"


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
    assert reconstruct_base_file(HEAD, _PATCH) == BASE


def test_reconstruct_base_drift_returns_empty():
    drifted_head = "completely\ndifferent\ncontent\n"
    assert reconstruct_base_file(drifted_head, _PATCH) == ""


# --- new tests for reconstruct_base_file ---

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
    result = reconstruct_base_file(_MULTI_HUNK_HEAD, _MULTI_HUNK_PATCH)
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
    result = reconstruct_base_file(_NO_TRAILING_NL_HEAD, _NO_TRAILING_NL_PATCH)
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
    result = reconstruct_base_file(_ADD_FILE_HEAD, _ADD_FILE_PATCH)
    # Even though head ends with "\n", an empty base must stay "" (no trailing
    # newline appended) so it is falsy for extend_patch().
    assert result == ""
