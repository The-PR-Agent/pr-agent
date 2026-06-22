import os
from collections import Counter
from typing import List, Optional

from unidiff.errors import UnidiffParseError

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import _find_repository_root, get_settings
from pr_agent.git_providers.diff_parsing import (parse_unified_diff,
                                                 reconstruct_base_file)
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger


class PullRequestMimic:
    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


class DiffGitProvider(GitProvider):
    """Tokenless provider that reviews a raw unified diff (stdin/file).

    The diff text and optional output path are read from global settings
    (diff.content, diff.output_path). The pr_url arg is an ignored sentinel.
    """

    def __init__(self, pr_url=None, incremental=False):
        diff_text = get_settings().get("diff.content", None)
        if not diff_text or not str(diff_text).strip():
            raise ValueError("No diff content provided for the 'diff' git provider")
        self.diff_text = diff_text
        self.output_path = get_settings().get("diff.output_path", None)
        self.diff_files = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())
        # inline code comments are not supported for the diff provider
        get_settings().pr_reviewer.inline_code_comments = False

    def get_diff_files(self) -> List[FilePatchInfo]:
        if self.diff_files is not None:
            return self.diff_files
        try:
            files = parse_unified_diff(self.diff_text)
        except UnidiffParseError as e:
            raise ValueError(f"Failed to parse the provided diff: {e}") from e
        # Resolve diff paths against the actual repository root (not the raw CWD)
        # so working-tree enrichment still works when run from a subdirectory.
        repo_root = _find_repository_root()
        root = os.path.realpath(str(repo_root) if repo_root else os.getcwd())
        for f in files:
            head = ""
            if f.filename:
                if os.path.isabs(f.filename):
                    get_logger().info(
                        f"Skipping absolute path in diff (unsafe): {f.filename}"
                    )
                else:
                    candidate = os.path.realpath(os.path.join(root, f.filename))
                    if candidate != root and not candidate.startswith(root + os.sep):
                        get_logger().info(
                            f"Skipping path that escapes repo root (path traversal): {f.filename}"
                        )
                    elif os.path.isfile(candidate):
                        try:
                            with open(candidate, "r", encoding="utf-8") as fh:
                                head = fh.read()
                        except (OSError, UnicodeDecodeError) as e:
                            get_logger().info(f"Could not read working-tree file {f.filename}: {e}")
            f.head_file = head
            f.base_file = reconstruct_base_file(head, f.patch) if head else ""
        self.diff_files = files
        return files

    def get_files(self) -> List[str]:
        return [f.filename for f in self.get_diff_files()]

    def _write_output(self, content: str):
        print(content)
        if self.output_path:
            try:
                with open(self.output_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            except Exception as e:
                get_logger().error(f"Failed to write output to {self.output_path}: {e}")

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if is_temporary:
            return  # don't emit "Preparing review..." placeholders to stdout
        self._write_output(pr_comment)

    def publish_description(self, pr_title: str, pr_body: str):
        self._write_output(f"{pr_title}\n\n{pr_body}")

    def is_supported(self, capability: str) -> bool:
        if capability in ["get_issue_comments", "create_inline_comment",
                          "publish_inline_comments", "publish_file_comments",
                          "get_labels"]:
            return False
        return True

    def get_languages(self):
        files = [f.filename for f in self.get_diff_files() if f.filename]
        lang_count = Counter(os.path.splitext(name)[1].lstrip(".").lower() for name in files)
        total = sum(lang_count.values()) or 1
        return {lang: count / total * 100 for lang, count in lang_count.items()}

    def get_pr_title(self):
        return "Local diff review"

    def get_pr_description_full(self):
        return ""

    def get_user_id(self):
        return -1

    def get_pr_branch(self):
        return ""

    # ---- unsupported publish operations (no-op or NotImplementedError) ----
    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        raise NotImplementedError("Inline comments are not supported by the diff provider")

    def publish_inline_comments(self, comments: list):
        raise NotImplementedError("Inline comments are not supported by the diff provider")

    def publish_code_suggestion(self, body: str, relevant_file: str, relevant_lines_start: int, relevant_lines_end: int):
        raise NotImplementedError("Code suggestions are not supported by the diff provider")

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        raise NotImplementedError("Code suggestions are not supported by the diff provider")

    def publish_labels(self, labels):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        pass

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        pass

    def get_commit_messages(self):
        return ""

    def get_repo_settings(self):
        return None

    def get_issue_comments(self):
        raise NotImplementedError("Issue comments are not supported by the diff provider")

    def get_pr_labels(self, update=False):
        return []
