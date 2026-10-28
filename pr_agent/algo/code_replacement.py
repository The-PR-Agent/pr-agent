"""Helpers for turning a model-proposed code replacement into an applicable change.

Models return replacement code in YAML block scalars, which strip its common indentation, so the code
has to be re-indented against the lines it replaces before it is offered as a committable suggestion.
A Python replacement is also compiled in place, so a suggestion that would break the file is not offered.
"""


from pr_agent.algo.git_patch_processing import split_diff_lines
from pr_agent.log import get_logger


def shift_code_indentation(code_snippet: str, delta_spaces: int) -> str:
    shifted_lines = []
    for line in code_snippet.splitlines():
        if not line.strip():
            shifted_lines.append("")
            continue
        if delta_spaces > 0:
            shifted_lines.append(" " * delta_spaces + line)
        elif delta_spaces < 0:
            shift = -delta_spaces
            leading_whitespace = len(line) - len(line.lstrip())
            shifted_lines.append(line[min(shift, leading_whitespace):])
        else:
            shifted_lines.append(line)
    return "\n".join(shifted_lines)


def _infer_space_indentation_unit(space_deltas: list[int]) -> int:
    if not space_deltas:
        return 1
    unique_deltas = sorted(set(space_deltas))
    for delta in unique_deltas:
        if delta * 2 in unique_deltas:
            return delta
    return unique_deltas[0]


def _continuation_space_adjustments(
    lines: list[str],
    leading_whitespace: list[str],
) -> list[int]:
    openers = []
    adjustments = [0] * len(lines)
    closer_for = {"(": ")", "[": "]"}
    for index, (line, prefix) in enumerate(
        zip(lines, leading_whitespace, strict=True)
    ):
        stripped = line.strip()
        if not stripped:
            continue
        for opener_position in range(len(openers) - 1, -1, -1):
            opener_index, opener_prefix, closer = openers[opener_position]
            if prefix == opener_prefix and stripped.startswith(closer):
                interior_indexes = [
                    line_index
                    for line_index in range(opener_index + 1, index)
                    if lines[line_index].strip()
                ]
                opener_spaces = opener_prefix.count(" ")
                positive_offsets = [
                    leading_whitespace[line_index].count(" ") - opener_spaces
                    for line_index in interior_indexes
                    if leading_whitespace[line_index].count(" ") > opener_spaces
                ]
                if positive_offsets:
                    continuation_offset = min(positive_offsets)
                    for line_index in interior_indexes:
                        if leading_whitespace[line_index].count(" ") > opener_spaces:
                            adjustments[line_index] += continuation_offset
                del openers[opener_position:]
                break
        if stripped[-1] in closer_for:
            openers.append((index, prefix, closer_for[stripped[-1]]))
    return adjustments


def align_code_with_tabs(code_snippet: str, anchor_prefix: str) -> str:
    lines = code_snippet.splitlines()
    if not lines:
        return code_snippet
    leading_whitespace = [line[:len(line) - len(line.lstrip())] for line in lines]
    anchor_depth = len(anchor_prefix) - len(anchor_prefix.lstrip("\t"))
    anchor_alignment = anchor_prefix[anchor_depth:]
    initial_index, initial_prefix = next(
        (
            (index, prefix)
            for index, (line, prefix) in enumerate(
                zip(lines, leading_whitespace, strict=True)
            )
            if line.strip()
        ),
        (0, ""),
    )
    initial_spaces = initial_prefix.count(" ")
    initial_tabs = initial_prefix.count("\t")
    continuation_adjustments = _continuation_space_adjustments(
        lines,
        leading_whitespace,
    )
    initial_continuation_adjustment = continuation_adjustments[initial_index]
    adjusted_initial_spaces = initial_spaces - initial_continuation_adjustment
    space_deltas = [
        abs(
            prefix.count(" ")
            - continuation_adjustments[index]
            - adjusted_initial_spaces
        )
        for index, (line, prefix) in enumerate(
            zip(lines, leading_whitespace, strict=True)
        )
        if (
            line.strip()
            and prefix.count(" ") - continuation_adjustments[index]
            != adjusted_initial_spaces
        )
    ]
    space_unit = _infer_space_indentation_unit(space_deltas)
    aligned_lines = []
    for index, (line, prefix) in enumerate(
        zip(lines, leading_whitespace, strict=True)
    ):
        if not line.strip():
            aligned_lines.append("")
            continue
        continuation_alignment = (
            continuation_adjustments[index] - initial_continuation_adjustment
        )
        relative_space_depth, alignment_spaces = divmod(
            prefix.count(" ")
            - initial_spaces
            - continuation_alignment,
            space_unit,
        )
        relative_depth = (
            prefix.count("\t") - initial_tabs
            + relative_space_depth
        )
        aligned_lines.append(
            "\t" * max(0, anchor_depth + relative_depth)
            + anchor_alignment
            + " " * (alignment_spaces + continuation_alignment)
            + line[len(prefix):]
        )
    return "\n".join(aligned_lines).rstrip("\n")


def reindent_to_line(code_snippet: str, original_initial_line: str) -> str:
    """Re-indent a code snippet so its first non-blank line matches the indentation of the line it replaces."""
    suggested_initial_line = next((line for line in code_snippet.splitlines() if line.strip()), "")
    original_initial_spaces = len(original_initial_line) - len(original_initial_line.lstrip())
    suggested_initial_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
    if original_initial_line.startswith("\t"):
        return align_code_with_tabs(code_snippet, original_initial_line[:original_initial_spaces])
    return shift_code_indentation(code_snippet, original_initial_spaces - suggested_initial_spaces)


def python_replacement_compiles(filename: str, head_file: str, relevant_lines_start: int,
                                relevant_lines_end: int, new_code_snippet: str) -> bool | None:
    """Return False only when a replacement makes valid Python fail compilation.

    Returns None when the check does not apply: not a Python file, the file does not compile as is,
    or the line range is outside the file.
    """
    if not (filename or "").lower().endswith((".py", ".pyi", ".pyw")) or not head_file:
        return None

    try:
        compile(head_file, filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    except Exception as e:
        get_logger().warning(f"Could not validate Python suggestion syntax: {e}")
        return None

    file_lines = split_diff_lines(head_file)
    if (relevant_lines_start < 1
            or relevant_lines_end < relevant_lines_start
            or relevant_lines_end > len(file_lines)):
        return None
    file_lines[relevant_lines_start - 1:relevant_lines_end] = new_code_snippet.splitlines()

    try:
        compile("\n".join(file_lines), filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    except Exception as e:
        get_logger().warning(f"Could not validate Python suggestion syntax: {e}")
        return None
    return True
