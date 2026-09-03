from __future__ import annotations

import datetime as _dt
import json
import os
import re
from types import SimpleNamespace
from typing import Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

from ..algo.file_filter import filter_ignored
from ..algo.inline_comment_dedup import (
    body_with_markers,
    code_fingerprint,
    extract_suggestion_code,
    full_body_fingerprint,
    get_inline_comment_store,
    has_marker,
)
from ..algo.language_handler import is_valid_file
from ..algo.utils import (
    PRCodeSuggestionsIdentity,
    PRDescriptionHeader,
    add_comment_identity,
    comment_matches_any_identity,
    comment_matches_identity,
    find_line_number_of_relevant_line_in_file,
    format_pr_code_suggestions_header,
    get_pr_review_comment_identifiers,
    load_large_diff,
)
from ..config_loader import get_settings, get_verbosity_level
from ..log import get_logger
from .git_provider import GitProvider, IncrementalPR

AZURE_DEVOPS_AVAILABLE = True
ADO_APP_CLIENT_DEFAULT_ID = "499b84ac-1321-427f-aa17-267ca6975798/.default"
AZURE_AGENT_RESPONSE_MARKER = "<!-- pr-agent-response -->"
AZURE_AGENT_PROGRESS_MARKER = "<!-- pr-agent-progress -->"
MAX_PR_DESCRIPTION_AZURE_LENGTH = 4000-1
_FALLBACK_SUGGESTION_PATH_RE = re.compile(
    r"^`(?P<path>[^`]+)` \(lines (?P<start>\d+)-(?P<end>\d+)\)",
    re.MULTILINE,
)
_SUGGESTIONS_HEADER_PREFIX = "## PR Code Suggestions"
_FALLBACK_SUGGESTIONS_HEADER = "## Unanchored Code Suggestions"
_MAX_DISCUSSION_CONTEXT_CHARS = 24000
_MAX_DISCUSSION_REPLIES = 10
_MAX_DISCUSSION_THREADS = 50
_MAX_DISCUSSION_MESSAGE_CHARS = 750


def _is_not_found_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return status_code == 404
    return re.search(r"\b404\b", str(error)) is not None


try:
    # noinspection PyUnresolvedReferences
    from azure.devops.connection import Connection

    # noinspection PyUnresolvedReferences
    from azure.devops.released.git import (
        Comment,
        CommentPosition,
        CommentThread,
        CommentThreadContext,
        GitClient,
        GitPullRequest,
        GitVersionDescriptor,
    )
    from azure.devops.released.work_item_tracking import WorkItemTrackingClient

    # noinspection PyUnresolvedReferences
    from azure.identity import DefaultAzureCredential
    from msrest.authentication import BasicAuthentication
except ImportError:
    AZURE_DEVOPS_AVAILABLE = False


def _to_naive_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return dt


class _AzureCommitInner:
    def __init__(self, raw):
        self.message = getattr(raw, "comment", "") or ""
        author = getattr(raw, "author", None)
        author_date = _to_naive_utc(getattr(author, "date", None)) if author else None
        self.author = type("_AzureAuthor", (), {"date": author_date})()


class _AzureCommitAdapter:
    """Mimics PyGithub `Commit` shape (.sha, .commit.author.date, .commit.message, .parents)."""

    def __init__(self, raw):
        self.sha = raw.commit_id
        self.commit_id = raw.commit_id
        self.commit = _AzureCommitInner(raw)
        self.parents = list(getattr(raw, "parents", None) or [])


def _get_azure_change_path(change):
    try:
        item = change["item"]
    except (KeyError, TypeError):
        item = getattr(change, "item", None)
        if item is None:
            additional = getattr(change, "additional_properties", None) or {}
            item = additional.get("item") or {}

    if isinstance(item, dict):
        if item.get("gitObjectType", item.get("git_object_type")) == "tree":
            return None
        return item.get("path")

    if getattr(item, "git_object_type", getattr(item, "gitObjectType", None)) == "tree":
        return None
    return getattr(item, "path", None)


class AzureDevopsProvider(GitProvider):

    _INCREMENTAL_ANCHOR_PREFIXES = {
        "review": get_pr_review_comment_identifiers(full=True, incremental=True),
        "suggestions": (
            PRCodeSuggestionsIdentity.SUMMARY.value,
            PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
            PRCodeSuggestionsIdentity.UNANCHORED.value,
            _SUGGESTIONS_HEADER_PREFIX,
            _FALLBACK_SUGGESTIONS_HEADER,
            "**Suggestion:**",
        ),
    }
    _AGENT_COMMENT_IDENTIFIERS = get_pr_review_comment_identifiers(full=True, incremental=True) + (
        PRCodeSuggestionsIdentity.SUMMARY.value,
        PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
        PRCodeSuggestionsIdentity.UNANCHORED.value,
        _SUGGESTIONS_HEADER_PREFIX,
        _FALLBACK_SUGGESTIONS_HEADER,
    )

    def __init__(
            self, pr_url: Optional[str] = None, incremental: Optional[bool] = False
    ):
        if not AZURE_DEVOPS_AVAILABLE:
            raise ImportError(
                "Azure DevOps provider is not available. Please install the required dependencies."
            )

        self.azure_devops_client, self.azure_devops_board_client = self._get_azure_devops_client()
        self.diff_files = None
        self._diff_path_map = None
        self.workspace_slug = None
        self.repo_slug = None
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.temp_comments = []
        self.incremental = incremental
        self.unreviewed_files_map = {}
        self.pr_commits = None
        self.previous_review = None
        self._published_inline_comment_bodies = []
        self._inline_comment_store = None
        self._threads_cache = None
        if pr_url:
            self.set_pr(pr_url)

    def _resolve_diff_file_path(self, relevant_file: str) -> Optional[str]:
        if not isinstance(relevant_file, str) or not relevant_file.strip():
            return None
        relevant_file = relevant_file.strip().strip("`").strip()
        if not relevant_file:
            return None
        path_map = getattr(self, "_diff_path_map", None)
        if path_map is None or getattr(self, "diff_files", None) is None:
            diff_paths = [f.filename for f in (self.get_diff_files() or []) if f.filename]
            path_map = {path: path for path in diff_paths}
            for path in diff_paths:
                path_map.setdefault(path.lstrip("/"), path)
            if getattr(self, "diff_files", None) is not None:
                self._diff_path_map = path_map
        return path_map.get(relevant_file) or path_map.get(relevant_file.lstrip("/"))

    def _get_suggestion_end_offset(self, relevant_file: str, relevant_lines_end: int) -> Optional[int]:
        for file in self.diff_files or []:
            if file.filename != relevant_file or not isinstance(file.head_file, str):
                continue
            lines = file.head_file.splitlines()
            if relevant_lines_end > len(lines):
                return None
            line = lines[relevant_lines_end - 1]
            return len(line.encode("utf-16-le")) // 2 + 1
        return None

    @staticmethod
    def _fallback_suggestion_section(suggestion: dict, reason: str) -> str:
        relevant_file = str(suggestion["relevant_file"]).strip().strip("`").strip().replace("`", "")
        location = (f"`{relevant_file}` "
                    f"(lines {suggestion['relevant_lines_start']}-{suggestion['relevant_lines_end']})")
        return f"{location} - {reason}\n\n{suggestion['body']}"

    @staticmethod
    def _fallback_suggestion_body(section: str, match: re.Match) -> str:
        """Extract the suggestion body from a fallback section."""
        body_start = section.find("\n\n", match.end())
        if body_start == -1:
            return section
        return section[body_start + 2:]

    def _publish_fallback_suggestions(self, suggestions: list[tuple[dict, str]]) -> list[dict]:
        prepared = []
        for suggestion, reason in suggestions:
            try:
                prepared.append((suggestion, self._fallback_suggestion_section(suggestion, reason)))
            except (KeyError, TypeError) as e:
                get_logger().warning(f"Could not format Azure code suggestion fallback, error: {e}")
        if not prepared:
            return []
        header = add_comment_identity(
            format_pr_code_suggestions_header(),
            PRCodeSuggestionsIdentity.UNANCHORED.value,
        )
        try:
            self.publish_comment(
                f"{header}\n\n" + "\n\n---\n\n".join(
                    section for _, section in prepared
                )
            )
        except Exception as e:
            get_logger().exception(f"Azure failed to publish code suggestion fallback, error: {e}")
            published = []
            for suggestion, section in prepared:
                try:
                    self.publish_comment(f"{header}\n\n{section}")
                except Exception as fallback_error:
                    get_logger().exception(
                        f"Azure failed to publish code suggestion fallback, error: {fallback_error}")
                else:
                    published.append(suggestion)
            return published
        return [suggestion for suggestion, _ in prepared]

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as comments on the PR.
        """
        publishable_count = 0
        published_count = 0
        skipped_duplicate_count = 0
        fallback_suggestions = []
        dedup_enabled = get_settings().get("config.persistent_inline_comments", False)
        store = get_inline_comment_store(self) if dedup_enabled else None
        local_fingerprints = set()
        status = get_settings().azure_devops.get("default_comment_status", "closed")
        for suggestion in code_suggestions:
            try:
                body = suggestion["body"]
                relevant_file = suggestion["relevant_file"]
                relevant_lines_start = suggestion["relevant_lines_start"]
                relevant_lines_end = suggestion["relevant_lines_end"]
            except (KeyError, TypeError) as e:
                get_logger().warning(f"Could not parse Azure code suggestion, error: {e}")
                continue

            if (not isinstance(body, str) or not isinstance(relevant_file, str)
                    or not isinstance(relevant_lines_start, int) or isinstance(relevant_lines_start, bool)
                    or not isinstance(relevant_lines_end, int) or isinstance(relevant_lines_end, bool)):
                get_logger().warning("Could not parse Azure code suggestion, invalid value types")
                continue

            if not relevant_file.strip().strip("`").strip():
                get_logger().warning("Could not parse Azure code suggestion, relevant_file is empty")
                continue

            if relevant_lines_start < 1:
                get_logger().warning(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}")
                continue

            if relevant_lines_end < relevant_lines_start:
                get_logger().warning(f"Failed to publish code suggestion, "
                                       f"relevant_lines_end is {relevant_lines_end} and "
                                       f"relevant_lines_start is {relevant_lines_start}")
                continue

            publishable_count += 1
            fallback_to_pr_comment = suggestion.get("fallback_to_pr_comment", True)
            fingerprint_file = relevant_file.strip().strip("`").strip().lstrip("/")
            fingerprint_anchor = f"{relevant_lines_start}-{relevant_lines_end}"
            body_fp = None
            code_fp = None
            published_body = body
            should_mark = store is not None and not has_marker(body)
            fingerprints = set()
            if should_mark:
                body_fp = full_body_fingerprint(fingerprint_file, fingerprint_anchor, body)
                code_fp = code_fingerprint(fingerprint_file, fingerprint_anchor, body)
                fingerprints.add(body_fp)
                if code_fp is not None:
                    fingerprints.add(code_fp)
                if any(store.seen(fingerprint) or fingerprint in local_fingerprints
                       for fingerprint in fingerprints):
                    skipped_duplicate_count += 1
                    continue

            resolved_file = self._resolve_diff_file_path(relevant_file)
            if not resolved_file:
                if should_mark:
                    published_body = body_with_markers(body, body_fp, code_fp)
                if fallback_to_pr_comment:
                    local_fingerprints.update(fingerprints)
                    get_logger().warning(f"Could not match '{relevant_file}' to a file in the PR diff, "
                                         f"publishing the suggestion as a PR-level comment")
                    fallback_suggestion = dict(suggestion)
                    fallback_suggestion["body"] = published_body
                    fallback_suggestions.append(
                        (fallback_suggestion, "could not be anchored to a file in the PR diff")
                    )
                else:
                    get_logger().warning(f"Could not match '{relevant_file}' to a file in the PR diff")
                continue

            end_offset = 1
            if "```suggestion" in body:
                end_offset = self._get_suggestion_end_offset(resolved_file, relevant_lines_end)
                if end_offset is None:
                    if fallback_to_pr_comment:
                        if should_mark:
                            published_body = body_with_markers(body, body_fp, code_fp)
                            local_fingerprints.update(fingerprints)
                        get_logger().warning(
                            f"Could not resolve the full suggestion range in '{resolved_file}', "
                            f"publishing the suggestion as a PR-level comment")
                        fallback_suggestion = dict(suggestion)
                        fallback_suggestion["body"] = published_body
                        fallback_suggestions.append(
                            (fallback_suggestion, "could not resolve the complete line range in the PR diff"))
                    else:
                        get_logger().warning(
                            f"Could not resolve the full suggestion range in '{resolved_file}'")
                    continue

            if should_mark:
                published_body = body_with_markers(body, body_fp, code_fp)
                local_fingerprints.update(fingerprints)

            thread_context = CommentThreadContext(
                file_path=resolved_file,
                right_file_start=CommentPosition(offset=1, line=relevant_lines_start),
                right_file_end=CommentPosition(offset=end_offset, line=relevant_lines_end))
            comment = Comment(content=published_body, comment_type=1)
            thread = CommentThread(comments=[comment], thread_context=thread_context, status=status)
            try:
                self.azure_devops_client.create_thread(
                    comment_thread=thread,
                    project=self.workspace_slug,
                    repository_id=self.repo_slug,
                    pull_request_id=self.pr_num
                )
            except Exception as e:
                get_logger().exception(
                    "Azure failed to publish code suggestion, error: {error}",
                    error=e,
                    suggestion=suggestion,
                )
                if fallback_to_pr_comment:
                    fallback_suggestion = dict(suggestion)
                    fallback_suggestion["body"] = published_body
                    fallback_suggestions.append(
                        (fallback_suggestion, "could not be published as an inline comment")
                    )
            else:
                published_count += 1
                self._threads_cache = None
                if store is not None:
                    store.add(body_fp)
                    store.add(code_fp)
                    store.add_body(published_body)
                recent_bodies = getattr(self, "_published_inline_comment_bodies", None)
                if recent_bodies is None:
                    recent_bodies = []
                    self._published_inline_comment_bodies = recent_bodies
                if published_body not in recent_bodies:
                    recent_bodies.append(published_body)
        if fallback_suggestions:
            published_fallbacks = self._publish_fallback_suggestions(fallback_suggestions)
            published_count += len(published_fallbacks)
            if store is not None:
                for suggestion in published_fallbacks:
                    store.add_body(suggestion["body"])
        return published_count > 0 or publishable_count == skipped_duplicate_count

    def reply_to_comment_from_comment_id(self, thread_id: int, body: str, is_temporary: bool = False) -> Comment:
        return self.reply_to_thread(thread_id, body, is_temporary)

    def get_pr_description_full(self) -> str:
        return self.pr.description

    def edit_comment(self, comment: Comment, body: str):
        try:
            self.azure_devops_client.update_comment(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                thread_id=comment.thread_id,
                comment_id=comment.id,
                comment=Comment(content=body),
                project=self.workspace_slug,
            )
            self._threads_cache = None
            return True
        except Exception as e:
            get_logger().exception(f"Failed to edit comment, error: {e}")
            return False

    def remove_comment(self, comment: Comment):
        try:
            self.azure_devops_client.delete_comment(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                thread_id=comment.thread_id,
                comment_id=comment.id,
                project=self.workspace_slug,
            )
            self._threads_cache = None
        except Exception as e:
            get_logger().exception(f"Failed to remove comment, error: {e}")

    def publish_labels(self, pr_types):
        try:
            for pr_type in pr_types:
                self.azure_devops_client.create_pull_request_label(
                    label={"name": pr_type},
                    project=self.workspace_slug,
                    repository_id=self.repo_slug,
                    pull_request_id=self.pr_num,
                )
        except Exception as e:
            get_logger().warning(f"Failed to publish labels, error: {e}")

    def get_pr_labels(self, update=False):
        try:
            labels = self.azure_devops_client.get_pull_request_labels(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
            )
            return [label.name for label in labels]
        except Exception as e:
            get_logger().exception(f"Failed to get labels, error: {e}")
            return []

    def is_supported(self, capability: str) -> bool:
        return True

    def supports_incremental_kind(self, kind: str) -> bool:
        return kind in self._INCREMENTAL_ANCHOR_PREFIXES

    def supports_code_suggestion_state(self) -> bool:
        return True

    def supports_threaded_pr_questions(self) -> bool:
        return True

    def supports_line_question_history(self) -> bool:
        return True

    def set_pr(self, pr_url: str):
        self.diff_files = None
        self._diff_path_map = None
        self.pr_commits = None
        self.previous_review = None
        self.unreviewed_files_map = {}
        self.temp_comments = []
        self._published_inline_comment_bodies = []
        self._inline_comment_store = None
        self._threads_cache = None
        self.pr_url = pr_url
        self.workspace_slug, self.repo_slug, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def get_incremental_commits(self, incremental=None, kind: str = "review"):
        if incremental is None:
            incremental = IncrementalPR(False)
        self.incremental = incremental
        if self.incremental.is_incremental:
            # Recompute a diff cached for a different incremental scope.
            self.diff_files = None
            self._diff_path_map = None
            self.unreviewed_files_map = {}
            self._incremental_kind = kind
            self._get_incremental_commits()

    def _get_incremental_commits(self):
        if not self.pr_commits:
            raw = list(self.azure_devops_client.get_pull_request_commits(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
            ))
            # Azure returns newest-first; oldest-first matches GitHub iteration order.
            raw.reverse()
            self.pr_commits = [_AzureCommitAdapter(c) for c in raw]

        kind = getattr(self, "_incremental_kind", "review")
        prefixes = self._INCREMENTAL_ANCHOR_PREFIXES.get(kind, ())
        self.previous_review = self._find_incremental_anchor(prefixes)
        if not self.previous_review:
            get_logger().info(f"No previous {kind} comment found, will review the entire PR")
            self.incremental.is_incremental = False
            return

        self.incremental.commits_range = self._get_commit_range()
        if self.incremental.commits_range is None:
            return
        candidate_paths = []
        had_errors = False
        non_merge_seen = False
        for commit in self.incremental.commits_range:
            if len(commit.parents) > 1:
                get_logger().info(f"Skipping merge commit {commit.sha}")
                continue
            non_merge_seen = True
            try:
                changes_obj = self.azure_devops_client.get_changes(
                    project=self.workspace_slug,
                    repository_id=self.repo_slug,
                    commit_id=commit.commit_id,
                )
            except Exception as e:
                had_errors = True
                get_logger().warning(f"Failed to fetch changes for {commit.commit_id}: {e}")
                continue
            for change in (getattr(changes_obj, "changes", None) or []):
                path = _get_azure_change_path(change)
                if path:
                    candidate_paths.append(path)

        if candidate_paths:
            deduped = list(dict.fromkeys(candidate_paths))
            filtered = filter_ignored(deduped, "azure")
            for path in filtered:
                if is_valid_file(path):
                    self.unreviewed_files_map[path] = path
        elif had_errors and self.incremental.commits_range:
            get_logger().warning(
                "Failed to fetch changes for incremental commits; falling back to full review."
            )
            self.incremental.is_incremental = False
        elif self.incremental.commits_range and not non_merge_seen:
            get_logger().info(
                "Incremental range only contains merge commits; falling back to full review."
            )
            self.incremental.is_incremental = False

    def _get_commit_range(self):
        last_review_time = _to_naive_utc(getattr(self.previous_review, "created_at", None))
        if last_review_time is None or not self.pr_commits:
            get_logger().info(
                "Cannot compute incremental commit range "
                "(missing previous review timestamp or PR commits); falling back to full review."
            )
            self.incremental.is_incremental = False
            return None
        # Walk newest -> oldest to find the newest commit that predates the previous review
        # (the "last seen" baseline). The new range is then everything positioned after that
        # baseline, sliced by index — not by re-testing each commit's date — so that commits
        # with a missing author date (the adapter allows author.date to be None) are still
        # included rather than silently dropped from the incremental scope.
        last_seen_index = None
        saw_reliable_date = False
        for index in range(len(self.pr_commits) - 1, -1, -1):
            cdate = self.pr_commits[index].commit.author.date
            if cdate is None:
                continue
            saw_reliable_date = True
            if cdate <= last_review_time:
                last_seen_index = index
                self.incremental.last_seen_commit = self.pr_commits[index]
                break
        if not saw_reliable_date:
            get_logger().info(
                "All PR commit author dates are missing; cannot compute incremental range. "
                "Falling back to full review."
            )
            self.incremental.is_incremental = False
            return None
        # No commit predates the previous review, so there is no baseline to diff against.
        # Without it get_diff_files() cannot rebuild the incremental diff and would silently
        # fall back to the full PR diff while still claiming to be incremental — so degrade
        # explicitly to a full review instead.
        if last_seen_index is None:
            get_logger().info(
                "No PR commit predates the previous review (no last-seen baseline commit); "
                "falling back to full review."
            )
            self.incremental.is_incremental = False
            return None
        commits_range = self.pr_commits[last_seen_index + 1:]
        if commits_range:
            self.incremental.first_new_commit = commits_range[0]
        return commits_range

    def get_previous_review(self, *, full: bool, incremental: bool):
        if not (full or incremental):
            raise ValueError("At least one of full or incremental must be True")
        identifiers = get_pr_review_comment_identifiers(full=full, incremental=incremental)
        return self._find_incremental_anchor(identifiers)

    def _find_incremental_anchor(self, identifiers):
        if not identifiers:
            return None
        matches = []
        for comment in self.get_issue_comments():
            body = getattr(comment, "body", None)
            if body and comment_matches_any_identity(body, identifiers):
                matches.append(comment)
        if not matches:
            return None

        def anchor_time(comment):
            published = _to_naive_utc(getattr(comment, "published_date", None))
            updated = _to_naive_utc(getattr(comment, "last_updated_date", None))
            return max((value for value in (published, updated) if value is not None),
                       default=_dt.datetime.min)

        latest = max(
            matches,
            key=anchor_time,
        )
        latest.html_url = self.get_comment_url(latest)
        latest.created_at = anchor_time(latest)
        return latest

    def get_repo_settings(self):
        try:
            contents = self.azure_devops_client.get_item_content(
                repository_id=self.repo_slug,
                project=self.workspace_slug,
                download=False,
                include_content_metadata=False,
                include_content=True,
                path=".pr_agent.toml",
            )
            return b"".join(list(contents))
        except Exception as e:
            if get_verbosity_level() >= 2:
                get_logger().error(f"Failed to get repo settings, error: {e}")
            return ""

    def get_repo_file_content(self, file_path: str, from_default_branch: bool = False):
        try:
            # Read from the PR target (base) commit, matching the other providers. When
            # from_default_branch is requested, omit the version so the default branch is used.
            if from_default_branch:
                version = None
            else:
                version = GitVersionDescriptor(
                    version=self.pr.last_merge_target_commit.commit_id, version_type="commit"
                )
            item = self.azure_devops_client.get_item(
                repository_id=self.repo_slug,
                path=file_path,
                project=self.workspace_slug,
                version_descriptor=version,
                download=False,
                include_content=True,
            )
            return item.content or ""
        except Exception as e:
            if get_verbosity_level() >= 2:
                get_logger().warning(f"Failed to load repo file: {file_path}, error: {e}")
            if _is_not_found_error(e):
                return ""
            raise

    def get_files(self):
        if (isinstance(getattr(self, "incremental", None), IncrementalPR)
                and self.incremental.is_incremental
                and self.unreviewed_files_map):
            return list(self.unreviewed_files_map.keys())
        return self._get_files_full()

    def _get_files_full(self):
        files = []
        for i in self.azure_devops_client.get_pull_request_commits(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
        ):
            changes_obj = self.azure_devops_client.get_changes(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                commit_id=i.commit_id,
            )

            for c in (changes_obj.changes or []):
                path = _get_azure_change_path(c)
                if path:
                    files.append(path)
        return list(set(files))

    def get_diff_files(self) -> list[FilePatchInfo]:
        try:

            if self.diff_files is not None:
                return self.diff_files
            self._diff_path_map = None

            if self.pr.last_merge_commit is None or self.pr.last_merge_target_commit is None:
                get_logger().info(
                    f"PR {self.pr_num} has no last_merge_commit/last_merge_target_commit; "
                    f"cannot compute diff files."
                )
                self.diff_files = []
                return []

            base_sha = self.pr.last_merge_target_commit
            head_sha = self.pr.last_merge_commit

            # Get PR iterations
            iterations = self.azure_devops_client.get_pull_request_iterations(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                project=self.workspace_slug
            )
            changes = None
            if iterations:
                iteration_id = iterations[-1].id  # Get the last iteration (most recent changes)

                # Get changes for the iteration
                changes = self.azure_devops_client.get_pull_request_iteration_changes(
                    repository_id=self.repo_slug,
                    pull_request_id=self.pr_num,
                    iteration_id=iteration_id,
                    project=self.workspace_slug
                )
            diff_files = []
            diffs = []
            diff_types = {}
            if changes:
                for change in changes.change_entries:
                    item = change.additional_properties.get('item', {})
                    path = item.get('path', None)
                    if path:
                        diffs.append(path)
                        diff_types[path] = change.additional_properties.get('changeType', 'Unknown')

            # wrong implementation - gets all the files that were changed in any commit in the PR
            # commits = self.azure_devops_client.get_pull_request_commits(
            #     project=self.workspace_slug,
            #     repository_id=self.repo_slug,
            #     pull_request_id=self.pr_num,
            # )
            #
            # diff_files = []
            # diffs = []
            # diff_types = {}

            # for c in commits:
            #     changes_obj = self.azure_devops_client.get_changes(
            #         project=self.workspace_slug,
            #         repository_id=self.repo_slug,
            #         commit_id=c.commit_id,
            #     )
            #     for i in changes_obj.changes:
            #         if i["item"]["gitObjectType"] == "tree":
            #             continue
            #         diffs.append(i["item"]["path"])
            #         diff_types[i["item"]["path"]] = i["changeType"]
            #
            # diffs = list(set(diffs))

            diffs_original = diffs
            diffs = filter_ignored(diffs_original, "azure")
            if diffs_original != diffs:
                try:
                    get_logger().info("Filtered out [ignore] files for pull request:", extra=
                    {"files": diffs_original,  # diffs is just a list of names
                     "filtered_files": diffs})
                except Exception:
                    pass

            incremental_active = (
                isinstance(getattr(self, "incremental", None), IncrementalPR)
                and self.incremental.is_incremental
                and bool(self.unreviewed_files_map)
                and self.incremental.last_seen_commit_sha
            )
            if incremental_active:
                diffs = [f for f in diffs if f in self.unreviewed_files_map]

            invalid_files_names = []
            for file in diffs:
                if not is_valid_file(file):
                    invalid_files_names.append(file)
                    continue

                version = GitVersionDescriptor(
                    version=head_sha.commit_id, version_type="commit"
                )
                try:
                    new_file_content_str = self.azure_devops_client.get_item(
                        repository_id=self.repo_slug,
                        path=file,
                        project=self.workspace_slug,
                        version_descriptor=version,
                        download=False,
                        include_content=True,
                    )

                    new_file_content_str = new_file_content_str.content
                except Exception as error:
                    get_logger().error(
                        "Failed to retrieve new file content of {file} at version {version}",
                        file=file,
                        version=str(version),
                        error=error,
                    )
                    new_file_content_str = ""

                edit_type = EDIT_TYPE.MODIFIED
                if diff_types[file] == "add":
                    edit_type = EDIT_TYPE.ADDED
                elif diff_types[file] == "delete":
                    edit_type = EDIT_TYPE.DELETED
                elif "rename" in diff_types[file]: # diff_type can be `rename` | `edit, rename`
                    edit_type = EDIT_TYPE.RENAMED

                if edit_type == EDIT_TYPE.ADDED or edit_type == EDIT_TYPE.RENAMED:
                    original_file_content_str = ""
                elif incremental_active:
                    inc_version = GitVersionDescriptor(
                        version=self.incremental.last_seen_commit_sha, version_type="commit"
                    )
                    try:
                        inc_original = self.azure_devops_client.get_item(
                            repository_id=self.repo_slug,
                            path=file,
                            project=self.workspace_slug,
                            version_descriptor=inc_version,
                            download=False,
                            include_content=True,
                        )
                        original_file_content_str = inc_original.content or ""
                    except Exception as error:
                        get_logger().warning(
                            f"Failed to retrieve original of {file} at {self.incremental.last_seen_commit_sha}: {error}"
                        )
                        original_file_content_str = ""
                else:
                    base_version = GitVersionDescriptor(
                        version=base_sha.commit_id, version_type="commit"
                    )
                    try:
                        base_original = self.azure_devops_client.get_item(
                            repository_id=self.repo_slug,
                            path=file,
                            project=self.workspace_slug,
                            version_descriptor=base_version,
                            download=False,
                            include_content=True,
                        )
                        original_file_content_str = base_original.content
                    except Exception as error:
                        get_logger().error(
                            "Failed to retrieve original file content of {file} at version {version}",
                            file=file,
                            version=str(base_version),
                            error=error,
                        )
                        original_file_content_str = ""

                patch = load_large_diff(
                    file, new_file_content_str, original_file_content_str, show_warning=False
                ).rstrip()
                if incremental_active:
                    self.unreviewed_files_map[file] = patch

                # count number of lines added and removed
                patch_lines = patch.splitlines(keepends=True)
                num_plus_lines = len([line for line in patch_lines if line.startswith('+')])
                num_minus_lines = len([line for line in patch_lines if line.startswith('-')])

                diff_files.append(
                    FilePatchInfo(
                        original_file_content_str,
                        new_file_content_str,
                        patch=patch,
                        filename=file,
                        edit_type=edit_type,
                        num_plus_lines=num_plus_lines,
                        num_minus_lines=num_minus_lines,
                    )
                )
            get_logger().info(f"Invalid files: {invalid_files_names}")

            self.diff_files = diff_files
            return diff_files
        except Exception as e:
            get_logger().exception(f"Failed to get diff files, error: {e}")
            return []

    def publish_comment(self, pr_comment: str, is_temporary: bool = False, thread_context=None) -> Comment:
        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment: {pr_comment}")
            return None
        comment = Comment(content=pr_comment)

        status = get_settings().azure_devops.get("default_comment_status", "closed")
        thread = CommentThread(comments=[comment], thread_context=thread_context, status=status)
        thread_response = self.azure_devops_client.create_thread(
            comment_thread=thread,
            project=self.workspace_slug,
            repository_id=self.repo_slug,
            pull_request_id=self.pr_num,
        )
        self._threads_cache = None
        created_comment = thread_response.comments[0]
        created_comment.thread_id = thread_response.id
        if is_temporary:
            self.temp_comments.append(created_comment)
        return created_comment

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True,
                                   identity_marker: str | None = None,
                                   legacy_initial_header: str | None = None):
        return self.publish_persistent_comment_full(
            pr_comment,
            initial_header,
            update_header,
            name,
            final_update_message,
            identity_marker=identity_marker,
            legacy_initial_header=legacy_initial_header,
        )

    def supports_review_comment_identity(self) -> bool:
        return True

    def publish_description(self, pr_title: str, pr_body: str):
        if len(pr_body) > MAX_PR_DESCRIPTION_AZURE_LENGTH:

            usage_guide_text='<details> <summary><strong>✨ Describe tool usage guide:</strong></summary><hr>'
            ind = pr_body.find(usage_guide_text)
            if ind != -1:
                pr_body = pr_body[:ind]

            if len(pr_body) > MAX_PR_DESCRIPTION_AZURE_LENGTH:
                changes_walkthrough_text = PRDescriptionHeader.FILE_WALKTHROUGH.value
                ind = pr_body.find(changes_walkthrough_text)
                if ind != -1:
                    pr_body = pr_body[:ind]

            if len(pr_body) > MAX_PR_DESCRIPTION_AZURE_LENGTH:
                trunction_message = " ... (description truncated due to length limit)"
                pr_body = pr_body[:MAX_PR_DESCRIPTION_AZURE_LENGTH - len(trunction_message)] + trunction_message
                get_logger().warning("PR description was truncated due to length limit")
        try:
            updated_pr = GitPullRequest()
            if pr_title is not None:
                updated_pr.title = pr_title
            updated_pr.description = pr_body
            self.azure_devops_client.update_pull_request(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                git_pull_request_to_update=updated_pr,
            )
        except Exception as e:
            get_logger().exception(
                f"Could not update pull request {self.pr_num} description: {e}"
            )

    def remove_initial_comment(self):
        try:
            for comment in self.temp_comments:
                self.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        self.publish_inline_comments([self.create_inline_comment(body, relevant_file, relevant_line_in_file)])

    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                              absolute_position: int = None):
        clean_relevant_file = relevant_file.strip().strip("`").strip() if isinstance(relevant_file, str) else ""
        resolved_file = self._resolve_diff_file_path(clean_relevant_file)
        lookup_file = resolved_file or clean_relevant_file
        position, absolute_position = find_line_number_of_relevant_line_in_file(self.get_diff_files(),
                                                                                lookup_file,
                                                                                relevant_line_in_file,
                                                                                absolute_position)
        if position == -1:
            if get_verbosity_level() >= 2:
                get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"
        return dict(
            body=body,
            path=resolved_file,
            relevant_file=clean_relevant_file,
            position=position,
            absolute_position=absolute_position,
            subject_type=subject_type,
        )

    def publish_inline_comments(self, comments: list[dict], disable_fallback: bool = False):
            overall_success = True
            for comment in comments:
                if not comment:
                    continue
                try:
                    comment_body = comment["body"]
                    relevant_file = comment.get("relevant_file") or "unknown file"
                    thread_context = None
                    if comment.get("path"):
                        relevant_file = comment["path"]
                        thread_context = {"filePath": relevant_file}
                        if comment.get("subject_type", "LINE") == "LINE":
                            thread_context["rightFileStart"] = {
                                "line": comment["absolute_position"],
                                "offset": comment["position"],
                            }
                            thread_context["rightFileEnd"] = {
                                "line": comment["absolute_position"],
                                "offset": comment["position"],
                            }
                        body = comment_body
                    else:
                        relevant_file = relevant_file.replace("`", "")
                        get_logger().warning(f"Could not match '{relevant_file}' to a file in the PR diff, "
                                             f"publishing the comment as a PR-level comment")
                        body = (f"`{relevant_file}` - could not be anchored to a file in the PR diff\n\n"
                                f"{comment_body}")
                    self.publish_comment(body, thread_context=thread_context)
                    if get_verbosity_level() >= 2:
                        get_logger().info(
                            f"Published code suggestion on {self.pr_num} at {relevant_file}"
                        )
                except Exception as e:
                    if get_verbosity_level() >= 2:
                        get_logger().error(f"Failed to publish code suggestion, error: {e}")
                    overall_success = False
            return overall_success

    def get_title(self):
        return self.pr.title

    def get_languages(self):
        languages = []
        files = self.azure_devops_client.get_items(
            project=self.workspace_slug,
            repository_id=self.repo_slug,
            recursion_level="Full",
            include_content_metadata=True,
            include_links=False,
            download=False,
        )
        for f in files:
            if f.git_object_type == "blob":
                file_name, file_extension = os.path.splitext(f.path)
                languages.append(file_extension[1:])

        extension_counts = {}
        for ext in languages:
            if ext != "":
                extension_counts[ext] = extension_counts.get(ext, 0) + 1

        total_extensions = sum(extension_counts.values())

        extension_percentages = {
            ext: (count / total_extensions) * 100
            for ext, count in extension_counts.items()
        }

        return extension_percentages

    def get_pr_branch(self):
        pr_info = self.azure_devops_client.get_pull_request_by_id(
            project=self.workspace_slug, pull_request_id=self.pr_num
        )
        source_branch = pr_info.source_ref_name.split("/")[-1]
        return source_branch

    def get_user_id(self):
        return 0

    def get_persistent_comment_bodies(self) -> list[str]:
        bodies = list(getattr(self, "_published_inline_comment_bodies", []))
        for thread in self._get_threads():
            comments = self._value(thread, "comments") or []
            content = self._value(comments[0], "content") if comments else None
            if isinstance(content, str) and content and content not in bodies:
                bodies.append(content)
        return bodies

    def get_agent_mention_aliases(self) -> set[str]:
        aliases = set()
        configured = get_settings().get("azure_devops_server.agent_identity", "")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple, set)):
            aliases.update(value.strip() for value in configured if isinstance(value, str) and value.strip())
        if aliases:
            return aliases

        for thread in self._get_threads():
            for comment in self._value(thread, "comments") or []:
                content = self._value(comment, "content") or ""
                if not self._is_agent_comment(content):
                    continue
                author = self._value(comment, "author")
                for attribute, serialized_attribute in (
                    ("id", None),
                    ("display_name", "displayName"),
                    ("unique_name", "uniqueName"),
                ):
                    value = self._value(author, attribute, serialized_attribute)
                    if isinstance(value, str) and value.strip():
                        aliases.add(value.strip())
        return aliases

    @classmethod
    def _is_agent_comment(cls, content: str) -> bool:
        if not isinstance(content, str):
            return False
        if AZURE_AGENT_RESPONSE_MARKER in content:
            return True
        return comment_matches_any_identity(content.lstrip(), cls._AGENT_COMMENT_IDENTIFIERS)

    def get_code_suggestion_thread_context(self) -> str:
        discussions = []
        for thread in reversed(self._get_threads()):
            comments = self._value(thread, "comments") or []
            if not comments:
                continue
            root_body = self._value(comments[0], "content")
            if not isinstance(root_body, str):
                continue
            if "```suggestion" not in root_body:
                continue
            replies = []
            for comment in comments[1:][-_MAX_DISCUSSION_REPLIES:]:
                message = self._value(comment, "content")
                if not isinstance(message, str) or AZURE_AGENT_PROGRESS_MARKER in message:
                    continue
                message = message.replace(AZURE_AGENT_RESPONSE_MARKER, "").strip()
                if not message:
                    continue
                author = self._value(comment, "author")
                author_name = (self._value(author, "display_name", "displayName")
                               or self._value(author, "unique_name", "uniqueName")
                               or "Unknown")
                replies.append({
                    "author": author_name,
                    "message": message[:_MAX_DISCUSSION_MESSAGE_CHARS],
                })
            context = self._value(thread, "thread_context", "threadContext")
            path = self._value(context, "file_path", "filePath")
            start_position = self._value(context, "right_file_start", "rightFileStart")
            end_position = self._value(context, "right_file_end", "rightFileEnd") or start_position
            discussion = {
                "thread_id": self._value(thread, "id"),
                "status": self._value(thread, "status"),
                "file": path,
                "start_line": self._value(start_position, "line"),
                "end_line": self._value(end_position, "line"),
                "suggestion": root_body.split("<!-- pr-agent-", 1)[0].strip()[:_MAX_DISCUSSION_MESSAGE_CHARS],
                "replies": replies,
            }
            candidate = discussions + [discussion]
            if len(json.dumps(candidate, ensure_ascii=False)) > _MAX_DISCUSSION_CONTEXT_CHARS:
                break
            discussions = candidate
            if len(discussions) >= _MAX_DISCUSSION_THREADS:
                break
        return json.dumps(discussions, ensure_ascii=False, indent=2) if discussions else ""

    def get_existing_inline_comment_fingerprints(self) -> set[str]:
        fingerprints = set()
        for thread in self._get_threads():
            context = self._value(thread, "thread_context", "threadContext")
            path = self._value(context, "file_path", "filePath")
            start_position = self._value(context, "right_file_start", "rightFileStart")
            end_position = self._value(context, "right_file_end", "rightFileEnd") or start_position
            start_line = self._value(start_position, "line")
            end_line = self._value(end_position, "line")
            anchor = self._suggestion_range_anchor(start_line, end_line)
            comments = self._value(thread, "comments") or []
            content = self._value(comments[0], "content") if comments else None
            if not isinstance(content, str):
                continue
            if isinstance(path, str) and path and anchor:
                if "```suggestion" not in content:
                    continue
                self._add_suggestion_fingerprints(fingerprints, path, anchor, content)
                continue
            if ("```suggestion" not in content and not comment_matches_identity(
                    content, PRCodeSuggestionsIdentity.UNANCHORED.value)):
                continue
            for section in content.split("\n\n---\n\n"):
                match = _FALLBACK_SUGGESTION_PATH_RE.search(section)
                if match:
                    fallback_anchor = self._suggestion_range_anchor(
                        int(match["start"]), int(match["end"])
                    )
                    self._add_suggestion_fingerprints(
                        fingerprints, match["path"], fallback_anchor,
                        self._fallback_suggestion_body(section, match)
                    )
        return fingerprints

    @staticmethod
    def _suggestion_range_anchor(start_line, end_line) -> Optional[str]:
        if (not isinstance(start_line, int) or isinstance(start_line, bool)
                or not isinstance(end_line, int) or isinstance(end_line, bool)
                or start_line < 1 or end_line < start_line):
            return None
        return f"{start_line}-{end_line}"

    @staticmethod
    def _add_suggestion_fingerprints(fingerprints: set[str], path: str, anchor: str, body: str):
        path = path.lstrip("/")
        fingerprints.add(full_body_fingerprint(path, anchor, body))
        code_fp = code_fingerprint(path, anchor, body)
        if code_fp is not None:
            fingerprints.add(code_fp)

    @staticmethod
    def _value(value, attribute: str, serialized_attribute: Optional[str] = None):
        if isinstance(value, dict):
            return value.get(serialized_attribute or attribute)
        return getattr(value, attribute, None)

    def _get_threads(self):
        threads = getattr(self, "_threads_cache", None)
        if threads is None:
            threads = list(self.azure_devops_client.get_threads(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                project=self.workspace_slug,
            ) or [])
            self._threads_cache = threads
        return threads

    def get_recent_inline_comment_bodies(self) -> list[str]:
        return list(getattr(self, "_published_inline_comment_bodies", []))

    def get_review_thread_comments(self, comment_id: int) -> list[dict]:
        try:
            thread = self.azure_devops_client.get_pull_request_thread(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                thread_id=comment_id,
                project=self.workspace_slug,
            )
        except Exception as e:
            get_logger().warning(f"Failed to read Azure DevOps thread {comment_id}: {e}")
            return []

        thread_comments = list(self._value(thread, "comments") or [])
        if len(thread_comments) > _MAX_DISCUSSION_REPLIES + 1:
            thread_comments = thread_comments[:1] + thread_comments[-_MAX_DISCUSSION_REPLIES:]

        comments = []
        for comment in thread_comments:
            content = self._value(comment, "content")
            if not isinstance(content, str) or AZURE_AGENT_PROGRESS_MARKER in content:
                continue
            content = content.replace(AZURE_AGENT_RESPONSE_MARKER, "").strip()
            content = content[:_MAX_DISCUSSION_MESSAGE_CHARS]
            author = self._value(comment, "author")
            author_name = (self._value(author, "display_name", "displayName")
                           or self._value(author, "unique_name", "uniqueName")
                           or "Unknown")
            comments.append(SimpleNamespace(
                id=self._value(comment, "id"),
                body=content,
                user=SimpleNamespace(login=author_name),
            ))
        return comments

    def reconcile_code_suggestion_threads(self) -> int:
        if not get_settings().get("config.persistent_inline_comments", False):
            return 0

        head = getattr(getattr(self.pr, "last_merge_commit", None), "commit_id", None)
        if not head:
            return 0

        file_lines = {}
        fixed = 0
        for thread in self._get_threads():
            if str(self._value(thread, "status") or "").lower() not in {"active", "closed"}:
                continue
            context = self._value(thread, "thread_context", "threadContext")
            path = self._value(context, "file_path", "filePath")
            position = self._value(context, "right_file_start", "rightFileStart")
            start = self._value(position, "line")
            if (not isinstance(path, str) or not path or not isinstance(start, int)
                    or isinstance(start, bool) or start < 1):
                continue
            comments = self._value(thread, "comments") or []
            body = next((self._value(comment, "content") for comment in comments
                         if isinstance(self._value(comment, "content"), str)
                         and self._value(comment, "content")), None)
            if not isinstance(body, str) or "<!-- pr-agent-dedup-code:" not in body:
                continue
            suggestion_code = extract_suggestion_code(body)
            if not suggestion_code or not suggestion_code.strip():
                continue

            if path not in file_lines:
                try:
                    version = GitVersionDescriptor(version=head, version_type="commit")
                    item = self.azure_devops_client.get_item(
                        repository_id=self.repo_slug,
                        path=path,
                        project=self.workspace_slug,
                        version_descriptor=version,
                        download=False,
                        include_content=True,
                    )
                    content = getattr(item, "content", None)
                    if not isinstance(content, str):
                        raise TypeError("Azure DevOps returned non-string file content")
                    file_lines[path] = content.splitlines()
                except Exception as e:
                    get_logger().warning(f"Failed to read {path} while reconciling code suggestions: {e}")
                    file_lines[path] = None
            lines = file_lines[path]
            if lines is None:
                continue
            suggested_lines = suggestion_code.splitlines()
            if lines[start - 1:start - 1 + len(suggested_lines)] != suggested_lines:
                continue
            thread_id = self._value(thread, "id")
            if thread_id is not None and self.set_thread_status(thread_id, "fixed"):
                fixed += 1
        return fixed

    def get_issue_comments(self) -> list[Comment]:
        comment_list = []
        for thread in reversed(self._get_threads()):
            for comment in thread.comments:
                if comment.content and comment not in comment_list:
                    comment.body = comment.content
                    comment.thread_id = thread.id
                    comment_list.append(comment)
        return comment_list

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    def set_like(self, thread_id: int, comment_id: int, create: bool = True):
        if create:
            self.azure_devops_client.create_like(self.repo_slug, self.pr_num, thread_id, comment_id, project=self.workspace_slug)
        else:
            self.azure_devops_client.delete_like(self.repo_slug, self.pr_num, thread_id, comment_id, project=self.workspace_slug)

    def set_thread_status(self, thread_id: int, status: str) -> bool:
        try:
            self.azure_devops_client.update_thread(
                CommentThread(status=status), self.repo_slug, self.pr_num, thread_id, self.workspace_slug
            )
            self._threads_cache = None
            return True
        except Exception as e:
            get_logger().exception(f"Failed to set thread status, error: {e}")
            return False

    def reply_to_thread(self, thread_id: int, body: str, is_temporary: bool = False) -> Comment:
        try:
            content = body if AZURE_AGENT_RESPONSE_MARKER in body else f"{body}\n\n{AZURE_AGENT_RESPONSE_MARKER}"
            if is_temporary and AZURE_AGENT_PROGRESS_MARKER not in content:
                content = f"{content}\n{AZURE_AGENT_PROGRESS_MARKER}"
            comment = Comment(content=content)
            response = self.azure_devops_client.create_comment(
                comment, self.repo_slug, self.pr_num, thread_id, self.workspace_slug
            )
            self._threads_cache = None
            response.thread_id = thread_id
            if is_temporary:
                self.temp_comments.append(response)
            return response
        except Exception as e:
            get_logger().exception(f"Failed to reply to thread, error: {e}")

    def get_thread_context(self, thread_id: int) -> CommentThreadContext:
        try:
            thread = self.azure_devops_client.get_pull_request_thread(self.repo_slug, self.pr_num, thread_id, self.workspace_slug)
            return thread.thread_context
        except Exception as e:
            get_logger().exception(f"Failed to set thread status, error: {e}")

    @staticmethod
    def _parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
        parsed_url = urlparse(pr_url)
        path_parts = parsed_url.path.strip("/").split("/")
        num_parts = len(path_parts)
        if num_parts < 5:
            raise ValueError("The provided URL has insufficient path components for an Azure DevOps PR URL")

        # Verify that the second-to-last path component is "pullrequest"
        if path_parts[num_parts - 2] != "pullrequest":
            raise ValueError("The provided URL does not follow the expected Azure DevOps PR URL format")

        # Decode percent-encoding (e.g. %20) so project/repo names with spaces
        # match what the Azure DevOps REST client expects (e.g. "Dev Project")
        workspace_slug = unquote(path_parts[num_parts - 5])
        repo_slug = unquote(path_parts[num_parts - 3])
        try:
            pr_number = int(path_parts[num_parts - 1])
        except ValueError as e:
            raise ValueError("Cannot parse PR number in the provided URL") from e

        return workspace_slug, repo_slug, pr_number

    @staticmethod
    def _get_azure_devops_client() -> Tuple[GitClient, WorkItemTrackingClient]:
        org = get_settings().azure_devops.get("org", None)
        pat = get_settings().azure_devops.get("pat", None)

        if not org:
            raise ValueError("Azure DevOps organization is required")

        if pat:
            auth_token = pat
        else:
            try:
                # try to use azure default credentials
                # see https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme?view=azure-python
                # for usage and env var configuration of user-assigned managed identity, local machine auth etc.
                get_logger().info("No PAT found in settings, trying to use Azure Default Credentials.")
                credentials = DefaultAzureCredential()
                accessToken = credentials.get_token(ADO_APP_CLIENT_DEFAULT_ID)
                auth_token = accessToken.token
            except Exception as e:
                get_logger().error(f"No PAT found in settings, and Azure Default Authentication failed, error: {e}")
                raise

        credentials = BasicAuthentication("", auth_token)
        azure_devops_connection = Connection(base_url=org, creds=credentials)
        azure_devops_client = azure_devops_connection.clients.get_git_client()
        azure_devops_board_client = azure_devops_connection.clients.get_work_item_tracking_client()

        return azure_devops_client, azure_devops_board_client

    def _get_repo(self):
        if self.repo is None:
            self.repo = self.azure_devops_client.get_repository(
                project=self.workspace_slug, repository_id=self.repo_slug
            )
        return self.repo

    def _get_pr(self):
        self.pr = self.azure_devops_client.get_pull_request_by_id(
            pull_request_id=self.pr_num, project=self.workspace_slug
        )
        return self.pr

    def get_commit_messages(self) -> str:
        return ""  # not implemented yet

    def get_pr_id(self):
        try:
            pr_id = f"{self.workspace_slug}/{self.repo_slug}/{self.pr_num}"
            return pr_id
        except Exception as e:
            if get_verbosity_level() >= 2:
                get_logger().info(f"Failed to get PR id, error: {e}")
            return ""

    def publish_file_comments(self, file_comments: list) -> bool:
        pass

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        return self.pr_url+f"?_a=files&path={relevant_file}"

    def get_comment_url(self, comment) -> str:
        return self.pr_url + "?discussionId=" + str(comment.thread_id)

    def get_latest_commit_url(self) -> str:
        commits = self.azure_devops_client.get_pull_request_commits(self.repo_slug, self.pr_num, self.workspace_slug)
        if not commits:
            # a PR force-pushed back onto its base has zero commits; fall back to the base-class contract
            return ""
        last = commits[0]
        # workspace/repo slugs are stored decoded (e.g. "Dev Project") for the REST API,
        # so re-encode them when building a web URL to avoid raw spaces in markdown output
        workspace = quote(self.workspace_slug, safe='')
        repo = quote(self.repo_slug, safe='')
        url = self.azure_devops_client.normalized_url + "/" + workspace + "/_git/" + repo + "/commit/" + last.commit_id
        return url

    def get_linked_work_items(self) -> list:
        """
        Get linked work items from the PR.
        """
        try:
            work_items = self.azure_devops_client.get_pull_request_work_item_refs(
                project=self.workspace_slug,
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
            )
            ids = [work_item.id for work_item in work_items]
            if not work_items:
                return []
            items = self.get_work_items(ids)
            return items
        except Exception as e:
            get_logger().exception(f"Failed to get linked work items, error: {e}")
            return []

    def get_work_items(self, work_item_ids: list) -> list:
        """
        Get work items by their IDs.
        """
        try:
            raw_work_items = self.azure_devops_board_client.get_work_items(
                project=self.workspace_slug,
                ids=work_item_ids,
            )
            work_items = []
            for item in raw_work_items:
                work_items.append(
                    {
                        "id": item.id,
                        "title": item.fields.get("System.Title", ""),
                        "url": item.url,
                        "body": item.fields.get("System.Description", ""),
                        "acceptance_criteria": item.fields.get(
                            "Microsoft.VSTS.Common.AcceptanceCriteria", ""
                        ),
                        "tags": item.fields.get("System.Tags", "").split("; ") if item.fields.get("System.Tags") else [],
                    }
                )
            return work_items
        except Exception as e:
            get_logger().exception(f"Failed to get work items, error: {e}")
            return []
