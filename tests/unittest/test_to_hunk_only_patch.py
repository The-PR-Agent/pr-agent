from pr_agent.algo.git_patch_processing import to_hunk_only_patch


def test_to_hunk_only_patch_strips_file_metadata():
    hunk = "@@ -1 +1 @@\n-old\n+new\n"
    patch = "--- a/file.py\n+++ b/file.py\n" + hunk

    assert to_hunk_only_patch(patch) == hunk


def test_to_hunk_only_patch_keeps_hunk_only_input_unchanged():
    patch = "@@ -1 +1 @@\r\n-old\r\n+new\r\n"

    assert to_hunk_only_patch(patch) == patch


def test_to_hunk_only_patch_ignores_inline_at_markers():
    patch = "--- a/file.py\n+++ b/file.py\n context @@ marker\n"

    assert to_hunk_only_patch(patch) == ""


def test_to_hunk_only_patch_preserves_splitlines_boundaries():
    separators = ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    for separator in separators:
        hunk = f"@@ -1 +1 @@{separator}+new{separator}"
        patch = f"metadata{separator}{hunk}"

        assert to_hunk_only_patch(patch) == hunk
