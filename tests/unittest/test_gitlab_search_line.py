"""A relevant_file missing from the diff must report "not found" instead of raising UnboundLocalError."""
import re
from unittest.mock import MagicMock

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers.gitlab_provider import GitLabProvider

PATCH = "@@ -1,2 +1,2 @@\n line1\n+added\n-line2\n"
DIFF_FILES = [
    FilePatchInfo(base_file="a.py", head_file="a.py", patch=PATCH,
                  filename="a.py", edit_type=EDIT_TYPE.MODIFIED),
]


def _provider(diff_files):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
    provider.get_diff_files = lambda: list(diff_files)
    provider.max_comment_chars = 65000
    return provider


def test_search_line_reports_not_found_for_file_absent_from_diff():
    provider = _provider(DIFF_FILES)

    edit_type, found, source_line_no, target_file, target_line_no = provider.search_line("missing.py", "+added")

    assert found is False
    assert (source_line_no, target_line_no, target_file) == (0, 0, None)
    assert edit_type == "addition"


def test_search_line_reports_not_found_for_empty_diff():
    provider = _provider([])

    assert provider.search_line("a.py", "+added")[1] is False


def test_search_line_still_resolves_a_present_file():
    provider = _provider(DIFF_FILES)

    edit_type, found, source_line_no, target_file, target_line_no = provider.search_line("a.py", "+added")

    assert found is True
    assert target_file.filename == "a.py"
    assert (edit_type, source_line_no, target_line_no) == ("addition", 2, 3)


def test_search_line_uses_the_first_matching_file():
    duplicate = FilePatchInfo(base_file="a.py", head_file="a.py", patch=PATCH,
                              filename="a.py", edit_type=EDIT_TYPE.MODIFIED)
    provider = _provider(DIFF_FILES + [duplicate])
    provider.find_in_file = MagicMock(return_value=("addition", True, 1, DIFF_FILES[0], 1))

    assert provider.search_line("a.py", "+added")[3] is DIFF_FILES[0]
    provider.find_in_file.assert_called_once_with(DIFF_FILES[0], "+added")


def test_publish_inline_comment_skips_absent_file_instead_of_raising():
    provider = _provider(DIFF_FILES)
    sent = []
    provider.send_inline_comment = lambda *args: sent.append(args) or False

    provider.publish_inline_comment("body", "missing.py", "+added")

    # send_inline_comment still runs, and bails out because found is False
    assert len(sent) == 1
    assert sent[0][2] is False
