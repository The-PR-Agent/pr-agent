"""Anchor docstring indentation on a real code line, never on a blank one.

A blank line has zero indentation, so using one as the reference emitted the
generated docstring at column 0. The suggestion then dropped the docstring out
of the enclosing block and left the file unparseable.
"""
import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.tools.pr_add_docs import PRAddDocs

SNIPPET = '"""Adds docs."""'


def _tool(head):
    tool = PRAddDocs.__new__(PRAddDocs)

    class GP:
        diff_files = [FilePatchInfo(base_file="", head_file=head, patch="", filename="a.py",
                                    edit_type=EDIT_TYPE.MODIFIED)]

        def get_diff_files(self):
            return self.diff_files

    tool.git_provider = GP()
    return tool


def _apply(head, line, suggestion):
    """Return the file produced by accepting `suggestion` for `line`."""
    lines = head.splitlines()
    lines[line - 1:line] = suggestion.splitlines()
    return "\n".join(lines)


@pytest.mark.parametrize("placement", ["before", "after"])
def test_blank_line_after_the_target_keeps_the_docstring_in_its_block(placement):
    """A blank line after the target has no indentation, so skip it."""
    head = "def f():\n    x = 1\n\n    return x"

    result = _tool(head).dedent_code("a.py", 2, SNIPPET, doc_placement=placement, add_original_line=True)

    assert "    " in result
    compile(_apply(head, 2, result), "a.py", "exec")


def test_docstring_after_a_def_still_uses_the_body_indentation():
    """Placement 'after' on a `def` line must follow the line into the function body."""
    head = "def f():\n    x = 1"

    result = _tool(head).dedent_code("a.py", 1, SNIPPET, doc_placement="after", add_original_line=True)

    assert result == 'def f():\n    """Adds docs."""'


def test_blank_line_between_a_def_and_its_body_still_uses_the_body_indentation():
    """A blank line after a `def` must not pull the docstring back to column 0."""
    head = "def f():\n\n    x = 1"

    result = _tool(head).dedent_code("a.py", 1, SNIPPET, doc_placement="after", add_original_line=True)

    assert result == 'def f():\n    """Adds docs."""'
    compile(_apply(head, 1, result), "a.py", "exec")


@pytest.mark.parametrize("placement", ["before", "after"])
def test_a_blank_target_line_takes_the_indentation_of_its_block(placement):
    """A blank target has no indentation of its own; the next code line has the block's."""
    head = "def f():\n\n    return x"

    result = _tool(head).dedent_code("a.py", 2, SNIPPET, doc_placement=placement, add_original_line=True)

    assert '"""Adds docs."""' in result
    compile(_apply(head, 2, result), "a.py", "exec")


def test_deeper_block_indentation_is_preserved_across_a_blank_line():
    """Indentation is resolved against the surrounding block, not always column 0."""
    head = "class C:\n    def f(self):\n        x = 1\n\n        return x"

    result = _tool(head).dedent_code("a.py", 3, SNIPPET, doc_placement="after", add_original_line=True)

    assert "        " in result
    assert '        """Adds docs."""' in result
    compile(_apply(head, 3, result), "a.py", "exec")


def test_a_leading_blank_line_in_the_snippet_does_not_skew_indentation():
    """The snippet's own base indentation comes from its first non-blank line."""
    head = "def f():\n    x = 1\n    y = 2"

    result = _tool(head).dedent_code("a.py", 2, "\n" + SNIPPET, doc_placement="after", add_original_line=True)

    assert '\n    """Adds docs."""' in result


def test_a_file_of_only_blank_lines_leaves_the_snippet_unchanged():
    """With no code line to anchor on, return the snippet rather than guess."""
    result = _tool("\n\n\n").dedent_code("a.py", 2, SNIPPET, doc_placement="after", add_original_line=True)

    assert result == SNIPPET


def test_blank_lines_are_skipped_when_anchoring_on_a_comment_line():
    """Comments are real code lines and supply usable indentation."""
    head = "def f():\n    x = 1\n\n    # trailing note\n    return x"

    result = _tool(head).dedent_code("a.py", 2, SNIPPET, doc_placement="after", add_original_line=True)

    assert '    """Adds docs."""' in result
    compile(_apply(head, 2, result), "a.py", "exec")
