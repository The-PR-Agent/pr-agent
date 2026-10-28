import pytest

from pr_agent.algo.code_replacement import python_replacement_compiles, reindent_to_line


@pytest.mark.parametrize(
    "code,original_line,expected",
    [
        ("if x:\n    return 1", "        if y:", "        if x:\n            return 1"),
        ("        if x:\n            return 1", "    if y:", "    if x:\n        return 1"),
        ("if x:\n    return 1", "\tif y:", "\tif x:\n\t\treturn 1"),
        ("if x:\n\n    return 1", "  if y:", "  if x:\n\n      return 1"),
    ],
)
def test_reindent_to_line(code, original_line, expected):
    assert reindent_to_line(code, original_line) == expected


HEAD = "def f():\n    return 1\n"


@pytest.mark.parametrize(
    "filename,head_file,start,end,replacement,expected",
    [
        ("app.py", HEAD, 2, 2, "    return 2", True),
        ("app.py", HEAD, 2, 2, "    return (", False),
        ("app.js", HEAD, 2, 2, "    return (", None),
        ("app.py", "def f(:\n", 1, 1, "def f():", None),
        ("app.py", HEAD, 2, 5, "    return 2", None),
        ("app.py", "", 1, 1, "x = 1", None),
    ],
)
def test_python_replacement_compiles(filename, head_file, start, end, replacement, expected):
    assert python_replacement_compiles(filename, head_file, start, end, replacement) is expected
