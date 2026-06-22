from unidiff import PatchSet

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.log import get_logger


def _strip_prefix(path: str) -> str:
    if path is None:
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(diff_text: str) -> list[FilePatchInfo]:
    """Parse a unified diff into FilePatchInfo objects (patch + metadata only).

    base_file / head_file are left empty here; the provider fills them from the
    working tree and by reverse-applying the patch. Binary files are skipped.
    """
    patch_set = PatchSet(diff_text)
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


def reconstruct_base_file(head_file_str: str, patch_str: str) -> str:
    """Reverse-apply a single-file unified diff to head (new) content to recover
    base (original) content. Returns "" if the patch does not cleanly apply."""
    try:
        patch_set = PatchSet(patch_str)
    except Exception as e:
        get_logger().info(f"Could not parse patch for base reconstruction: {e}")
        return ""
    if len(patch_set) != 1:
        return ""

    head_lines = head_file_str.splitlines()
    base_lines: list[str] = []
    head_idx = 0  # 0-based cursor into head_lines

    for hunk in patch_set[0]:
        hunk_head_start = hunk.target_start - 1  # 1-based -> 0-based
        if hunk_head_start < head_idx or hunk_head_start > len(head_lines):
            return ""  # out-of-order / out-of-bounds hunk
        base_lines.extend(head_lines[head_idx:hunk_head_start])
        head_idx = hunk_head_start

        for line in hunk:
            value = line.value.rstrip("\n")
            if line.is_context:
                if head_idx >= len(head_lines) or head_lines[head_idx] != value:
                    return ""
                base_lines.append(head_lines[head_idx])
                head_idx += 1
            elif line.is_added:  # present only in head: verify + skip
                if head_idx >= len(head_lines) or head_lines[head_idx] != value:
                    return ""
                head_idx += 1
            elif line.is_removed:  # present only in base: emit, don't consume head
                base_lines.append(value)

    base_lines.extend(head_lines[head_idx:])
    result = "\n".join(base_lines)
    if head_file_str.endswith("\n") and result:
        result += "\n"
    return result
