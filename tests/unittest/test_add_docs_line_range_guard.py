"""Range-check the model's line number before the dedent helper indexes the head file."""
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.tools.pr_add_docs import PRAddDocs

HEAD = "def f():\n    pass\n    return 1"
SNIPPET = '"""docs"""'


def _tool(head=HEAD):
    tool = PRAddDocs.__new__(PRAddDocs)

    class GP:
        diff_files = [FilePatchInfo(base_file="", head_file=head, patch="", filename="a.py",
                                    edit_type=EDIT_TYPE.MODIFIED)]

        def get_diff_files(self):
            return self.diff_files

    tool.git_provider = GP()
    return tool


def test_the_last_line_with_placement_after_is_still_dedented():
    """Dedent the final line, where `splitlines()[start]` is out of range."""
    result = _tool().dedent_code("a.py", 3, SNIPPET, doc_placement="after")

    assert result.startswith("    "), "snippet was not indented to match the last line"


def test_a_line_number_past_the_end_returns_the_snippet_unchanged():
    """Ignore an out-of-range line from the model instead of indexing the file."""
    assert _tool().dedent_code("a.py", 99, SNIPPET) == SNIPPET


def test_a_zero_line_number_returns_the_snippet_unchanged():
    """Reject line 0, since line numbers are 1-based and 0 reads the last line."""
    assert _tool().dedent_code("a.py", 0, SNIPPET) == SNIPPET


def test_a_middle_line_with_placement_after_still_dedents():
    """Keep the existing behaviour where a following line exists."""
    result = _tool().dedent_code("a.py", 1, SNIPPET, doc_placement="after")

    assert result.startswith("    ")


def test_placement_before_uses_the_target_line_indentation():
    """Read the target line itself for placement='before', which is in range."""
    result = _tool().dedent_code("a.py", 2, SNIPPET, doc_placement="before")

    assert result.startswith("    ")
