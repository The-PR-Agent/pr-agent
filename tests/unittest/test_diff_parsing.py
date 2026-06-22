from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.diff_parsing import parse_unified_diff

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
