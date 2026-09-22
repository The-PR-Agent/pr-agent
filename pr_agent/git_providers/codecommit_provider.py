import re
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.language_handler import build_language_file_matcher, is_valid_file
from pr_agent.algo.review_finding_state import split_review_state_marker
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers.codecommit_client import CodeCommitClient

from ..algo.comment_identity import (
    add_pr_review_identity,
    comment_carries_other_identity,
    comment_matches_identity,
)
from ..algo.utils import load_large_diff
from ..config_loader import get_settings
from ..log import get_logger
from .git_provider import GitProvider


class PullRequestCCMimic:
    """
    This class mimics the PullRequest class from the PyGithub library for the CodeCommitProvider.
    """

    def __init__(self, title: str, diff_files: List[FilePatchInfo], targets=None):
        self.title = title
        self.diff_files = diff_files
        self.targets = targets or []
        self.description = None
        self.source_commit = None
        self.source_branch = None  # the branch containing your new code changes
        self.destination_commit = None
        self.destination_branch = None  # the branch you are going to merge into


class CodeCommitFile:
    """
    This class represents a file in a pull request in CodeCommit.
    """

    def __init__(
        self,
        a_path: str,
        a_blob_id: str,
        b_path: str,
        b_blob_id: str,
        edit_type: EDIT_TYPE,
        repository_name: Optional[str] = None,
        source_commit: Optional[str] = None,
        destination_commit: Optional[str] = None,
        comparison_base_commit: Optional[str] = None,
    ):
        self.a_path = a_path
        self.a_blob_id = a_blob_id
        self.b_path = b_path
        self.b_blob_id = b_blob_id
        self.edit_type: EDIT_TYPE = edit_type
        self.filename = b_path if b_path else a_path
        self.repository_name = repository_name
        self.source_commit = source_commit
        self.destination_commit = destination_commit
        self.comparison_base_commit = comparison_base_commit


class CodeCommitProvider(GitProvider):
    """
    This class implements the GitProvider interface for AWS CodeCommit repositories.
    """

    # PostCommentForPullRequest / UpdateComment reject a body above 10,240
    # characters and raise instead of degrading (#3272). Every outgoing body
    # goes through _prepare_comment_body, which caps it AFTER the newline
    # doubling and after any persistent-comment header has been added, so the
    # cap is measured on what CodeCommit actually receives. Class-level, like
    # the other providers' max_comment_length, minus the truncation marker
    # limit_output_characters appends.
    max_comment_length = 10240 - len("...")

    def __init__(self, pr_url: Optional[str] = None, incremental: Optional[bool] = False):
        self.codecommit_client = CodeCommitClient()
        self.aws_client = None
        self.repo_name = None
        self.pr_num = None
        self.pr = None
        self.diff_files = None
        self.git_files = None
        self.pr_url = pr_url
        if pr_url:
            self.set_pr(pr_url)

    def provider_name(self):
        return "CodeCommit"

    def is_supported(self, capability: str) -> bool:
        if capability in [
            "create_inline_comment",
            "publish_inline_comments",
            "get_labels",
            "gfm_markdown",
            "markdown_backslash_escapes",
        ]:
            return False
        return True

    def set_pr(self, pr_url: str):
        self.repo_name, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def get_files(self) -> list[CodeCommitFile]:
        # bring files from CodeCommit only once
        if self.git_files:
            return self.git_files

        self.git_files = []
        for target in self._get_target_contexts():
            differences = self.codecommit_client.get_differences(
                target["repository_name"], target["comparison_base_commit"], target["source_commit"]
            )
            for item in differences:
                self.git_files.append(
                    CodeCommitFile(
                        item.before_blob_path,
                        item.before_blob_id,
                        item.after_blob_path,
                        item.after_blob_id,
                        CodeCommitProvider._get_edit_type(item.change_type),
                        repository_name=target["repository_name"],
                        source_commit=target["source_commit"],
                        destination_commit=target["destination_commit"],
                        comparison_base_commit=target["comparison_base_commit"],
                    )
                )
        return self.git_files

    def get_diff_files(self) -> list[FilePatchInfo]:
        """
        Retrieves the list of files that have been modified, added, deleted, or renamed in a pull request in CodeCommit,
        along with their content and patch information.

        Returns:
            diff_files (List[FilePatchInfo]): List of FilePatchInfo objects representing the modified, added, deleted,
            or renamed files in the merge request.
        """
        # bring files from CodeCommit only once
        if self.diff_files is not None:
            return self.diff_files

        diff_files = []

        files = self.get_files()
        # Repository settings are request-scoped and currently come from one canonical target.
        # Applying those ignore rules to a multi-target PR could suppress files from unrelated
        # target repositories, so preserve the pre-existing multi-target behavior until settings
        # can be scoped per target.
        if len(self._get_target_contexts()) == 1:
            files = filter_ignored(files, platform="codecommit")

        for diff_item in files:
            # Skip "bad extensions" from language_extensions.toml, lockfiles and minified assets
            if not is_valid_file(diff_item.filename):
                continue

            patch_filename = ""
            repository_name = diff_item.repository_name or self.repo_name
            destination_commit = diff_item.destination_commit or self.pr.destination_commit
            source_commit = diff_item.source_commit or self.pr.source_commit
            comparison_base_commit = diff_item.comparison_base_commit or destination_commit
            try:
                if diff_item.a_blob_id:
                    patch_filename = diff_item.a_path
                    original_file_content_str = self.codecommit_client.get_file(
                        repository_name, diff_item.a_path, comparison_base_commit)
                    if isinstance(original_file_content_str, (bytes, bytearray)):
                        original_file_content_str = original_file_content_str.decode("utf-8")
                else:
                    original_file_content_str = ""

                if diff_item.b_blob_id:
                    patch_filename = diff_item.b_path
                    new_file_content_str = self.codecommit_client.get_file(
                        repository_name, diff_item.b_path, source_commit)
                    if isinstance(new_file_content_str, (bytes, bytearray)):
                        new_file_content_str = new_file_content_str.decode("utf-8")
                else:
                    new_file_content_str = ""
            except UnicodeDecodeError as e:
                get_logger().warning(f"Skipping non-UTF-8 file in CodeCommit diff: {diff_item.filename!r} ({e})")
                continue

            patch = load_large_diff(patch_filename, new_file_content_str, original_file_content_str)

            # Store the diffs as a list of FilePatchInfo objects
            info = FilePatchInfo(
                original_file_content_str,
                new_file_content_str,
                patch,
                diff_item.filename,
                edit_type=diff_item.edit_type,
                old_filename=None
                if diff_item.a_path == diff_item.b_path
                else diff_item.a_path,
            )
            diff_files.append(info)

        self.diff_files = diff_files
        return self.diff_files

    def publish_description(self, pr_title: str, pr_body: str):
        try:
            self.codecommit_client.publish_description(
                pr_number=self.pr_num,
                pr_title=pr_title,
                pr_body=CodeCommitProvider._add_additional_newlines(pr_body),
            )
        except Exception as e:
            raise ValueError(f"CodeCommit Cannot publish description for PR: {self.pr_num}") from e

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if is_temporary:
            get_logger().info(pr_comment)
            return None

        try:
            published_comments = [
                self._publish_comment_to_target(pr_comment, target)
                for target in self._get_target_contexts()
            ]
            if len(published_comments) == 1:
                return published_comments[0]
            return published_comments
        except Exception as e:
            raise ValueError(f"CodeCommit Cannot publish comment for PR: {self.pr_num}") from e

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True,
                                   as_thread: bool = False,
                                   identity_marker: str | None = None,
                                   legacy_initial_header: str | None = None):
        if as_thread:
            get_logger().debug("CodeCommit does not support threaded persistent comments; publishing as a PR comment")

        if not identity_marker:
            get_logger().debug(
                "CodeCommit persistent comment updates require a stable identity marker; publishing a new comment"
            )
            return self.publish_comment(pr_comment)

        persistent_comment = add_pr_review_identity(pr_comment, identity_marker)
        try:
            comments = self.get_issue_comments_newest_first()
        except Exception as e:
            get_logger().warning(
                f"CodeCommit could not read existing comments; publishing a new persistent comment: {e}"
            )
            return self.publish_comment(persistent_comment)

        identifiers = [identity_marker, legacy_initial_header]
        used_comment_ids = set()
        published_comments = []
        target_contexts = self._get_target_contexts()
        destination_counts = Counter(
            (target["repository_name"], target["destination_commit"])
            for target in target_contexts
        )
        allow_repository_fallback = len(target_contexts) == 1

        for target in target_contexts:
            comment_to_update = self._find_persistent_comment_for_target(
                comments,
                target,
                identifiers,
                identity_marker,
                used_comment_ids,
                destination_counts[(target["repository_name"], target["destination_commit"])] == 1,
                allow_repository_fallback,
            )
            if comment_to_update is None:
                published_comments.append(self._publish_comment_to_target(persistent_comment, target))
                continue

            used_comment_ids.add(comment_to_update.id)
            comment_body = self._persistent_body_for_target(
                persistent_comment,
                update_header,
                name,
                identity_marker,
                target,
            )
            comment_url = self.get_comment_url(comment_to_update)
            get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
            if self.edit_comment(comment_to_update, comment_body) is not False:
                published_comments.append(comment_to_update)
                continue

            published_comments.append(self._publish_comment_to_target(persistent_comment, target))

        if final_update_message:
            get_logger().debug("CodeCommit does not publish separate persistent update status comments")
        if len(published_comments) == 1:
            return published_comments[0]
        return published_comments

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        counter = 1
        publishable_count = 0
        published_count = 0
        for suggestion in code_suggestions:
            # Verify that each suggestion has the required keys
            if not all(key in suggestion for key in ["body", "relevant_file", "relevant_lines_start"]):
                get_logger().warning(f"Skipping code suggestion #{counter}: Each suggestion must have 'body', 'relevant_file', 'relevant_lines_start' keys")
                continue

            publishable_count += 1
            target_contexts = self._get_target_contexts_for_file(suggestion["relevant_file"])
            for target in target_contexts:
                try:
                    get_logger().debug(
                        f"Code Suggestion #{counter} in file: {suggestion['relevant_file']}: "
                        f"{suggestion['relevant_lines_start']} for repository: {target['repository_name']}"
                    )
                    self.codecommit_client.publish_comment(
                        repo_name=target["repository_name"],
                        pr_number=self.pr_num,
                        destination_commit=target["destination_commit"],
                        source_commit=target["source_commit"],
                        comment=suggestion["body"],
                        annotation_file=suggestion["relevant_file"],
                        annotation_line=suggestion["relevant_lines_start"],
                    )
                    published_count += 1
                except Exception as e:
                    raise ValueError(f"CodeCommit Cannot publish code suggestions for PR: {self.pr_num}") from e

            counter += 1

        # A partial failure must not report failure: the caller republishes the whole
        # list, which would post the already-accepted suggestions a second time.
        return published_count > 0 or publishable_count == 0

    def publish_labels(self, labels):
        return [""]  # not implemented yet

    def get_pr_labels(self, update=False):
        return [""]  # not implemented yet

    def remove_initial_comment(self):
        return ""  # not implemented yet

    def remove_comment(self, comment):
        return ""  # not implemented yet

    def edit_comment(self, comment, body: str):
        comment_id = comment.get("id") if isinstance(comment, dict) else getattr(comment, "id", None)
        if not isinstance(comment_id, str) or not comment_id:
            get_logger().warning(f"CodeCommit cannot update comment without a valid comment id: {comment_id!r}")
            return False

        body = self._prepare_comment_body(body)
        try:
            response = self.codecommit_client.update_comment(comment_id, body)
        except Exception as e:
            get_logger().warning(f"CodeCommit failed to update comment {comment_id}: {e}")
            return False

        updated_comment = response.get("comment") if isinstance(response, dict) else None
        if not isinstance(updated_comment, dict):
            return False
        if updated_comment.get("deleted") is True:
            return False
        return updated_comment.get("commentId") == comment_id and updated_comment.get("content") == body

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/codecommit/client/post_comment_for_compared_commit.html
        raise NotImplementedError("CodeCommit provider does not support publishing inline comments yet")

    def publish_inline_comments(self, comments: list[dict]):
        raise NotImplementedError("CodeCommit provider does not support publishing inline comments yet")

    def get_title(self):
        return self.pr.title

    def get_pr_id(self):
        """
        Returns the PR ID in the format: "repo_name/pr_number".
        Note: This is an internal identifier for PR-Agent,
        and is not the same as the CodeCommit PR identifier.
        """
        try:
            pr_id = f"{self.repo_name}/{self.pr_num}"
            return pr_id
        except:
            return ""

    def get_languages(self):
        """Return recognized language percentages for diff prioritization."""
        language_map = get_settings().get("language_extension_map_org", {}) or {}
        get_language = build_language_file_matcher(language_map)
        language_counts = Counter()
        for file in self.get_files():
            if not file.filename:
                continue
            language = get_language(file.filename)
            if language:
                language_counts[language] += 1

        total = sum(language_counts.values()) or 1
        return {language: count / total * 100 for language, count in language_counts.items()}

    def get_pr_branch(self):
        return self.pr.source_branch

    def get_pr_description_full(self) -> str:
        return self.pr.description

    def get_user_id(self):
        return -1  # not implemented yet

    def get_issue_comments(self):
        comments = []
        for comment_data in self.codecommit_client.get_comments_for_pull_request(self.pr_num):
            comments.extend(self._extract_issue_comments(comment_data))
        return sorted(comments, key=self._comment_sort_key)

    def get_issue_comments_newest_first(self):
        return sorted(self.get_issue_comments(), key=self._comment_sort_key, reverse=True)

    def supports_review_comment_identity(self) -> bool:
        return True

    def get_comment_url(self, comment) -> str:
        return self.get_pr_url()

    def get_repo_settings(self):
        # a local ".pr_agent.toml" settings file is optional
        settings_filename = ".pr_agent.toml"
        target = self._get_target_contexts()[0]
        return self.codecommit_client.get_file(
            target["repository_name"], settings_filename, target["destination_commit"], optional=True
        )

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        get_logger().info("CodeCommit provider does not support eyes reaction yet")
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        get_logger().info("CodeCommit provider does not support removing reactions yet")
        return True

    @staticmethod
    def _parse_pr_url(pr_url: str) -> Tuple[str, int]:
        """
        Parse the CodeCommit PR URL and return the repository name and PR number.

        Args:
        - pr_url: the full AWS CodeCommit pull request URL

        Returns:
        - Tuple[str, int]: A tuple containing the repository name and PR number.
        """
        # Example PR URL:
        # https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/__MY_REPO__/pull-requests/123456"
        parsed_url = urlparse(pr_url)

        if not CodeCommitProvider._is_valid_codecommit_hostname(parsed_url.netloc):
            raise ValueError(f"The provided URL is not a valid CodeCommit URL: {pr_url}")

        path_parts = parsed_url.path.strip("/").split("/")

        if (
            len(path_parts) < 6
            or path_parts[0] != "codesuite"
            or path_parts[1] != "codecommit"
            or path_parts[2] != "repositories"
            or path_parts[4] != "pull-requests"
        ):
            raise ValueError(f"The provided URL does not appear to be a CodeCommit PR URL: {pr_url}")

        repo_name = path_parts[3]

        try:
            pr_number = int(path_parts[5])
        except ValueError as e:
            raise ValueError(f"Unable to convert PR number to integer: '{path_parts[5]}'") from e

        return repo_name, pr_number

    @staticmethod
    def _is_valid_codecommit_hostname(hostname: str) -> bool:
        """
        Check if the provided hostname is a valid AWS CodeCommit hostname.

        This is not an exhaustive check of AWS region names,
        but instead uses a regex to check for matching AWS region patterns.

        Args:
        - hostname: the hostname to check

        Returns:
        - bool: True if the hostname is valid, False otherwise.
        """
        return re.match(r"^[a-z]{2}-(gov-)?[a-z]+-\d\.console\.aws\.amazon\.com$", hostname) is not None

    def _get_pr(self):
        response = self.codecommit_client.get_pr(self.repo_name, self.pr_num)

        if len(response.targets) == 0:
            raise ValueError(f"No files found in CodeCommit PR: {self.pr_num}")

        # Return our object that mimics PullRequest class from the PyGithub library
        # (This strategy was copied from the LocalGitProvider)
        mimic = PullRequestCCMimic(response.title, self.diff_files, targets=response.targets)
        mimic.description = response.description
        mimic.source_commit = response.targets[0].source_commit
        mimic.source_branch = response.targets[0].source_branch
        mimic.destination_commit = response.targets[0].destination_commit
        mimic.destination_branch = response.targets[0].destination_branch

        return mimic

    def _get_target_contexts(self):
        """Return the CodeCommit comparisons represented by this pull request.

        CodeCommit can associate one pull request with multiple repository/branch
        targets. Keep a scalar fallback for callers and tests that construct the
        legacy PR mimic directly.
        """
        targets = getattr(self.pr, "targets", None) or []
        if not targets:
            return [{
                "repository_name": self.repo_name,
                "source_commit": self.pr.source_commit,
                "destination_commit": self.pr.destination_commit,
                "comparison_base_commit": self.pr.destination_commit,
            }]

        return [
            {
                "repository_name": getattr(target, "repository_name", "") or self.repo_name,
                "source_commit": target.source_commit,
                "destination_commit": target.destination_commit,
                "comparison_base_commit": getattr(target, "merge_base", "") or target.destination_commit,
            }
            for target in targets
        ]

    def _get_target_contexts_for_file(self, filename: str):
        """Return target comparisons whose CodeCommit diff contains ``filename``.

        Suggestions are tied to a file comparison. Publishing an annotation to
        every target would fail when only one target changed that path, so route
        it to matching target comparisons and retain the legacy first-target
        fallback for malformed or synthetic suggestions.
        """
        normalized_filename = filename.lstrip("/")
        matching_contexts = []
        for diff_file in self.get_files():
            paths = {diff_file.a_path, diff_file.b_path}
            normalized_paths = {path.lstrip("/") for path in paths if path}
            if filename not in paths and normalized_filename not in normalized_paths:
                continue
            context = {
                "repository_name": diff_file.repository_name or self.repo_name,
                "source_commit": diff_file.source_commit or self.pr.source_commit,
                "destination_commit": diff_file.destination_commit or self.pr.destination_commit,
            }
            if context not in matching_contexts:
                matching_contexts.append(context)

        return matching_contexts or self._get_target_contexts()[:1]

    def get_commit_messages(self) -> str:
        return ""  # not implemented yet

    def _publish_comment_to_target(self, pr_comment: str, target: dict):
        pr_comment = self._prepare_comment_body(pr_comment)
        response = self.codecommit_client.publish_comment(
            repo_name=target["repository_name"],
            pr_number=self.pr_num,
            destination_commit=target["destination_commit"],
            source_commit=target["source_commit"],
            comment=pr_comment,
        )
        if isinstance(response, dict):
            return self._comment_from_api_comment(
                response.get("comment"),
                {
                    "repositoryName": target["repository_name"],
                    "beforeCommitId": target["destination_commit"],
                    "afterCommitId": target["source_commit"],
                },
            )
        return response

    def _find_persistent_comment_for_target(
        self,
        comments: list,
        target: dict,
        identifiers: list,
        identity_marker: str,
        used_comment_ids: set,
        allow_destination_fallback: bool,
        allow_repository_fallback: bool,
    ):
        matchers = [self._comment_matches_target_exact]
        if allow_destination_fallback:
            matchers.append(self._comment_matches_target_destination)
        if allow_repository_fallback:
            matchers.append(self._comment_matches_target_repository)

        for matcher in matchers:
            comment = self._find_persistent_comment(
                comments,
                identifiers,
                identity_marker,
                used_comment_ids,
                lambda candidate: matcher(candidate, target),
            )
            if comment is not None:
                return comment
        return None

    @staticmethod
    def _find_persistent_comment(
        comments: list,
        identifiers: list,
        identity_marker: str,
        used_comment_ids: set,
        target_matcher,
    ):
        for identifier in identifiers:
            if not identifier:
                continue
            for comment in comments:
                if comment.id in used_comment_ids or not target_matcher(comment):
                    continue
                body = GitProvider._get_comment_body(comment)
                if not comment_matches_identity(body, identifier):
                    continue
                if comment_carries_other_identity(body, identity_marker):
                    continue
                return comment
        return None

    @staticmethod
    def _comment_matches_target_exact(comment, target: dict) -> bool:
        return (
            CodeCommitProvider._comment_matches_target_destination(comment, target)
            and getattr(comment, "after_commit_id", None) == target["source_commit"]
        )

    @staticmethod
    def _comment_matches_target_destination(comment, target: dict) -> bool:
        return (
            CodeCommitProvider._comment_matches_target_repository(comment, target)
            and getattr(comment, "before_commit_id", None) == target["destination_commit"]
        )

    @staticmethod
    def _comment_matches_target_repository(comment, target: dict) -> bool:
        repository_name = getattr(comment, "repository_name", None)
        return repository_name == target["repository_name"]

    @staticmethod
    def _persistent_body_for_target(
        pr_comment: str,
        update_header: bool,
        name: str,
        identity_marker: str,
        target: dict,
    ) -> str:
        if not update_header:
            return pr_comment

        update_message = f"#### ({name.capitalize()} updated until commit {target['source_commit']})\n"
        updated_anchor = f"{identity_marker}\n\n{update_message}"
        return pr_comment.replace(identity_marker, updated_anchor, 1)

    def _prepare_comment_body(self, pr_comment: str) -> str:
        pr_comment = CodeCommitProvider._remove_markdown_html(pr_comment)
        body, marker = split_review_state_marker(pr_comment)
        body = CodeCommitProvider._add_additional_newlines(body)
        if not marker:
            return self.limit_output_characters(body, self.max_comment_length)
        # The persistent review state is a hidden marker at the end of the body.
        # The reviewer sizes it before the newline doubling above, so the doubled
        # body can overrun the cap; truncate only the human text and keep the
        # marker whole, otherwise the next run cannot parse the state.
        budget = self.max_comment_length - len(marker) - 2
        if budget <= 0:
            return marker[: self.max_comment_length]
        return f"{self.limit_output_characters(body, budget)}\n\n{marker}"

    @staticmethod
    def _extract_issue_comments(comment_data: dict):
        if not isinstance(comment_data, dict) or comment_data.get("location"):
            return []

        comments = []
        for comment in comment_data.get("comments", []):
            issue_comment = CodeCommitProvider._comment_from_api_comment(comment, comment_data)
            if issue_comment is not None:
                comments.append(issue_comment)
        return comments

    @staticmethod
    def _comment_from_api_comment(comment: dict, comment_data: dict):
        if not isinstance(comment, dict):
            return None
        if comment.get("deleted") is True or comment.get("inReplyTo"):
            return None

        body = comment.get("content")
        comment_id = comment.get("commentId")
        created_at = CodeCommitProvider._comment_timestamp(comment.get("creationDate"))
        if not isinstance(body, str) or not body:
            return None
        if not isinstance(comment_id, str) or not comment_id:
            return None
        if created_at is None:
            return None

        author_arn = comment.get("authorArn") if isinstance(comment.get("authorArn"), str) else ""
        return SimpleNamespace(
            body=body,
            id=comment_id,
            created_at=created_at,
            last_modified_at=CodeCommitProvider._comment_timestamp(comment.get("lastModifiedDate")),
            repository_name=comment_data.get("repositoryName"),
            before_commit_id=comment_data.get("beforeCommitId"),
            after_commit_id=comment_data.get("afterCommitId"),
            user=SimpleNamespace(login=author_arn),
        )

    @staticmethod
    def _comment_timestamp(value):
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @staticmethod
    def _comment_sort_key(comment):
        return (comment.created_at, comment.id)

    @staticmethod
    def _add_additional_newlines(body: str) -> str:
        """
        Replace single newlines in a PR body with double newlines.

        CodeCommit Markdown does not seem to render as well as GitHub Markdown,
        so we add additional newlines to the PR body to make it more readable in CodeCommit.

        Args:
        - body: the PR body

        Returns:
        - str: the PR body with the double newlines added
        """
        return re.sub(r'(?<!\n)\n(?!\n)', '\n\n', body)

    @staticmethod
    def _remove_markdown_html(comment: str) -> str:
        """
        Remove the HTML tags from a PR comment.

        CodeCommit Markdown does not seem to render as well as GitHub Markdown,
        so we remove the HTML tags from the PR comment to make it more readable in CodeCommit.

        Args:
        - comment: the PR comment

        Returns:
        - str: the PR comment with the HTML tags removed
        """
        comment = comment.replace("<details>", "")
        comment = comment.replace("</details>", "")
        comment = comment.replace("<summary>", "")
        comment = comment.replace("</summary>", "")
        return comment

    @staticmethod
    def _get_edit_type(codecommit_change_type: str):
        """
        Convert the CodeCommit change type string to the EDIT_TYPE enum.
        The CodeCommit change type string is returned from the get_differences SDK method.

        Args:
        - codecommit_change_type: the CodeCommit change type string

        Returns:
        - An EDIT_TYPE enum representing the modified, added, deleted, or renamed file in the PR diff.
        """
        t = codecommit_change_type.upper()
        edit_type = None
        if t == "A":
            edit_type = EDIT_TYPE.ADDED
        elif t == "D":
            edit_type = EDIT_TYPE.DELETED
        elif t == "M":
            edit_type = EDIT_TYPE.MODIFIED
        elif t == "R":
            edit_type = EDIT_TYPE.RENAMED
        return edit_type
