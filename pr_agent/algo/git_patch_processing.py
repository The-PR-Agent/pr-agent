from __future__ import annotations

import re
import traceback

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Optimized: Pre-compile the hunk header regex at the module level to avoid redundant compilation
# in performance-critical patch processing functions.
RE_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")


def extend_patch(original_file_str, patch_str, patch_extra_lines_before=0,
                 patch_extra_lines_after=0, filename: str = "", new_file_str="") -> str:
    if not patch_str or (patch_extra_lines_before == 0 and patch_extra_lines_after == 0) or not original_file_str:
        return patch_str

    original_file_str = decode_if_bytes(original_file_str)
    new_file_str = decode_if_bytes(new_file_str)
    if not original_file_str:
        return patch_str

    if should_skip_patch(filename):
        return patch_str

    try:
        extended_patch_str = process_patch_lines(patch_str, original_file_str,
                                                 patch_extra_lines_before, patch_extra_lines_after, new_file_str)
    except Exception as e:
        get_logger().warning(f"Failed to extend patch: {e}", artifact={"traceback": traceback.format_exc()})
        return patch_str

    return extended_patch_str


def decode_if_bytes(original_file_str):
    if isinstance(original_file_str, (bytes, bytearray)):
        try:
            return original_file_str.decode('utf-8')
        except UnicodeDecodeError:
            encodings_to_try = ['iso-8859-1', 'latin-1', 'ascii', 'utf-16']
            for encoding in encodings_to_try:
                try:
                    return original_file_str.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return ""
    return original_file_str


def should_skip_patch(filename):
    patch_extension_skip_types = get_settings().config.patch_extension_skip_types
    if patch_extension_skip_types and filename:
        return any(filename.endswith(skip_type) for skip_type in patch_extension_skip_types)
    return False


def process_patch_lines(patch_str, original_file_str, patch_extra_lines_before, patch_extra_lines_after, new_file_str=""):
    allow_dynamic_context = get_settings().config.allow_dynamic_context
    patch_extra_lines_before_dynamic = get_settings().config.max_extra_lines_before_dynamic_context

    file_original_lines = original_file_str.splitlines()
    file_new_lines = new_file_str.splitlines() if new_file_str else []
    len_original_lines = len(file_original_lines)
    patch_lines = patch_str.splitlines()
    extended_patch_lines = []

    is_valid_hunk = True
    start1, size1, start2, size2 = -1, -1, -1, -1
    try:
        for i,line in enumerate(patch_lines):
            if line.startswith('@@'):
                match = RE_HUNK_HEADER.match(line)
                # identify hunk header
                if match:
                    # finish processing previous hunk
                    if is_valid_hunk and (start1 != -1 and patch_extra_lines_after > 0):
                        delta_lines_original = [f' {line}' for line in file_original_lines[start1 + size1 - 1:start1 + size1 - 1 + patch_extra_lines_after]]
                        extended_patch_lines.extend(delta_lines_original)

                    section_header, size1, size2, start1, start2 = extract_hunk_headers(match)

                    is_valid_hunk = check_if_hunk_lines_matches_to_file(i, file_original_lines, patch_lines, start1)

                    if is_valid_hunk and (patch_extra_lines_before > 0 or patch_extra_lines_after > 0):
                        def _calc_context_limits(patch_lines_before):
                            extended_start1 = max(1, start1 - patch_lines_before)
                            extended_size1 = size1 + (start1 - extended_start1) + patch_extra_lines_after
                            extended_start2 = max(1, start2 - patch_lines_before)
                            extended_size2 = size2 + (start2 - extended_start2) + patch_extra_lines_after
                            if extended_start1 - 1 + extended_size1 > len_original_lines:
                                # we cannot extend beyond the original file
                                delta_cap = extended_start1 - 1 + extended_size1 - len_original_lines
                                extended_size1 = max(extended_size1 - delta_cap, size1)
                                extended_size2 = max(extended_size2 - delta_cap, size2)
                            return extended_start1, extended_size1, extended_start2, extended_size2

                        if allow_dynamic_context and file_new_lines:
                            extended_start1, extended_size1, extended_start2, extended_size2 = \
                                _calc_context_limits(patch_extra_lines_before_dynamic)

                            lines_before_original = file_original_lines[extended_start1 - 1:start1 - 1]
                            lines_before_new = file_new_lines[extended_start2 - 1:start2 - 1]
                            found_header = False
                            for i, line in enumerate(lines_before_original):
                                if section_header in line:
                                    # Update start and size in one line each
                                    extended_start1, extended_start2 = extended_start1 + i, extended_start2 + i
                                    extended_size1, extended_size2 = extended_size1 - i, extended_size2 - i
                                    lines_before_original_dynamic_context = lines_before_original[i:]
                                    lines_before_new_dynamic_context = lines_before_new[i:]
                                    if lines_before_original_dynamic_context == lines_before_new_dynamic_context:
                                        # get_logger().debug(f"found dynamic context match for section header: {section_header}")
                                        found_header = True
                                        section_header = ''
                                    else:
                                        pass  # its ok to be here. We can't apply dynamic context if the lines are different if 'old' and 'new' hunks
                                    break

                            if not found_header:
                                # get_logger().debug(f"Section header not found in the extra lines before the hunk")
                                extended_start1, extended_size1, extended_start2, extended_size2 = \
                                    _calc_context_limits(patch_extra_lines_before)
                        else:
                            extended_start1, extended_size1, extended_start2, extended_size2 = \
                                _calc_context_limits(patch_extra_lines_before)

                        # check if extra lines before hunk are different in original and new file
                        delta_lines_original = [f' {line}' for line in file_original_lines[extended_start1 - 1:start1 - 1]]
                        if file_new_lines:
                            delta_lines_new = [f' {line}' for line in file_new_lines[extended_start2 - 1:start2 - 1]]
                            if delta_lines_original != delta_lines_new:
                                found_mini_match = False
                                for i in range(len(delta_lines_original)):
                                    if delta_lines_original[i:] == delta_lines_new[i:]:
                                        delta_lines_original = delta_lines_original[i:]
                                        delta_lines_new = delta_lines_new[i:]
                                        extended_start1 += i
                                        extended_size1 -= i
                                        extended_start2 += i
                                        extended_size2 -= i
                                        found_mini_match = True
                                        break
                                if not found_mini_match:
                                    extended_start1 = start1
                                    extended_size1 = size1
                                    extended_start2 = start2
                                    extended_size2 = size2
                                    delta_lines_original = []
                                    # get_logger().debug(f"Extra lines before hunk are different in original and new file",
                                    #                    artifact={"delta_lines_original": delta_lines_original,
                                    #                              "delta_lines_new": delta_lines_new})

                        #  logic to remove section header if its in the extra delta lines (in dynamic context, this is also done)
                        if section_header and not allow_dynamic_context:
                            for line in delta_lines_original:
                                if section_header in line:
                                    section_header = ''  # remove section header if it is in the extra delta lines
                                    break
                    else:
                        extended_start1 = start1
                        extended_size1 = size1
                        extended_start2 = start2
                        extended_size2 = size2
                        delta_lines_original = []
                    extended_patch_lines.append('')
                    extended_patch_lines.append(
                        f'@@ -{extended_start1},{extended_size1} '
                        f'+{extended_start2},{extended_size2} @@ {section_header}')
                    extended_patch_lines.extend(delta_lines_original)  # one to zero based
                    continue
            extended_patch_lines.append(line)
    except Exception as e:
        get_logger().warning(f"Failed to extend patch: {e}", artifact={"traceback": traceback.format_exc()})
        return patch_str

    # finish processing last hunk
    if start1 != -1 and patch_extra_lines_after > 0 and is_valid_hunk:
        delta_lines_original = file_original_lines[start1 + size1 - 1:start1 + size1 - 1 + patch_extra_lines_after]
        # add space at the beginning of each extra line
        delta_lines_original = [f' {line}' for line in delta_lines_original]
        extended_patch_lines.extend(delta_lines_original)

    extended_patch_str = '\n'.join(extended_patch_lines)
    return extended_patch_str

def check_if_hunk_lines_matches_to_file(i, original_lines, patch_lines, start1):
    """
    Check if the hunk lines match the original file content. We saw cases where the hunk header line doesn't match the original file content, and then
    extending the hunk with extra lines before the hunk header can cause the hunk to be invalid.
    """
    is_valid_hunk = True
    try:
        if i + 1 < len(patch_lines) and patch_lines[i + 1][0] == ' ': # an existing line in the file
            if patch_lines[i + 1].strip() != original_lines[start1 - 1].strip():
                # check if different encoding is needed
                original_line = original_lines[start1 - 1].strip()
                for encoding in ['iso-8859-1', 'latin-1', 'ascii', 'utf-16']:
                    try:
                        if original_line.encode(encoding).decode().strip() == patch_lines[i + 1].strip():
                            get_logger().info(f"Detected different encoding in hunk header line {start1}, needed encoding: {encoding}")
                            return False # we still want to avoid extending the hunk. But we don't want to log an error
                    except:
                        pass

                is_valid_hunk = False
                get_logger().info(
                    f"Invalid hunk in PR, line {start1} in hunk header doesn't match the original file content")
    except:
        pass
    return is_valid_hunk


def extract_hunk_headers(match):
    res = list(match.groups())
    for i in range(len(res)):
        if res[i] is None:
            res[i] = 0
    try:
        start1, size1, start2, size2 = map(int, res[:4])
    except:  # '@@ -0,0 +1 @@' case
        start1, size1, size2 = map(int, res[:3])
        start2 = 0
    section_header = res[4]
    return section_header, size1, size2, start1, start2


def omit_deletion_hunks(patch_lines) -> str:
    """
    Omit deletion hunks from the patch and return the modified patch.
    Args:
    - patch_lines: a list of strings representing the lines of the patch
    Returns:
    - A string representing the modified patch with deletion hunks omitted
    """

    temp_hunk = []
    added_patched = []
    add_hunk = False
    inside_hunk = False

    for line in patch_lines:
        if line.startswith('@@'):
            match = RE_HUNK_HEADER.match(line)
            if match:
                # finish previous hunk
                if inside_hunk and add_hunk:
                    added_patched.extend(temp_hunk)
                    temp_hunk = []
                    add_hunk = False
                temp_hunk.append(line)
                inside_hunk = True
        else:
            temp_hunk.append(line)
            if line:
                edit_type = line[0]
                if edit_type == '+':
                    add_hunk = True
    if inside_hunk and add_hunk:
        added_patched.extend(temp_hunk)

    return '\n'.join(added_patched)


def handle_patch_deletions(patch: str, original_file_content_str: str,
                           new_file_content_str: str, file_name: str, edit_type: EDIT_TYPE = EDIT_TYPE.UNKNOWN) -> str:
    """
    Handle entire file or deletion patches.

    This function takes a patch, original file content, new file content, and file name as input.
    It handles entire file or deletion patches and returns the modified patch with deletion hunks omitted.

    Args:
        patch (str): The patch to be handled.
        original_file_content_str (str): The original content of the file.
        new_file_content_str (str): The new content of the file.
        file_name (str): The name of the file.

    Returns:
        str: The modified patch with deletion hunks omitted.

    """
    if not new_file_content_str and (edit_type == EDIT_TYPE.DELETED or edit_type == EDIT_TYPE.UNKNOWN):
        # logic for handling deleted files - don't show patch, just show that the file was deleted
        if get_settings().config.verbosity_level > 0:
            get_logger().info(f"Processing file: {file_name}, minimizing deletion file")
        patch = None # file was deleted
    else:
        patch_lines = patch.splitlines()
        patch_new = omit_deletion_hunks(patch_lines)
        if patch != patch_new:
            if get_settings().config.verbosity_level > 0:
                get_logger().info(f"Processing file: {file_name}, hunks were deleted")
            patch = patch_new
    return patch


def decouple_and_convert_to_hunks_with_lines_numbers(patch: str, file) -> str:
    """
    Convert a given patch string into a string with line numbers for each hunk, indicating the new and old content of
    the file.

    Args:
        patch (str): The patch string to be converted.
        file: An object containing the filename of the file being patched.

    Returns:
        str: A string with line numbers for each hunk, indicating the new and old content of the file.

    example output:
## src/file.ts
__new hunk__
881        line1
882        line2
883        line3
887 +      line4
888 +      line5
889        line6
890        line7
...
__old hunk__
        line1
        line2
-       line3
-       line4
        line5
        line6
           ...
    """

    # Add a header for the file
    if file:
        # if the file was deleted, return a message indicating that the file was deleted
        if hasattr(file, 'edit_type') and file.edit_type == EDIT_TYPE.DELETED:
            return f"\n\n## File '{file.filename.strip()}' was deleted\n"

        patch_with_lines_str = f"\n\n## File: '{file.filename.strip()}'\n"
    else:
        patch_with_lines_str = ""

    patch_lines = patch.splitlines()
    new_content_lines = []
    old_content_lines = []
    match = None
    start1, size1, start2, size2 = -1, -1, -1, -1
    prev_header_line = []
    header_line = []
    for line_i, line in enumerate(patch_lines):
        if 'no newline at end of file' in line.lower():
            continue

        if line.startswith('@@'):
            header_line = line
            match = RE_HUNK_HEADER.match(line)
            if match and (new_content_lines or old_content_lines):  # found a new hunk, split the previous lines
                if prev_header_line:
                    patch_with_lines_str += f'\n{prev_header_line}\n'
                is_plus_lines = is_minus_lines = False
                if new_content_lines:
                    is_plus_lines = any([line.startswith('+') for line in new_content_lines])
                if old_content_lines:
                    is_minus_lines = any([line.startswith('-') for line in old_content_lines])
                if is_plus_lines or is_minus_lines: # notice 'True' here - we always present __new hunk__ for section, otherwise LLM gets confused
                    patch_with_lines_str = patch_with_lines_str.rstrip() + '\n__new hunk__\n'
                    for i, line_new in enumerate(new_content_lines):
                        patch_with_lines_str += f"{start2 + i} {line_new}\n"
                if is_minus_lines:
                    patch_with_lines_str = patch_with_lines_str.rstrip() + '\n__old hunk__\n'
                    for line_old in old_content_lines:
                        patch_with_lines_str += f"{line_old}\n"
                new_content_lines = []
                old_content_lines = []
            if match:
                prev_header_line = header_line

            section_header, size1, size2, start1, start2 = extract_hunk_headers(match)

        elif line.startswith('+'):
            new_content_lines.append(line)
        elif line.startswith('-'):
            old_content_lines.append(line)
        else:
            if not line and line_i: # if this line is empty and the next line is a hunk header, skip it
                if line_i + 1 < len(patch_lines) and patch_lines[line_i + 1].startswith('@@'):
                    continue
                elif line_i + 1 == len(patch_lines):
                    continue
            new_content_lines.append(line)
            old_content_lines.append(line)

    # finishing last hunk
    if match and new_content_lines:
        patch_with_lines_str += f'\n{header_line}\n'
        is_plus_lines = is_minus_lines = False
        if new_content_lines:
            is_plus_lines = any([line.startswith('+') for line in new_content_lines])
        if old_content_lines:
            is_minus_lines = any([line.startswith('-') for line in old_content_lines])
        if is_plus_lines or is_minus_lines:  # notice 'True' here - we always present __new hunk__ for section, otherwise LLM gets confused
            patch_with_lines_str = patch_with_lines_str.rstrip() + '\n__new hunk__\n'
            for i, line_new in enumerate(new_content_lines):
                patch_with_lines_str += f"{start2 + i} {line_new}\n"
        if is_minus_lines:
            patch_with_lines_str = patch_with_lines_str.rstrip() + '\n__old hunk__\n'
            for line_old in old_content_lines:
                patch_with_lines_str += f"{line_old}\n"

    return patch_with_lines_str.rstrip()


def extract_hunk_lines_from_patch(patch: str, file_name, line_start, line_end, side, remove_trailing_chars: bool = True) -> tuple[str, str]:
    try:
        patch_with_lines_str = f"\n\n## File: '{file_name.strip()}'\n\n"
        selected_lines = ""
        patch_lines = patch.splitlines()
        match = None
        start1, size1, start2, size2 = -1, -1, -1, -1
        skip_hunk = False
        selected_lines_num = 0
        for line in patch_lines:
            if 'no newline at end of file' in line.lower():
                continue

            if line.startswith('@@'):
                skip_hunk = False
                selected_lines_num = 0
                header_line = line

                match = RE_HUNK_HEADER.match(line)

                section_header, size1, size2, start1, start2 = extract_hunk_headers(match)

                # check if line range is in this hunk
                if side.lower() == 'left':
                    # check if line range is in this hunk
                    if not (start1 <= line_start <= start1 + size1):
                        skip_hunk = True
                        continue
                elif side.lower() == 'right':
                    if not (start2 <= line_start <= start2 + size2):
                        skip_hunk = True
                        continue
                patch_with_lines_str += f'\n{header_line}\n'

            elif not skip_hunk:
                if side.lower() == 'right' and line_start <= start2 + selected_lines_num <= line_end:
                    selected_lines += line + '\n'
                if side.lower() == 'left' and start1 <= selected_lines_num + start1 <= line_end:
                    selected_lines += line + '\n'
                patch_with_lines_str += line + '\n'
                if not line.startswith('-'): # currently we don't support /ask line for deleted lines
                    selected_lines_num += 1
    except Exception as e:
        get_logger().error(f"Failed to extract hunk lines from patch: {e}", artifact={"traceback": traceback.format_exc()})
        return "", ""

    if remove_trailing_chars:
        patch_with_lines_str = patch_with_lines_str.rstrip()
        selected_lines = selected_lines.rstrip()

    return patch_with_lines_str, selected_lines
