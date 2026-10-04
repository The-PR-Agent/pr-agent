"""Regression tests for three defects that silently dropped or corrupted output.

1. ``PRQuestions.identify_image_in_comment`` used ``split('https://')[1]``, which keeps
   everything after the *first* scheme. A question with more than one link lost the real
   image and handed prose to the image fetch, so ``/ask`` answered with an error instead
   of a response.
2. ``check_if_hunk_lines_matches_to_file`` wrapped its comparison in a bare ``except: pass``
   and returned ``is_valid_hunk`` still set to its ``True`` default, so a hunk header
   pointing past the end of the original file was reported as valid and the caller extended
   it with a bogus range.
3. ``get_main_pr_language`` picked the most common extension with
   ``max(set(extension_list), key=extension_list.count)``. On a tie the winner came from
   hash-randomized set iteration order, so the resolved language varied between processes.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pr_agent.algo.git_patch_processing import check_if_hunk_lines_matches_to_file, extend_patch
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.tools.pr_questions import PRQuestions

# ---------------------------------------------------------------------------
# 1. image reference extraction
# ---------------------------------------------------------------------------


def _identify(question: str) -> str:
    tool = PRQuestions.__new__(PRQuestions)
    tool.vars = {}
    tool.question_str = question
    return tool.identify_image_in_comment()


def test_direct_image_link_after_another_link_is_extracted():
    """The image must win even when an unrelated link appears first in the question."""
    question = "check https://docs.example.com/page, why does https://i.imgur.com/b.png look wrong?"

    assert _identify(question) == "https://i.imgur.com/b.png"
    assert _identify(question) == PRQuestions._find_image_url(question)


def test_first_of_several_image_links_is_used():
    assert _identify("compare https://a.example.com/1.png and https://b.example.com/2.png") == \
        "https://a.example.com/1.png"


def test_prose_around_the_link_is_not_absorbed():
    """Regression: the old code returned the whole tail of the question."""
    question = "check https://docs.example.com/page fail here? see https://i.imgur.com/b.png"

    extracted = _identify(question)

    assert extracted == "https://i.imgur.com/b.png"
    assert "fail here" not in extracted
    assert "docs.example.com" not in extracted


def test_mentioning_an_image_extension_without_an_image_url_is_not_treated_as_an_image():
    """Regression: 'jpg' in the text used to make any https link an image path."""
    question = "can you explain this https://example.com/api? we tried jpg conversion"

    assert _identify(question) == ""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.example.com/a/b/c.PNG", "https://cdn.example.com/a/b/c.PNG"),
        ("https://cdn.example.com/x.jpeg?token=1", "https://cdn.example.com/x.jpeg?token=1"),
        ("https://cdn.example.com/y.gif", "https://cdn.example.com/y.gif"),
        ("https://cdn.example.com/z.webp", "https://cdn.example.com/z.webp"),
        ("https://cdn.example.com/s.svg", ""),
    ],
)
def test_image_extensions_and_query_strings(url, expected):
    assert _identify(f"what is wrong with {url}?") == expected


def test_markdown_directive_form_still_works():
    """The '![image](path)' form must keep taking precedence over link scanning."""
    tool = PRQuestions.__new__(PRQuestions)
    tool.vars = {}
    tool.question_str = "/ask what is this > ![image](https://i.imgur.com/x.png)"

    assert tool.identify_image_in_comment() == "https://i.imgur.com/x.png"
    assert tool.vars["img_path"] == "https://i.imgur.com/x.png"


def test_resolved_path_is_recorded_in_vars():
    tool = PRQuestions.__new__(PRQuestions)
    tool.vars = {}
    tool.question_str = "look at https://a.example.com/1.png and https://b.example.com/2.png"

    assert tool.identify_image_in_comment() == "https://a.example.com/1.png"
    assert tool.vars["img_path"] == "https://a.example.com/1.png"


def test_no_image_leaves_img_path_unset():
    """A miss must not poison vars with the previous run's value."""
    tool = PRQuestions.__new__(PRQuestions)
    tool.vars = {}
    tool.question_str = "plain question, no links at all"

    assert tool.identify_image_in_comment() == ""
    assert "img_path" not in tool.vars


# ---------------------------------------------------------------------------
# 2. hunk validation must fail closed
# ---------------------------------------------------------------------------


def test_hunk_header_past_end_of_file_is_reported_invalid():
    """Regression: the IndexError used to be swallowed and the hunk reported valid."""
    original_lines = ["line1", "line2", "line3"]
    patch_lines = ["@@ -10000,3 +1,3 @@", " context"]

    assert check_if_hunk_lines_matches_to_file(0, original_lines, patch_lines, 10000) is False


def test_hunk_header_past_end_of_file_is_not_extended():
    original = "".join(f"line{i}\n" for i in range(1, 4))
    patch = "@@ -10000,3 +1,3 @@\n context"

    extended = extend_patch(original, patch, patch_extra_lines_before=2, patch_extra_lines_after=2)

    # An unvalidatable hunk is not padded with original-file lines, so none of the
    # three real lines may be spliced in around the bogus start1.
    for leaked in (" line1", " line2", " line3"):
        assert leaked not in extended
    assert " context" in extended


def test_genuinely_mismatched_hunk_is_still_invalid():
    original_lines = ["line1", "line2", "line3"]
    patch_lines = ["@@ -1,2 +1,2 @@", " completely_different"]

    assert check_if_hunk_lines_matches_to_file(0, original_lines, patch_lines, 1) is False


def test_matching_hunk_is_still_valid():
    original_lines = ["line1", "line2", "line3"]
    patch_lines = ["@@ -1,2 +1,2 @@", " line1"]

    assert check_if_hunk_lines_matches_to_file(0, original_lines, patch_lines, 1) is True


# ---------------------------------------------------------------------------
# 3. language detection must not depend on hash randomization
# ---------------------------------------------------------------------------

_TIE_SNIPPET = textwrap.dedent(
    """
    from pr_agent.git_providers.git_provider import FilePatchInfo, get_main_pr_language
    files = [FilePatchInfo(base_file=None, head_file=None, patch=None, filename=name)
             for name in ['a.py', 'b.js', 'c.py', 'd.js', 'e.py', 'f.js']]
    print(get_main_pr_language({'Python': 60, 'JavaScript': 40}, files))
    """
)


def test_tied_extension_counts_resolve_identically_across_processes():
    """Regression: a 3-vs-3 tie used to resolve by set iteration order, varying per process."""
    repo_root = str(Path(__file__).resolve().parents[2])
    results = set()
    for seed in ("0", "1", "2", "3", "4", "5"):
        completed = subprocess.run(
            [sys.executable, "-c", _TIE_SNIPPET],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": repo_root, "PATH": "/usr/bin:/bin"},
        )
        assert completed.returncode == 0, completed.stderr
        results.add(completed.stdout.strip())

    assert len(results) == 1, f"language detection is not deterministic: {results}"

    # a language was resolved, not an empty string
    (resolved,) = results
    assert resolved


def test_unequivocal_majority_is_unaffected():
    files = ["a.js", "b.py", "c.py", "d.py"]
    assert get_main_pr_language({"Python": 75, "JavaScript": 25}, files) == "python"
