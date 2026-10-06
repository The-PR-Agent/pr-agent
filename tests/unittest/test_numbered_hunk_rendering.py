import pytest

from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.algo.types import FilePatchInfo


@pytest.mark.parametrize("with_file", [False, True])
def test_multi_hunk_rendering_preserves_spacing_and_line_numbers(with_file):
    patch = (
        "@@ -1,2 +1,2 @@ first\n"
        "-old  \n"
        "+new  \n"
        " \n"
        "@@ -10 +10,0 @@ deletion\n"
        "-removed\t\n"
        "@@ -20,0 +20 @@ addition\n"
        "+added  \n"
    )
    file = FilePatchInfo("", "", patch, " example.py ") if with_file else None
    expected = (
        "\n@@ -1,2 +1,2 @@ first\n"
        "__new hunk__\n"
        "1 +new  \n"
        "2  \n"
        "__old hunk__\n"
        "-old  \n"
        " \n"
        "\n@@ -10 +10,0 @@ deletion\n"
        "__new hunk__\n"
        "__old hunk__\n"
        "-removed\t\n"
        "\n@@ -20,0 +20 @@ addition\n"
        "__new hunk__\n"
        "20 +added  "
    )
    if with_file:
        expected = "\n\n## File: 'example.py'\n" + expected

    assert decouple_and_convert_to_hunks_with_lines_numbers(patch, file) == expected


def test_context_only_hunk_between_changes_preserves_separators():
    patch = (
        "@@ -1 +1 @@\n-a\n+b\n"
        "@@ -10 +10 @@ context only  \n unchanged\n"
        "@@ -20 +20 @@\n-c\n+d\n"
    )
    expected = (
        "\n@@ -1 +1 @@\n__new hunk__\n1 +b\n__old hunk__\n-a\n"
        "\n@@ -10 +10 @@ context only  \n"
        "\n@@ -20 +20 @@\n__new hunk__\n20 +d\n__old hunk__\n-c"
    )

    assert decouple_and_convert_to_hunks_with_lines_numbers(patch, None) == expected


@pytest.mark.parametrize("hunk_count", [1, 128])
def test_many_hunks_preserve_complete_output(hunk_count):
    patch_parts = []
    expected_parts = []
    for i in range(hunk_count):
        start = i * 10 + 1
        header = f"@@ -{start} +{start} @@ section_{i}"
        patch_parts.append(f"{header}\n-old_{i}\n+new_{i}\n")
        expected_parts.append(
            f"\n{header}\n__new hunk__\n{start} +new_{i}\n__old hunk__\n-old_{i}\n"
        )

    assert decouple_and_convert_to_hunks_with_lines_numbers("".join(patch_parts), None) == (
        "".join(expected_parts).rstrip()
    )


def test_hunkless_patch_preserves_mode_metadata():
    patch = (
        "diff --git a/modestuff.txt b/modestuff.txt\n"
        "old mode 100644\n"
        "new mode 100755"
    )
    file = FilePatchInfo("", "", patch, "modestuff.txt")
    assert decouple_and_convert_to_hunks_with_lines_numbers(patch, file) == (
        "\n\n## File: 'modestuff.txt'\n"
        "diff --git a/modestuff.txt b/modestuff.txt\n"
        "old mode 100644\n"
        "new mode 100755"
    )


def test_hunkless_patch_preserves_binary_metadata():
    patch = (
        "diff --git a/img.png b/img.png\n"
        "index 0000000..abcdef 100644\n"
        "Binary files /dev/null and b/img.png differ"
    )
    file = FilePatchInfo("", "", patch, "img.png")
    result = decouple_and_convert_to_hunks_with_lines_numbers(patch, file)
    assert "Binary files /dev/null and b/img.png differ" in result
    assert "index 0000000..abcdef 100644" in result
    assert "__new hunk__" not in result


def test_hunkless_metadata_ignored_when_a_hunk_exists():
    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "index 7898192..422c2b7 100644\n"
        "@@ -1 +1,2 @@\n"
        " a\n"
        "+b"
    )
    file = FilePatchInfo("", "", patch, "src/app.py")
    result = decouple_and_convert_to_hunks_with_lines_numbers(patch, file)
    assert "__new hunk__" in result
    assert "old mode 100644" not in result
    assert "new mode 100755" not in result
