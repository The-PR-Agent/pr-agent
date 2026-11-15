"""Checks shared by /improve and /review before a code fix is offered as a committable suggestion."""

import re
import textwrap

from pr_agent.algo.code_replacement import python_replacement_compiles, reindent_to_line
from pr_agent.algo.git_patch_processing import split_diff_lines
from pr_agent.log import get_logger


def get_patch_range_lines(patch, relevant_lines_start, relevant_lines_end) -> list[str] | None:
    target_lines = {}
    target_line = None
    target_remaining = 0
    for line in split_diff_lines(patch or ""):
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if hunk_match:
            target_line = int(hunk_match.group(1))
            target_remaining = int(hunk_match.group(2) or 1)
            continue
        if target_line is None or target_remaining == 0 or line.startswith(("-", "\\")):
            continue
        if line.startswith((" ", "+")):
            if relevant_lines_start <= target_line <= relevant_lines_end:
                target_lines[target_line] = line[1:]
            target_line += 1
            target_remaining -= 1

    if len(target_lines) != relevant_lines_end - relevant_lines_start + 1:
        return None
    return [target_lines[line_number]
            for line_number in range(relevant_lines_start, relevant_lines_end + 1)]


def is_line_range_in_diff(diff_file, relevant_lines_start: int, relevant_lines_end: int) -> bool:
    """Return True when the 1-based inclusive line range can be resolved from head_file or patch."""
    if diff_file is None or relevant_lines_start < 1 or relevant_lines_end < relevant_lines_start:
        return False
    if diff_file.head_file and getattr(diff_file, "head_file_is_complete", True):
        line_count = diff_file.head_file.count("\n") + (not diff_file.head_file.endswith("\n"))
        return relevant_lines_end <= line_count
    return get_patch_range_lines(diff_file.patch, relevant_lines_start, relevant_lines_end) is not None


def validate_suggestion(diff_file, relevant_lines_start, relevant_lines_end,
                        existing_code) -> tuple[bool, str, bool]:
    """Return (is_applicable, reason_if_not, has_valid_anchor) for a fix against the changed file."""
    if relevant_lines_start < 1 or relevant_lines_end < relevant_lines_start:
        return False, "the anchored range is outside the file", False

    if diff_file is None:
        return False, "the file content is unavailable", False
    if diff_file.head_file and getattr(diff_file, "head_file_is_complete", True):
        file_lines = split_diff_lines(diff_file.head_file)
        if relevant_lines_end > len(file_lines):
            return False, "the anchored range is outside the file", False
        anchored_lines = file_lines[relevant_lines_start - 1:relevant_lines_end]
    else:
        anchored_lines = get_patch_range_lines(diff_file.patch, relevant_lines_start, relevant_lines_end)
        if anchored_lines is None:
            return False, "the file content is unavailable", False

    if not existing_code:
        return False, "the existing code is unavailable", True
    anchored_lines = [line.rstrip(" \t") for line in textwrap.dedent("\n".join(anchored_lines)).split("\n")]
    existing_lines = [line.rstrip(" \t") for line in split_diff_lines(textwrap.dedent(existing_code))]
    if existing_lines != anchored_lines:
        return False, "the existing code does not match the anchored range", True
    return True, "", True


def dedent_code_for_file(diff_file, relevant_lines_start, new_code_snippet) -> str:
    """Re-indent a snippet to the indentation of the file line it replaces."""
    if diff_file.head_file and getattr(diff_file, "head_file_is_complete", True):
        file_lines = split_diff_lines(diff_file.head_file)
        if relevant_lines_start > len(file_lines):
            get_logger().warning(
                "Could not dedent code snippet, because relevant_lines_start is out of range",
                artifact={'filename': diff_file.filename,
                          'file_content': diff_file.head_file,
                          'relevant_lines_start': relevant_lines_start,
                          'new_code_snippet': new_code_snippet})
            return new_code_snippet
        original_initial_line = file_lines[relevant_lines_start - 1]
    else:
        patch_lines = get_patch_range_lines(diff_file.patch, relevant_lines_start, relevant_lines_start)
        if patch_lines is None:
            get_logger().warning(
                "Could not dedent code snippet, because file content is unavailable",
                artifact={'filename': diff_file.filename,
                          'relevant_lines_start': relevant_lines_start,
                          'new_code_snippet': new_code_snippet})
            return new_code_snippet
        original_initial_line = patch_lines[0]
    if original_initial_line:
        new_code_snippet = reindent_to_line(new_code_snippet, original_initial_line)
    return new_code_snippet


def python_syntax_is_valid(relevant_file: str, diff_file, relevant_lines_start: int,
                           relevant_lines_end: int, new_code_snippet: str) -> bool | None:
    """Return False only when a verified replacement makes valid Python fail compilation."""
    if not relevant_file.lower().endswith((".py", ".pyi", ".pyw")):
        return None
    if (diff_file is None
            or not diff_file.head_file
            or not getattr(diff_file, "head_file_is_complete", True)):
        return None
    return python_replacement_compiles(relevant_file, diff_file.head_file, relevant_lines_start,
                                       relevant_lines_end, new_code_snippet)
