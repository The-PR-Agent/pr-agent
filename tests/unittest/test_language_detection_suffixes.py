"""Regression tests for language detection that ignored all but the last suffix.

``set_file_languages`` and ``get_main_pr_language`` both classified a file by
``filename.rsplit('.')[-1]``. The configured map stores multi-part keys
(``.cmake.in``), wildcard keys (``*.bsl``) and its lookups were case-sensitive, so
``Config.cmake.in``, ``module.bsl`` and ``handler.PY`` all resolved to ``txt`` or to
nothing at all. Every provider that calls ``build_language_file_matcher`` already
resolves these correctly; the two helpers above were the stragglers.
"""

import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import extract_relevant_lines_str, set_file_languages
from pr_agent.git_providers.git_provider import get_main_pr_language


def _files(names):
    return [
        FilePatchInfo(base_file=None, head_file=None, patch=None, filename=name)
        for name in names
    ]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("app/main.py", "python"),
        ("cmake/Config.cmake.in", "cmake"),
        ("src/module.bsl", "1c enterprise"),
        ("app/handler.PY", "python"),
        ("docs/index.html.hl", "html"),
        ("src/lib.rs.in", "rust"),
        ("README.md", "markdown"),
    ],
)
def test_set_file_languages_resolves_non_simple_suffixes(filename, expected):
    (file,) = _files([filename])

    set_file_languages([file])

    assert file.language == expected


def test_set_file_languages_falls_back_to_txt_for_unknown_files():
    (file,) = _files(["data/table.unknownext"])

    set_file_languages([file])

    assert file.language == "txt"


def test_set_file_languages_keeps_a_language_that_is_already_set():
    (file,) = _files(["app/main.py"])
    file.language = "rust"

    set_file_languages([file])

    assert file.language == "rust"


def test_code_fence_uses_the_resolved_language():
    """The fence label is what a reader sees, so an uppercase suffix must not degrade to txt."""
    filename = "app/handler.PY"
    file = FilePatchInfo(
        base_file=None, head_file="def handler():\n    return 1\n", patch=None, filename=filename
    )

    rendered = extract_relevant_lines_str(2, [file], filename, 1, 2)

    assert rendered.splitlines()[0] == "```python"


@pytest.mark.parametrize(
    ("languages", "files", "expected"),
    [
        # multi-part key
        ({"CMake": 900}, ["cmake/a.cmake.in", "cmake/b.cmake.in"], "cmake"),
        # wildcard key
        ({"1C Enterprise": 900}, ["src/a.bsl", "src/b.bsl"], "1c enterprise"),
        # uppercase suffix
        ({"Python": 900}, ["app/Handler.PY"], "python"),
        # a stray README must not outvote the code
        ({"CMake": 900}, ["cmake/a.cmake.in", "README.txt"], "cmake"),
    ],
)
def test_get_main_pr_language_resolves_non_simple_suffixes(languages, files, expected):
    assert get_main_pr_language(languages, files) == expected


def test_get_main_pr_language_prefers_the_diff_over_the_repository_ranking():
    """The provider ranks the whole repository; the diff itself decides when they disagree."""
    languages = {"Python": 5000, "Java": 4000}

    assert get_main_pr_language(languages, ["a.py", "b.java", "c.java", "d.java"]) == "java"
    assert get_main_pr_language(languages, ["a.java", "b.py", "c.py", "d.py"]) == "python"


def test_get_main_pr_language_is_empty_when_no_file_is_recognised():
    assert get_main_pr_language({"CMake": 900}, ["data/table.unknownext"]) == ""
