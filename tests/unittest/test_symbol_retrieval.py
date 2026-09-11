"""R-16 v1: grep/identifier-based cross-file symbol retrieval for Dart."""

from pathlib import Path

from pr_agent.algo.symbol_retrieval import (
    SymbolIndex,
    build_repo_symbol_index,
    extract_changed_identifiers,
    retrieve_context_for_files,
)


def _write(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_extract_changed_identifiers_picks_declarations_and_calls():
    patch = """\
--- a/lib/foo.dart
+++ b/lib/foo.dart
@@ -1,6 +1,6 @@
 class Foo {
-  void oldMethod() {
+  void newMethod() {
-    otherCaller();
+    otherCaller();
-    // comment only
+    // still a comment
     if (x) return;
   }
}
"""
    ids = extract_changed_identifiers(patch)
    assert "newMethod" in ids
    assert "otherCaller" in ids
    assert "void" not in ids
    assert "if" not in ids
    assert "x" not in ids
    assert "class" not in ids


def test_index_skips_generated_and_build_paths(tmp_path):
    _write(tmp_path, "lib/main.dart", "class Main {}\n")
    _write(tmp_path, "lib/main.g.dart", "class Generated {}\n")
    _write(tmp_path, "lib/model.freezed.dart", "class Freezed {}\n")
    _write(tmp_path, "build/output.dart", "class BuildOut {}\n")

    index = build_repo_symbol_index(str(tmp_path))
    paths = {path for entries in index.definitions.values() for path, _ in entries}
    assert "lib/main.dart" in paths
    assert "lib/main.g.dart" not in paths
    assert "lib/model.freezed.dart" not in paths
    assert "build/output.dart" not in paths


def test_definition_in_untouched_file_is_retrieved(tmp_path):
    _write(tmp_path, "lib/types.dart", "class UserService {\n  UserService();\n}\n")
    _write(tmp_path, "lib/changed.dart", "void useIt() {\n  final s = UserService();\n}\n")
    index = build_repo_symbol_index(str(tmp_path))

    patch = """\
--- a/lib/changed.dart
+++ b/lib/changed.dart
@@ -1,3 +1,3 @@
 void useIt() {
-  final s = UserService();
+  final svc = UserService();
 }
"""
    rendered = retrieve_context_for_files(
        index,
        {"lib/changed.dart": patch},
        max_chars=8000,
    )
    assert "lib/types.dart" in rendered
    assert "UserService" in rendered
    assert "<retrieved_context>" in rendered
    assert rendered.strip().endswith("</retrieved_context>")


def test_references_are_retrieved_up_to_max_files_per_symbol(tmp_path):
    callers = []
    for i in range(5):
        rel = f"lib/caller_{i}.dart"
        callers.append(rel)
        _write(tmp_path, rel, f"void call{i}() {{\n  targetFn();\n}}\n")
    _write(tmp_path, "lib/target.dart", "void targetFn() {}\n")
    _write(tmp_path, "lib/changed.dart", "void edit() {\n  targetFn();\n}\n")
    index = build_repo_symbol_index(str(tmp_path))

    patch = """\
--- a/lib/changed.dart
+++ b/lib/changed.dart
@@ -1,3 +1,3 @@
 void edit() {
-  targetFn();
+  targetFn(); // touch
 }
"""
    rendered = retrieve_context_for_files(
        index,
        {"lib/changed.dart": patch},
        max_chars=20000,
        max_files_per_symbol=2,
    )
    referenced_callers = [p for p in callers if p in rendered]
    assert len(referenced_callers) == 2
    assert "lib/target.dart" not in rendered or "void targetFn" not in rendered.split("lib/target.dart")[0]


def test_definition_inside_changed_files_is_not_retrieved_again(tmp_path):
    _write(
        tmp_path,
        "lib/changed.dart",
        "class LocalType {\n  void localMethod() {}\n}\n",
    )
    index = build_repo_symbol_index(str(tmp_path))

    patch = """\
--- a/lib/changed.dart
+++ b/lib/changed.dart
@@ -1,3 +1,3 @@
 class LocalType {
-  void localMethod() {}
+  void localMethodRenamed() {}
 }
"""
    rendered = retrieve_context_for_files(
        index,
        {"lib/changed.dart": patch},
        max_chars=8000,
    )
    assert rendered == ""


def test_max_chars_is_respected_and_truncation_note_counts_dropped_snippets(tmp_path):
    for i in range(8):
        _write(tmp_path, f"lib/ref_{i}.dart", f"void helper{i}() {{\n  sharedSymbol();\n  // padding line for snippet size\n  // padding line for snippet size\n}}\n")
    _write(tmp_path, "lib/changed.dart", "void edit() {\n  sharedSymbol();\n}\n")
    index = build_repo_symbol_index(str(tmp_path))

    patch = """\
--- a/lib/changed.dart
+++ b/lib/changed.dart
@@ -1,3 +1,3 @@
 void edit() {
-  sharedSymbol();
+  sharedSymbol(); // edited
 }
"""
    rendered = retrieve_context_for_files(
        index,
        {"lib/changed.dart": patch},
        max_chars=500,
        max_files_per_symbol=8,
    )
    assert len(rendered) <= 500
    assert "snippet(s) omitted" in rendered
    assert rendered.strip().endswith("</retrieved_context>")
    for part in rendered.split("\n\n"):
        if part.startswith("lib/"):
            assert part.count("\n") >= 1


def test_literal_close_tag_in_snippet_cannot_end_block_early(tmp_path):
    _write(
        tmp_path,
        "lib/tricky.dart",
        "void tricky() {\n  final s = '</retrieved_context>';\n}\n",
    )
    _write(tmp_path, "lib/changed.dart", "void edit() {\n  tricky();\n}\n")
    index = build_repo_symbol_index(str(tmp_path))

    patch = """\
--- a/lib/changed.dart
+++ b/lib/changed.dart
@@ -1,3 +1,3 @@
 void edit() {
-  tricky();
+  tricky(); // touch
 }
"""
    rendered = retrieve_context_for_files(
        index,
        {"lib/changed.dart": patch},
        max_chars=8000,
    )
    assert rendered.count("</retrieved_context>") == 1
    assert "&lt;/retrieved_context&gt;" in rendered
    assert rendered.strip().endswith("</retrieved_context>")


def test_empty_index_or_no_changed_identifiers_renders_empty(tmp_path):
    _write(tmp_path, "lib/only.dart", "class Only {}\n")
    index = build_repo_symbol_index(str(tmp_path))
    empty_index = SymbolIndex(repo_root=str(tmp_path), definitions={}, references={})

    comment_only_patch = """\
--- a/lib/only.dart
+++ b/lib/only.dart
@@ -1,1 +1,1 @@
-// old comment
+// new comment
"""
    assert retrieve_context_for_files(index, {"lib/only.dart": comment_only_patch}, max_chars=1000) == ""
    assert retrieve_context_for_files(empty_index, {"lib/only.dart": "// x\n"}, max_chars=1000) == ""


def test_root_level_paths_carry_no_dot_prefix_and_match_diff_paths(tmp_path):
    """A root-level file must index as "a.dart", not "./a.dart".

    Diff paths have no "./" prefix, so a mismatch here makes `changed_files` membership fail and the
    retriever hands the model back the very lines the diff already contains.
    """
    (tmp_path / "target.dart").write_text("class AdsService {\n  void show() {}\n}\n")
    (tmp_path / "caller.dart").write_text("var a = AdsService();\n")
    index = build_repo_symbol_index(str(tmp_path))

    assert all(not path.startswith("./") for entries in index.definitions.values() for path, _ in entries)

    # target.dart is part of the diff, so its own definition must not be retrieved back.
    rendered = retrieve_context_for_files(
        index,
        {"target.dart": "@@\n+class AdsService {\n"},
        max_chars=10000,
    )
    assert "caller.dart" in rendered
    assert "target.dart:" not in rendered
