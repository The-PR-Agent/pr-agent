import io
import re

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from pr_agent.algo.git_patch_processing import to_hunk_only_patch as _to_hunk_only_patch
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.log import get_logger
from pr_agent.mosaico.diff_provider import _unquote_git_path


def to_hunk_only_patch(patch_str: str) -> str:
    """Keep the existing provider-layer import path for callers."""
    return _to_hunk_only_patch(patch_str)


def _strip_prefix(path: str | None) -> str | None:
    if path is None:
        return None
    path = _unquote_git_path(path)
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(?P<source_length>\d+))? \+\d+(?:,(?P<target_length>\d+))? @@")


def _normalize_metadata_line_endings(diff_text: str) -> str:
    """Normalize CRLF metadata lines while preserving hunk payload line endings."""
    normalized: list[str] = []
    hunk_lines_remaining = 0

    # Split only on LF, as Git does. Tracking hunk body length prevents a payload
    # line that begins with "--- " or "+++ " from being mistaken for a file header.
    for line in io.StringIO(diff_text, newline="\n"):
        if hunk_lines_remaining == 0 and line.endswith("\r\n"):
            line = line[:-2] + "\n"
        normalized.append(line)

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            source_length = int(hunk.group("source_length") or 1)
            target_length = int(hunk.group("target_length") or 1)
            hunk_lines_remaining = source_length + target_length
        elif hunk_lines_remaining and not line.startswith("\\"):
            # A context line counts toward both the source and the target length.
            hunk_lines_remaining -= 1 if line.startswith(("+", "-")) else 2

    return "".join(normalized)


def parse_unified_diff(diff_text: str) -> list[FilePatchInfo]:
    """Parse a unified diff into FilePatchInfo objects (patch + metadata only).

    base_file / head_file are left empty here; the provider fills them from the
    working tree and by reverse-applying the patch. Binary files are skipped.
    """
    # unidiff leaves CR from CRLF file headers in parsed paths, which can make a
    # modified file look like a rename. Normalize metadata framing only so hunk data
    # retains its original line endings.
    patch_set = PatchSet(_normalize_metadata_line_endings(diff_text))
    files: list[FilePatchInfo] = []
    for pf in patch_set:
        if pf.is_binary_file:
            get_logger().info(f"Skipping binary file in diff: {pf.path}")
            continue
        if pf.is_added_file:
            edit_type = EDIT_TYPE.ADDED
        elif pf.is_removed_file:
            edit_type = EDIT_TYPE.DELETED
        elif pf.is_rename:
            edit_type = EDIT_TYPE.RENAMED
        else:
            edit_type = EDIT_TYPE.MODIFIED

        if pf.is_removed_file:  # target is /dev/null: use source path as the name
            filename = _strip_prefix(pf.source_file)
        else:
            filename = _strip_prefix(pf.target_file)
        old_filename = _strip_prefix(pf.source_file) if pf.is_rename else None

        files.append(
            FilePatchInfo(
                base_file="",
                head_file="",
                patch=str(pf),
                filename=filename,
                edit_type=edit_type,
                old_filename=old_filename,
            )
        )
    return files


def reconstruct_base_file_checked(head_file_str: str, patch_str: str) -> str | None:
    """Reverse-apply a patch, returning None when it does not match the head file."""
    try:
        patch_set = PatchSet(patch_str)
    except UnidiffParseError as e:
        get_logger().info(f"Could not parse patch for base reconstruction: {e}")
        return None
    if len(patch_set) != 1:
        return None

    head_lines = head_file_str.splitlines()
    base_lines: list[str] = []
    head_idx = 0  # 0-based cursor into head_lines

    for hunk in patch_set[0]:
        # 1-based -> 0-based. A hunk with no head lines (a pure deletion, as in
        # `git diff -U0`, or a file emptied of all lines) numbers the line *before*
        # the change instead, so its start already is the 0-based insertion point.
        hunk_head_start = hunk.target_start if hunk.target_length == 0 else hunk.target_start - 1
        if hunk_head_start < head_idx or hunk_head_start > len(head_lines):
            return None  # out-of-order / out-of-bounds hunk
        base_lines.extend(head_lines[head_idx:hunk_head_start])
        head_idx = hunk_head_start

        for line in hunk:
            value = line.value.rstrip("\r\n")
            if line.is_context:
                if head_idx >= len(head_lines) or head_lines[head_idx] != value:
                    return None
                base_lines.append(head_lines[head_idx])
                head_idx += 1
            elif line.is_added:  # present only in head: verify + skip
                if head_idx >= len(head_lines) or head_lines[head_idx] != value:
                    return None
                head_idx += 1
            elif line.is_removed:  # present only in base: emit, don't consume head
                base_lines.append(value)

    base_lines.extend(head_lines[head_idx:])
    result = "\n".join(base_lines)
    # Preserve a trailing newline only when the base actually has content; an
    # empty base (e.g. reversing an add-file patch) must stay "" so downstream
    # extend_patch() correctly treats it as having no original file.
    if base_lines and head_file_str.endswith("\n"):
        result += "\n"
    return result


def reconstruct_base_file(head_file_str: str, patch_str: str) -> str:
    """Reverse-apply a patch, returning "" when it does not match the head file."""
    result = reconstruct_base_file_checked(head_file_str, patch_str)
    return result if result is not None else ""
