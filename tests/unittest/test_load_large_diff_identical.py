from unittest.mock import patch

import pytest

from pr_agent.algo import utils


@pytest.mark.parametrize(
    ("original", "updated"),
    [
        ("", ""),
        (None, ""),
        ("same\n", "same\n"),
        ("same", "same"),
        ("same", "same\n"),
        ("same\n", "same"),
        (" \t\n\n", " \t\n\n"),
        ("same\r\n", "same\r\n"),
    ],
)
def test_identical_content_skips_unified_diff(original, updated):
    with patch.object(utils.difflib, "unified_diff", wraps=utils.difflib.unified_diff) as make_diff:
        assert utils.load_large_diff("file.txt", updated, original) == ""

    make_diff.assert_not_called()


@pytest.mark.parametrize(
    ("original", "updated", "expected"),
    [
        ("old\n", "new\n", "@@ -1 +1 @@\n-old\n+new\n"),
        ("value\n", "value \n", "@@ -1 +1 @@\n-value\n+value \n"),
        ("same\n", "same\r\n", "@@ -1 +1 @@\n-same\n+same\r\n"),
        ("", "new", "@@ -0,0 +1 @@\n+new\n"),
        ("old", "", "@@ -1 +0,0 @@\n-old\n"),
    ],
)
def test_changed_content_preserves_patch(original, updated, expected):
    with patch.object(utils.difflib, "unified_diff", wraps=utils.difflib.unified_diff) as make_diff:
        assert utils.load_large_diff("file.txt", updated, original) == expected

    make_diff.assert_called_once()
