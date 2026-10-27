import pytest

from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers, handle_patch_deletions
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


@pytest.mark.parametrize("separator", ["\u2028", "\f", "\x85"])
def test_line_separators_inside_a_line_do_not_shift_line_numbers(separator):
    patch = (
        "@@ -1,2 +1,2 @@\n"
        f" const sep = 'x{separator}y';\n"
        "-const b = 2;\n"
        "+const b = 3;\n"
    )
    expected = (
        "\n@@ -1,2 +1,2 @@\n"
        "__new hunk__\n"
        f"1  const sep = 'x{separator}y';\n"
        "2 +const b = 3;\n"
        "__old hunk__\n"
        f" const sep = 'x{separator}y';\n"
        "-const b = 2;"
    )

    assert decouple_and_convert_to_hunks_with_lines_numbers(patch, None) == expected


@pytest.mark.parametrize("separator", [" ", "\f", "\x85"])
def test_line_separators_survive_deletion_handling(separator):
    patch = (
        "@@ -1,2 +1,2 @@\n"
        f" const sep = 'x{separator}y';\n"
        "-const b = 2;\n"
        "+const b = 3;\n"
    )
    handled = handle_patch_deletions(patch, "old", "new", "a.js")

    assert "2 +const b = 3;" in decouple_and_convert_to_hunks_with_lines_numbers(handled, None)
