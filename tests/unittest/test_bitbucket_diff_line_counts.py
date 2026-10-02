"""Regression tests: Bitbucket providers must report added/removed line counts.

``FilePatchInfo.num_plus_lines`` / ``num_minus_lines`` default to ``-1`` when a
provider leaves them unset. ``pr_agent/tools/pr_description.py`` renders them
unconditionally as ``f"+{num_plus_lines}/-{num_minus_lines}"`` and only replaces
the result with ``[link]`` when it is longer than 12 characters or exactly
``+0/-0``, so the unset sentinel reached user-visible PR descriptions verbatim
as ``+-1/--1`` for every Bitbucket Cloud and Bitbucket Server file.
"""

from unittest.mock import MagicMock, patch

import pytest
from atlassian.bitbucket import Bitbucket

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers import BitbucketServerProvider
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider


def _format_delta(num_plus_lines: int, num_minus_lines: int) -> str:
    """Mirror the formatting branch in ``pr_agent.tools.pr_description``."""
    diff_plus_minus = f"+{num_plus_lines}/-{num_minus_lines}"
    if len(diff_plus_minus) > 12 or diff_plus_minus == "+0/-0":
        diff_plus_minus = "[link]"
    return diff_plus_minus


def _cloud_diff_file(raw_diff, status="modified", lines_added=0, lines_removed=0,
                     filename="src/example.py", old_filename=None, drop_diffstat_counts=False,
                     drop_diffstat_removed=False):
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.diff_files = None
    provider.pr = MagicMock()

    diffstat = MagicMock()
    diffstat.new.path = None if status == "removed" else filename
    diffstat.old.path = None if status == "added" else old_filename or filename
    diffstat.data = {"status": status}
    if not drop_diffstat_counts:
        diffstat.data["lines_added"] = lines_added
        if not drop_diffstat_removed:
            diffstat.data["lines_removed"] = lines_removed
    provider.pr.diffstat.return_value = [diffstat]
    provider.pr.diff.return_value = raw_diff

    settings = MagicMock()
    settings.get.return_value = True  # bitbucket_app.avoid_full_files -> no file content calls
    with (
        patch("pr_agent.git_providers.bitbucket_provider.filter_ignored", return_value=[diffstat]),
        patch("pr_agent.git_providers.bitbucket_provider.get_settings", return_value=settings),
    ):
        return provider.get_diff_files()[0]


MODIFIED_RAW_DIFF = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -1,4 +1,5 @@
 keep
-drop
+add one
+add two
 tail
"""

ADDED_RAW_DIFF = """diff --git a/src/generated.py b/src/generated.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/generated.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""

REMOVED_RAW_DIFF = """diff --git a/src/legacy.py b/src/legacy.py
deleted file mode 100644
index 4444444..0000000
--- a/src/legacy.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-forgotten
"""


class TestBitbucketCloudLineCounts:
    def test_counts_ignore_the_unified_diff_file_headers(self):
        # The `--- a/...` and `+++ b/...` lines must not be counted as changed
        # source lines; only the hunk body counts.
        diff_file = _cloud_diff_file(MODIFIED_RAW_DIFF, lines_added=2, lines_removed=1)

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 1

    def test_added_file_reports_only_additions(self):
        diff_file = _cloud_diff_file(ADDED_RAW_DIFF, status="added", lines_added=3,
                                     old_filename=None)

        assert diff_file.num_plus_lines == 3
        assert diff_file.num_minus_lines == 0
        assert diff_file.edit_type == EDIT_TYPE.ADDED

    def test_removed_file_reports_only_deletions(self):
        diff_file = _cloud_diff_file(REMOVED_RAW_DIFF, status="removed", lines_removed=2)

        assert diff_file.num_plus_lines == 0
        assert diff_file.num_minus_lines == 2
        assert diff_file.edit_type == EDIT_TYPE.DELETED

    def test_rename_without_hunks_reports_zeroes(self):
        raw_diff = """diff --git a/src/old.py b/src/new.py
similarity index 100%
rename from src/old.py
rename to src/new.py
"""
        diff_file = _cloud_diff_file(raw_diff, status="renamed", filename="src/new.py",
                                     old_filename="src/old.py")

        assert diff_file.num_plus_lines == 0
        assert diff_file.num_minus_lines == 0

    def test_diffstat_counts_survive_a_diff_without_any_hunk(self):
        # Bitbucket's diffstat is authoritative: when the raw diff carries no textual hunk the
        # counts must come from the diffstat, not from the empty patch.
        raw_diff = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
Binary files /dev/null and b/src/example.py differ
"""
        diff_file = _cloud_diff_file(raw_diff, lines_added=1, lines_removed=1)

        assert diff_file.patch == ""
        assert diff_file.num_plus_lines == 1
        assert diff_file.num_minus_lines == 1

    def test_diffstat_counts_win_over_a_truncated_patch(self):
        # A truncated raw diff under-reports, so the diffstat stays the source of truth.
        raw_diff = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -1,3 +1,3 @@
 keep
-drop
+add
"""
        diff_file = _cloud_diff_file(raw_diff, lines_added=40, lines_removed=12)

        assert diff_file.num_plus_lines == 40
        assert diff_file.num_minus_lines == 12

    def test_falls_back_to_counting_the_patch_when_the_diffstat_omits_counts(self):
        diff_file = _cloud_diff_file(MODIFIED_RAW_DIFF, drop_diffstat_counts=True)

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 1

    def test_a_partially_populated_diffstat_falls_back_per_side(self):
        # `lines_added` is present and authoritative; `lines_removed` is absent, so only that
        # side falls back to the patch. The reported side must not collapse to 0.
        diff_file = _cloud_diff_file(MODIFIED_RAW_DIFF, lines_added=2, drop_diffstat_removed=True)

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 1

    def test_a_partially_populated_diffstat_falls_back_against_an_empty_patch(self):
        # With no hunk to count, the absent side has nothing to fall back to and stays 0,
        # while the reported side keeps the diffstat value.
        raw_diff = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
Binary files /dev/null and b/src/example.py differ
"""
        diff_file = _cloud_diff_file(raw_diff, lines_added=7, drop_diffstat_removed=True)

        assert diff_file.num_plus_lines == 7
        assert diff_file.num_minus_lines == 0

    def test_non_numeric_diffstat_counts_fall_back_to_the_patch(self):
        provider = BitbucketProvider.__new__(BitbucketProvider)
        provider.diff_files = None
        provider.pr = MagicMock()

        diffstat = MagicMock()
        diffstat.new.path = "src/example.py"
        diffstat.old.path = "src/example.py"
        diffstat.data = {"status": "modified", "lines_added": "two", "lines_removed": "one"}
        provider.pr.diffstat.return_value = [diffstat]
        provider.pr.diff.return_value = MODIFIED_RAW_DIFF

        settings = MagicMock()
        settings.get.return_value = True
        with (
            patch("pr_agent.git_providers.bitbucket_provider.filter_ignored", return_value=[diffstat]),
            patch("pr_agent.git_providers.bitbucket_provider.get_settings", return_value=settings),
        ):
            diff_file = provider.get_diff_files()[0]

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 1

    @pytest.mark.parametrize(
        ("num_plus_lines", "num_minus_lines", "expected"),
        [
            (2, 1, "+2/-1"),
            (0, 0, "[link]"),
            (120000, 3400, "[link]"),
        ],
    )
    def test_pr_description_never_renders_the_unset_sentinel(self, num_plus_lines, num_minus_lines, expected):
        assert _format_delta(num_plus_lines, num_minus_lines) == expected
        assert _format_delta(num_plus_lines, num_minus_lines) != "+-1/--1"

    def test_unset_counts_would_leak_into_the_description(self):
        # Guards the reason this fix exists: the default -1 sentinel survives both
        # of pr_description's guards and is written into the file table as-is.
        assert _format_delta(-1, -1) == "+-1/--1"


class TestBitbucketServerLineCounts:
    @staticmethod
    def _server_diff_file(original, new, change_type="MODIFY"):
        bitbucket_client = MagicMock(Bitbucket)
        bitbucket_client.get_pull_request.return_value = {
            "toRef": {"latestCommit": "base-sha"},
            "fromRef": {"latestCommit": "head-sha"},
        }
        bitbucket_client.get_pull_requests_commits.return_value = [
            {"id": "head-sha", "parents": [{"id": "base-sha"}]},
        ]
        bitbucket_client.get_pull_requests_changes.return_value = [
            {"path": {"toString": "src/example.py"}, "type": change_type},
        ]

        def get_content_of_file(project_key, repository_slug, path, at=None, markup=None):
            return {"base-sha": original, "head-sha": new}.get(at, "")

        bitbucket_client.get_content_of_file.side_effect = get_content_of_file

        provider = BitbucketServerProvider(
            "https://git.onpreminstance.com/projects/AAA/repos/my-repo/pull-requests/1",
            bitbucket_client=bitbucket_client,
        )
        return provider.get_diff_files()[0]

    def test_modified_file_counts_the_generated_hunk(self):
        original = "keep\ndrop\ntail\n"
        new = "keep\nadd one\nadd two\ntail\n"

        diff_file = self._server_diff_file(original, new)

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 1
        assert diff_file.edit_type == EDIT_TYPE.MODIFIED

    def test_added_file_reports_only_additions(self):
        diff_file = self._server_diff_file("", "one\ntwo\n", change_type="ADD")

        assert diff_file.num_plus_lines == 2
        assert diff_file.num_minus_lines == 0
        assert diff_file.edit_type == EDIT_TYPE.ADDED

    def test_deleted_file_reports_only_deletions(self):
        diff_file = self._server_diff_file("gone\nforgotten\n", "", change_type="DELETE")

        assert diff_file.num_plus_lines == 0
        assert diff_file.num_minus_lines == 2
        assert diff_file.edit_type == EDIT_TYPE.DELETED

    def test_unchanged_file_reports_zeroes(self):
        diff_file = self._server_diff_file("same\n", "same\n")

        assert diff_file.num_plus_lines == 0
        assert diff_file.num_minus_lines == 0
