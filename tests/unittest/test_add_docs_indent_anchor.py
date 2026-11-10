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


@pytest.mark.parametrize("add_original_line", [False, True])
def test_a_file_of_only_blank_lines_cannot_supply_a_replacement_anchor(add_original_line):
    result = _tool("\n\n\n").dedent_code("a.py", 2, SNIPPET, add_original_line=add_original_line)

    assert result == (None if add_original_line else SNIPPET)


@pytest.mark.parametrize("head, filename, line", [
    ("", "a.py", 1),
    ("def f():\n    return 1", "missing.py", 1),
    ("def f():\n    return 1", "a.py", 0),
    ("def f():\n    return 1", "a.py", 3),
])
def test_unavailable_original_line_does_not_become_a_documentation_only_replacement(head, filename, line):
    assert _tool(head).dedent_code(filename, line, SNIPPET, add_original_line=True) is None


def test_partial_head_content_cannot_supply_an_original_line():
    # Model a hunk starting at line 2, where source line 2 is at index 0.
    tool = _tool("def f():\n    return 1")
    tool.git_provider.diff_files[0].head_file_is_complete = False

    assert tool.dedent_code("a.py", 2, SNIPPET) == "    " + SNIPPET
    assert tool.dedent_code("a.py", 2, SNIPPET, add_original_line=True) is None


def test_source_read_error_does_not_become_a_documentation_only_replacement():
    class UnreadableSource(str):
        def split(self, *args, **kwargs):
            raise ValueError("source unavailable")

    assert _tool(UnreadableSource("def f():")).dedent_code("a.py", 1, SNIPPET, add_original_line=True) is None


def test_blank_lines_are_skipped_when_anchoring_on_a_comment_line():
    """Comments are real code lines and supply usable indentation."""
    head = "def f():\n    x = 1\n\n    # trailing note\n    return x"

    result = _tool(head).dedent_code("a.py", 2, SNIPPET, doc_placement="after", add_original_line=True)

    assert '    """Adds docs."""' in result
    compile(_apply(head, 2, result), "a.py", "exec")


@pytest.mark.parametrize("prefix, model_prefix, placement", [
    ("\t", "", "before"),
    ("\t", "    ", "after"),
    (" \t", "        ", "before"),
    (" \t", "", "after"),
    ("    ", "        ", "before"),
    ("    ", "        ", "after"),
])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_documentation_uses_the_literal_anchor_prefix(prefix, model_prefix, placement, newline):
    head = f"def f():{newline}{prefix}return 1"
    compile(head, "a.py", "exec")
    line = 1 if placement == "after" else 2
    target = head.splitlines()[line - 1]

    result = _tool(head).dedent_code(
        "a.py", line, model_prefix + SNIPPET, doc_placement=placement, add_original_line=True
    )

    aligned = prefix + SNIPPET
    assert result == (target + "\n" + aligned if placement == "after" else aligned + "\n" + target)
    compile(_apply(head, line, result), "a.py", "exec")


@pytest.mark.parametrize("placement", ["before", "after"])
def test_multiline_alignment_preserves_relative_indentation_and_blank_positions(placement):
    head = "def f():\n\treturn 1"
    snippet = '\n    """Summary.\n      Detail.\n   \n    End.\n    """\n\n'
    aligned = '\n\t"""Summary.\n\t  Detail.\n\n\tEnd.\n\t"""\n\n'
    line = 1 if placement == "after" else 2
    target = head.splitlines()[line - 1]

    tool = _tool(head)
    assert tool.dedent_code("a.py", line, snippet, doc_placement=placement) == aligned
    result = tool.dedent_code("a.py", line, snippet, doc_placement=placement, add_original_line=True)

    assert result == (target + "\n" + aligned if placement == "after" else aligned.rstrip() + "\n" + target)
    compile(_apply(head, line, result), "a.py", "exec")


@pytest.mark.parametrize("snippet", [
    '    """Summary.\n  Outdented.\n    """',
    '\t"""Summary.\n    Incompatible.\n\t"""',
    " \t\n\n ",
])
def test_unalignable_documentation_is_rejected(snippet):
    assert _tool("def f():\n    return 1").dedent_code("a.py", 1, snippet, add_original_line=True) is None


def test_post_anchor_alignment_error_does_not_return_raw_documentation():
    class UnreadableDocumentation(str):
        def replace(self, *args, **kwargs):
            raise ValueError("unreadable documentation")

        def split(self, *args, **kwargs):
            raise ValueError("unreadable documentation")

        def splitlines(self, *args, **kwargs):
            raise ValueError("unreadable documentation")

    snippet = UnreadableDocumentation(SNIPPET)
    assert _tool("def f():\n    return 1").dedent_code("a.py", 1, snippet, add_original_line=True) is None


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n"])
@pytest.mark.parametrize("placement", ["before", "after"])
def test_each_python_logical_line_is_aligned(newline, placement):
    head = "def f():\n    return 1"
    snippet = newline.join(['    """First."""', '    """Second."""'])
    line = 1 if placement == "after" else 2
    target = head.splitlines()[line - 1]
    aligned = '    """First."""\n    """Second."""'

    result = _tool(head).dedent_code(
        "a.py", line, snippet, doc_placement=placement, add_original_line=True
    )

    assert result == (target + "\n" + aligned if placement == "after" else aligned + "\n" + target)
    compile(_apply(head, line, result), "a.py", "exec")


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_cr_newlines_keep_blank_positions_and_relative_indentation(newline):
    snippet = newline.join(['', '    """Summary.', '      Detail.', ' ', '    """', ''])
    expected = '\n\t"""Summary.\n\t  Detail.\n\n\t"""\n'

    assert _tool("def f():\n\treturn 1").dedent_code("a.py", 1, snippet) == expected


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_cr_newlines_do_not_hide_incompatible_prefixes(newline):
    snippet = newline.join(['    """Summary.', '  Outdented.', '    """'])

    assert _tool("def f():\n    return 1").dedent_code("a.py", 1, snippet) is None


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c"])
def test_literal_separators_inside_a_docstring_are_preserved(separator):
    snippet = f'    """Résumé.{separator}Body."""'
    expected = f'\t"""Résumé.{separator}Body."""'

    result = _tool("def f():\n\treturn 1").dedent_code("a.py", 1, snippet)

    assert result == expected
    namespace = {}
    exec(compile("def f():\n" + result + "\n\treturn 1", "a.py", "exec"), namespace)
    assert namespace["f"].__doc__ == f"Résumé.{separator}Body."
