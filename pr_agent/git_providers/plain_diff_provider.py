import os
from collections import Counter
from typing import List, Optional

from unidiff.errors import UnidiffParseError

from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import _find_repository_root, get_settings
from pr_agent.git_providers.diff_parsing import parse_unified_diff, reconstruct_base_file, to_hunk_only_patch
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger


class PullRequestMimic:
    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


class PlainDiffGitProvider(GitProvider):
    """Provider that reviews a raw unified diff (stdin/file), no hosting platform.

    The diff text and optional output path are read from global settings
    (plain_diff.content, plain_diff.output_path). The pr_url arg is an ignored
    sentinel.
    """

    def __init__(self, pr_url=None, incremental=False):
        diff_text = get_settings().get("plain_diff.content", None)
        if not diff_text or not str(diff_text).strip():
            raise ValueError("No diff content provided for the 'plain-diff' git provider")
        self.diff_text = diff_text
        self.output_path = get_settings().get("plain_diff.output_path", None)
        # cli.run() already forces config.publish_output=True, but apply_repo_settings()
        # runs afterwards and can overwrite it back to False from an extra/repo config
        # (tools gate all publishing on this flag). This provider is constructed after
        # apply_repo_settings, so re-assert it here: stdout/--output is plain-diff mode's
        # only output channel, and it must never be silently suppressed.
        get_settings().set("config.publish_output", True)
        self.diff_files = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())

    def get_diff_files(self) -> List[FilePatchInfo]:
        if self.diff_files is not None:
            return self.diff_files
        try:
            files = parse_unified_diff(self.diff_text)
        except UnidiffParseError as e:
            raise ValueError(f"Failed to parse the provided diff: {e}") from e
        # Resolve diff paths against the actual repository root (not the raw CWD)
        # so working-tree enrichment still works when run from a subdirectory.
        # If there is no detectable .git root, disable enrichment entirely and
        # run patch-only: reading files from an arbitrary CWD could disclose
        # unrelated local files to the LLM.
        repo_root = _find_repository_root()
        root = os.path.realpath(str(repo_root)) if repo_root else None
        if root is None:
            get_logger().info(
                "No repository root (.git) found; running in patch-only mode "
                "(working-tree enrichment disabled)."
            )
        for f in files:
            head = ""
            if root is not None and f.filename:
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
            # Reconstruction needs the full patch (with --- /+++ headers); the
            # rest of the pipeline expects hunk-only patches, so normalize after.
            f.patch = to_hunk_only_patch(f.patch)
        self.diff_files = files
        return files

    def get_files(self) -> List[str]:
        return [f.filename for f in self.get_diff_files()]

    def get_incremental_commits(self, incremental):
        # A standalone diff has no commit history, so incremental review (-i) is
        # not applicable. Disable it explicitly to avoid a TypeError downstream
        # (PRReviewer would otherwise call len() on an unpopulated commits_range).
        if getattr(incremental, "is_incremental", False):
            get_logger().info(
                "Incremental review is not supported in plain-diff mode; "
                "running a full review instead."
            )
        incremental.is_incremental = False

    def _write_output(self, content: str):
        print(content)
        if self.output_path:
            # --output is always an explicit user request, so a write failure
            # must surface (fail fast) rather than be silently swallowed.
            try:
                with open(self.output_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            except (OSError, UnicodeError) as e:
                get_logger().error(f"Failed to write output to {self.output_path}: {e}")
                raise

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
        # Return {language-name: percentage}, matching the hosted providers.
        # sort_files_by_main_languages() keys on language NAMES (it maps each
        # name back to its extensions), so returning raw extensions here would
        # drop every file into the "Other" bucket and disable language-based
        # hunk prioritization. Invert the settings map (name -> [extensions])
        # into an extension -> name lookup; files with unknown extensions are
        # left out and fall through to "Other" downstream.
        ext_to_lang = {}
        lang_map = get_settings().get("language_extension_map_org", {}) or {}
        for language, extensions in lang_map.items():
            for ext in extensions:
                ext_to_lang.setdefault(ext.lower().lstrip("*"), language)

        lang_count = Counter()
        for f in self.get_diff_files():
            if not f.filename:
                continue
            language = ext_to_lang.get(os.path.splitext(f.filename)[1].lower())
            if language:
                lang_count[language] += 1

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

    # ---- code suggestions: rendered to stdout/--output (no hosting platform) ----
    def publish_code_suggestion(self, body: str, relevant_file: str,
                                relevant_lines_start: int, relevant_lines_end: int):
        location = f"{relevant_file}:{relevant_lines_start}-{relevant_lines_end}"
        self._write_output(f"### {location}\n\n{body}")

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        # The 'improve' tool calls this unconditionally; render the suggestions
        # as a single markdown document to stdout/--output instead of pushing
        # them to a (non-existent) hosting platform.
        if not code_suggestions:
            return True
        sections = ["## Code suggestions", ""]
        for s in code_suggestions:
            relevant_file = s.get("relevant_file", "")
            start = s.get("relevant_lines_start", "")
            end = s.get("relevant_lines_end", "")
            location = f"{relevant_file}:{start}-{end}".strip(":-")
            if location:
                sections.append(f"### {location}")
            sections.append(s.get("body", ""))
            sections.append("")
        self._write_output("\n".join(sections).rstrip() + "\n")
        return True

    # ---- unsupported publish operations (no-op or NotImplementedError) ----
    def publish_inline_comment(self, body: str, relevant_file: str,
                               relevant_line_in_file: str, original_suggestion=None):
        raise NotImplementedError("Inline comments are not supported by the plain-diff provider")

    def publish_inline_comments(self, comments: list):
        raise NotImplementedError("Inline comments are not supported by the plain-diff provider")

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
        # A raw diff has no issue-comment history. Return an empty iterable rather
        # than raising: the improve persistent-comment path calls this without a
        # capability guard, and treating "no comments" as the default lets it fall
        # through to publishing the suggestions to stdout without a spurious
        # traceback in the log.
        return []

    def get_pr_labels(self, update=False):
        return []
