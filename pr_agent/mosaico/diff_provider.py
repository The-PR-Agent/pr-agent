"""DiffInputProvider — a GitProvider that feeds a SUPPLIED unified diff to pr-agent's
tools (no host, no checkout). Used by the MOSAICO path (b): the inbound text is a
pasted unified diff. INPUT methods are real; publish/label/comment/reaction methods
are safe no-op stubs (with publish_output=False the tools render into
get_settings().data rather than calling the publish path).

The diff is parsed by parse_unified_diff(); per-request the parsed files/languages/title
are read from MOSAICO.INPUT on the (context) settings.
"""
import re
from typing import List, Optional

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider


class _PullRequestMimic:
    """Mimics the PullRequest object the tools touch (.title, .diff_files)."""

    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


_DIFF_GIT_RE = re.compile(r'^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$')


def parse_unified_diff(diff_text: str) -> List[FilePatchInfo]:
    """Parse a supplied unified diff (git format) into a list of FilePatchInfo.

    Splits on ``diff --git a/<f> b/<f>`` headers; per file: filename = the b/ path,
    patch = that file's section verbatim (the @@ hunk body pr-agent's hunk processing
    consumes), edit_type inferred from new/deleted/rename file modes, and head/base
    file content reconstructed best-effort from +/-/context lines. Degrades gracefully:
    a blob with no ``diff --git`` header yields []."""
    if not diff_text or not isinstance(diff_text, str):
        return []

    lines = diff_text.splitlines(keepends=True)
    # Find the start index of each "diff --git" section.
    starts = [i for i, ln in enumerate(lines) if _DIFF_GIT_RE.match(ln.rstrip("\n"))]
    if not starts:
        return []
    starts.append(len(lines))

    files: List[FilePatchInfo] = []
    for idx in range(len(starts) - 1):
        section = lines[starts[idx]:starts[idx + 1]]
        header = section[0].rstrip("\n")
        m = _DIFF_GIT_RE.match(header)
        a_path = m.group("a") if m else ""
        b_path = m.group("b") if m else ""

        patch = "".join(section)

        edit_type = EDIT_TYPE.MODIFIED
        old_filename = None
        for ln in section[1:]:
            s = ln.rstrip("\n")
            if s.startswith("new file mode"):
                edit_type = EDIT_TYPE.ADDED
            elif s.startswith("deleted file mode"):
                edit_type = EDIT_TYPE.DELETED
            elif s.startswith("rename from "):
                edit_type = EDIT_TYPE.RENAMED
                old_filename = s[len("rename from "):].strip()
            elif s.startswith("rename to "):
                edit_type = EDIT_TYPE.RENAMED
        if a_path != b_path and old_filename is None and a_path:
            old_filename = a_path

        # Best-effort reconstruct head/base file content from hunk lines.
        base_lines: List[str] = []
        head_lines: List[str] = []
        in_hunk = False
        for ln in section:
            if ln.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            # diff metadata lines that can appear mid-section
            if ln.startswith("\\ No newline"):
                continue
            if ln.startswith("+"):
                head_lines.append(ln[1:])
            elif ln.startswith("-"):
                base_lines.append(ln[1:])
            elif ln.startswith(" "):
                base_lines.append(ln[1:])
                head_lines.append(ln[1:])
            # other lines (e.g. index/+++/---) are ignored for content reconstruction

        filename = b_path or a_path
        files.append(FilePatchInfo(
            base_file="".join(base_lines),
            head_file="".join(head_lines),
            patch=patch,
            filename=filename,
            edit_type=edit_type,
            old_filename=old_filename,
        ))
    return files


class DiffInputProvider(GitProvider):
    """GitProvider over a supplied unified diff. Constructed like every other provider
    with a single positional ``pr_url`` arg (unused here); reads the parsed diff,
    languages and title from MOSAICO.INPUT on the (context) settings."""

    def __init__(self, pr_url: Optional[str] = None):
        self.pr_url = pr_url
        mosaico_input = get_settings().get("MOSAICO.INPUT", {}) or {}
        self.diff_files: List[FilePatchInfo] = list(mosaico_input.get("files", []) or [])
        self._languages = dict(mosaico_input.get("languages", {}) or {})
        self._title = mosaico_input.get("title", "") or ""
        self.pr = _PullRequestMimic(self._title, self.diff_files)

    # ---- INPUT methods (real) ----
    def is_supported(self, capability: str) -> bool:
        # Steer tools to the simplest render branches (no gfm table, no inline comments, no labels).
        return False

    def get_diff_files(self) -> List[FilePatchInfo]:
        return self.diff_files

    def get_files(self) -> list:
        return [f.filename for f in self.diff_files]

    def get_languages(self):
        return self._languages

    def get_pr_branch(self):
        return ""

    def get_commit_messages(self):
        return ""

    def get_pr_description_full(self) -> str:
        return ""

    def get_user_id(self):
        return -1

    def get_repo_settings(self):
        return ""

    # ---- publish / label / comment / reaction (safe no-op stubs) ----
    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return True

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        pass

    def publish_inline_comments(self, comments: list):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def get_issue_comments(self):
        return []

    def publish_labels(self, labels):
        pass

    def get_pr_labels(self, update=False):
        return []

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False):
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True
