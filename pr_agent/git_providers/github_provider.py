import copy
import difflib
import hashlib
import itertools
import re
import time
import traceback
import json
from datetime import datetime
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from github.Issue import Issue
from github import AppAuthentication, Auth, Github, GithubException
from retry import retry
from starlette_context import context

from ..algo.file_filter import filter_ignored
from ..algo.git_patch_processing import extract_hunk_headers
from ..algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    RESOLVED_BODY_MARKER,
    append_marker,
    build_marker_index,
    find_comment_by_location,
    format_resolved_body,
    generate_marker,
    normalize_persistent_mode,
)
from ..algo.language_handler import is_valid_file
from ..algo.types import EDIT_TYPE
from ..algo.utils import (PRReviewHeader, Range, clip_tokens,
                          find_line_number_of_relevant_line_in_file,
                          load_large_diff, set_file_languages)
from ..config_loader import get_settings
from ..log import get_logger
from ..servers.utils import RateLimitExceeded
from .git_provider import (MAX_FILES_ALLOWED_FULL, FilePatchInfo, GitProvider,
                           IncrementalPR)


_TRUE_CONFIG_VALUES = {"1", "true", "t", "yes", "y", "on"}
_FALSE_CONFIG_VALUES = {"0", "false", "f", "no", "n", "off"}


def _normalize_bool_config(raw: Any, *, default: bool, setting_name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _TRUE_CONFIG_VALUES:
            return True
        if value in _FALSE_CONFIG_VALUES:
            return False

    get_logger().warning(
        f"Invalid boolean value for {setting_name}: {raw!r}. "
        f"Expected one of true/false, 1/0, yes/no, or on/off; using default {default!r}."
    )
    return default


def _is_valid_review_comment_id(comment_id: Any) -> bool:
    return isinstance(comment_id, int) and not isinstance(comment_id, bool)


class GithubProvider(GitProvider):
    def __init__(self, pr_url: Optional[str] = None):
        self.repo_obj = None
        try:
            self.installation_id = context.get("installation_id", None)
        except Exception:
            self.installation_id = None
        self.max_comment_chars = 65000
        self.base_url = get_settings().get("GITHUB.BASE_URL", "https://api.github.com").rstrip("/") # "https://api.github.com"
        self.base_url_html = self.base_url.split("api/")[0].rstrip("/") if "api/" in self.base_url else "https://github.com"
        self.github_client = self._get_github_client()
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.issue_main = None
        self.github_user_id = None
        self.diff_files = None
        self.git_files = None
        self.incremental = IncrementalPR(False)
        if pr_url and 'pull' in pr_url:
            self.set_pr(pr_url)
            self.pr_commits = list(self.pr.get_commits())
            self.last_commit_id = self.pr_commits[-1]
            self.pr_url = self.get_pr_url() # pr_url for github actions can be as api.github.com, so we need to get the url from the pr object
        elif pr_url and 'issue' in pr_url: #url is an issue
            self.issue_main = self._get_issue_handle(pr_url)
        else: #Instantiated the provider without a PR / Issue
            self.pr_commits = None

    def _get_issue_handle(self, issue_url) -> Optional[Issue]:
        repo_name, issue_number = self._parse_issue_url(issue_url)
        if not repo_name or not issue_number:
            get_logger().error(f"Given url: {issue_url} is not a valid issue.")
            return None
        # else: Check if can get a valid Repo handle:
        try:
            repo_obj = self.github_client.get_repo(repo_name)
            if not repo_obj:
                get_logger().error(f"Given url: {issue_url}, belonging to owner/repo: {repo_name} does "
                                   f"not have a valid repository: {self.get_git_repo_url(issue_url)}")
                return None
            # else: Valid repo handle:
            return repo_obj.get_issue(issue_number)
        except Exception as e:
            get_logger().exception(f"Failed to get an issue object for issue: {issue_url}, belonging to owner/repo: {repo_name}")
            return None

    def get_incremental_commits(self, incremental=IncrementalPR(False)):
        self.incremental = incremental
        if self.incremental.is_incremental:
            self.unreviewed_files_set = dict()
            self._get_incremental_commits()

    def is_supported(self, capability: str) -> bool:
        return True

    def _get_owner_and_repo_path(self, given_url: str) -> str:
        try:
            repo_path = None
            if 'issues' in given_url:
                repo_path, _ = self._parse_issue_url(given_url)
            elif 'pull' in given_url:
                repo_path, _ = self._parse_pr_url(given_url)
            elif given_url.endswith('.git'):
                parsed_url = urlparse(given_url)
                repo_path = (parsed_url.path.split('.git')[0])[1:] # /<owner>/<repo>.git -> <owner>/<repo>
            if not repo_path:
                get_logger().error(f"url is neither an issues url nor a PR url nor a valid git url: {given_url}. Returning empty result.")
                return ""
            return repo_path
        except Exception as e:
            get_logger().exception(f"unable to parse url: {given_url}. Returning empty result.")
            return ""

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        repo_path = self._get_owner_and_repo_path(issues_or_pr_url) #Return: <OWNER>/<REPO>
        if not repo_path or repo_path not in issues_or_pr_url:
            get_logger().error(f"Unable to retrieve owner/path from url: {issues_or_pr_url}")
            return ""
        return f"{self.base_url_html}/{repo_path}.git" #https://github.com / <OWNER>/<REPO>.git

    # Given a git repo url, return prefix and suffix of the provider in order to view a given file belonging to that repo.
    # Example: https://github.com/qodo-ai/pr-agent.git and branch: v0.8 -> prefix: "https://github.com/qodo-ai/pr-agent/blob/v0.8", suffix: ""
    # In case git url is not provided, provider will use PR context (which includes branch) to determine the prefix and suffix.
    def get_canonical_url_parts(self, repo_git_url:str, desired_branch:str) -> Tuple[str, str]:
        owner = None
        repo = None
        scheme_and_netloc = None

        if repo_git_url or self.issue_main: #Either user provided an external git url, which may be different than what this provider was initialized with, or an issue:
            desired_branch = desired_branch if repo_git_url else self.issue_main.repository.default_branch
            html_url = repo_git_url if repo_git_url else self.issue_main.html_url
            parsed_git_url = urlparse(html_url)
            scheme_and_netloc = parsed_git_url.scheme + "://" + parsed_git_url.netloc
            repo_path = self._get_owner_and_repo_path(html_url)
            if repo_path.count('/') == 1: #Has to have the form <owner>/<repo>
                owner, repo = repo_path.split('/')
            else:
                get_logger().error(f"Invalid repo_path: {repo_path} from url: {html_url}")
                return ("", "")

        if (not owner or not repo) and self.repo: #"else" - User did not provide an external git url, or not an issue, use self.repo object
            owner, repo = self.repo.split('/')
            scheme_and_netloc = self.base_url_html
            desired_branch = self.repo_obj.default_branch
        if not all([scheme_and_netloc, owner, repo]): #"else": Not invoked from a PR context,but no provided git url for context
            get_logger().error(f"Unable to get canonical url parts since missing context (PR or explicit git url)")
            return ("", "")

        prefix = f"{scheme_and_netloc}/{owner}/{repo}/blob/{desired_branch}"
        suffix = ""  # github does not add a suffix
        return (prefix, suffix)

    def get_pr_url(self) -> str:
        return self.pr.html_url

    def set_pr(self, pr_url: str):
        self.repo, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def _get_incremental_commits(self):
        if not self.pr_commits:
            self.pr_commits = list(self.pr.get_commits())

        self.previous_review = self.get_previous_review(full=True, incremental=True)
        if self.previous_review:
            self.incremental.commits_range = self.get_commit_range()
            # Get all files changed during the commit range

            for commit in self.incremental.commits_range:
                if commit.commit.message.startswith(f"Merge branch '{self._get_repo().default_branch}'"):
                    get_logger().info(f"Skipping merge commit {commit.commit.message}")
                    continue
                self.unreviewed_files_set.update({file.filename: file for file in commit.files})
        else:
            get_logger().info("No previous review found, will review the entire PR")
            self.incremental.is_incremental = False

    def get_commit_range(self):
        last_review_time = self.previous_review.created_at
        first_new_commit_index = None
        for index in range(len(self.pr_commits) - 1, -1, -1):
            if self.pr_commits[index].commit.author.date > last_review_time:
                self.incremental.first_new_commit = self.pr_commits[index]
                first_new_commit_index = index
            else:
                self.incremental.last_seen_commit = self.pr_commits[index]
                break
        return self.pr_commits[first_new_commit_index:] if first_new_commit_index is not None else []

    def get_previous_review(self, *, full: bool, incremental: bool):
        if not (full or incremental):
            raise ValueError("At least one of full or incremental must be True")
        if not getattr(self, "comments", None):
            self.comments = list(self.pr.get_issue_comments())
        prefixes = []
        if full:
            prefixes.append(PRReviewHeader.REGULAR.value)
        if incremental:
            prefixes.append(PRReviewHeader.INCREMENTAL.value)
        for index in range(len(self.comments) - 1, -1, -1):
            if any(self.comments[index].body.startswith(prefix) for prefix in prefixes):
                return self.comments[index]

    def get_files(self):
        if self.incremental.is_incremental and self.unreviewed_files_set:
            return self.unreviewed_files_set.values()
        try:
            git_files = context.get("git_files", None)
            if git_files:
                return git_files
            self.git_files = list(self.pr.get_files()) # 'list' to handle pagination
            context["git_files"] = self.git_files
            return self.git_files
        except Exception:
            if not self.git_files:
                self.git_files = list(self.pr.get_files())
            return self.git_files

    def get_num_of_files(self):
        if hasattr(self.git_files, "totalCount"):
            return self.git_files.totalCount
        else:
            try:
                return len(self.git_files)
            except Exception as e:
                return -1

    @retry(exceptions=RateLimitExceeded,
           tries=get_settings().github.ratelimit_retries, delay=2, backoff=2, jitter=(1, 3))
    def get_diff_files(self) -> list[FilePatchInfo]:
        """
        Retrieves the list of files that have been modified, added, deleted, or renamed in a pull request in GitHub,
        along with their content and patch information.

        Returns:
            diff_files (List[FilePatchInfo]): List of FilePatchInfo objects representing the modified, added, deleted,
            or renamed files in the merge request.
        """
        try:
            try:
                diff_files = context.get("diff_files", None)
                if diff_files:
                    return diff_files
            except Exception:
                pass

            if self.diff_files:
                return self.diff_files

            # filter files using [ignore] patterns
            files_original = self.get_files()
            files = filter_ignored(files_original)
            if files_original != files:
                try:
                    names_original = [file.filename for file in files_original]
                    names_new = [file.filename for file in files]
                    get_logger().info(f"Filtered out [ignore] files for pull request:", extra=
                    {"files": names_original,
                     "filtered_files": names_new})
                except Exception:
                    pass

            diff_files = []
            invalid_files_names = []
            is_close_to_rate_limit = False

            # The base.sha will point to the current state of the base branch (including parallel merges), not the original base commit when the PR was created
            # We can fix this by finding the merge base commit between the PR head and base branches
            # Note that The pr.head.sha is actually correct as is - it points to the latest commit in your PR branch.
            # This SHA isn't affected by parallel merges to the base branch since it's specific to your PR's branch.
            repo = self.repo_obj
            pr = self.pr
            try:
                compare = repo.compare(pr.base.sha, pr.head.sha) # communication with GitHub
                merge_base_commit = compare.merge_base_commit
            except Exception as e:
                get_logger().error(f"Failed to get merge base commit: {e}")
                merge_base_commit = pr.base
            if merge_base_commit.sha != pr.base.sha:
                get_logger().info(
                    f"Using merge base commit {merge_base_commit.sha} instead of base commit ")

            counter_valid = 0
            for file in files:
                if not is_valid_file(file.filename):
                    invalid_files_names.append(file.filename)
                    continue

                patch = file.patch
                if is_close_to_rate_limit:
                    new_file_content_str = ""
                    original_file_content_str = ""
                else:
                    # allow only a limited number of files to be fully loaded. We can manage the rest with diffs only
                    counter_valid += 1
                    avoid_load = False
                    if counter_valid >= MAX_FILES_ALLOWED_FULL and patch and not self.incremental.is_incremental:
                        avoid_load = True
                        if counter_valid == MAX_FILES_ALLOWED_FULL:
                            get_logger().info(f"Too many files in PR, will avoid loading full content for rest of files")

                    if avoid_load:
                        new_file_content_str = ""
                    else:
                        new_file_content_str = self._get_pr_file_content(file, self.pr.head.sha)  # communication with GitHub

                    if self.incremental.is_incremental and self.unreviewed_files_set:
                        original_file_content_str = self._get_pr_file_content(file, self.incremental.last_seen_commit_sha)
                        patch = load_large_diff(file.filename, new_file_content_str, original_file_content_str)
                        self.unreviewed_files_set[file.filename] = patch
                    else:
                        if avoid_load:
                            original_file_content_str = ""
                        else:
                            original_file_content_str = self._get_pr_file_content(file, merge_base_commit.sha)
                            # original_file_content_str = self._get_pr_file_content(file, self.pr.base.sha)
                        if not patch:
                            patch = load_large_diff(file.filename, new_file_content_str, original_file_content_str)


                if file.status == 'added':
                    edit_type = EDIT_TYPE.ADDED
                elif file.status == 'removed':
                    edit_type = EDIT_TYPE.DELETED
                elif file.status == 'renamed':
                    edit_type = EDIT_TYPE.RENAMED
                elif file.status == 'modified':
                    edit_type = EDIT_TYPE.MODIFIED
                else:
                    get_logger().error(f"Unknown edit type: {file.status}")
                    edit_type = EDIT_TYPE.UNKNOWN

                # count number of lines added and removed
                if hasattr(file, 'additions') and hasattr(file, 'deletions'):
                    num_plus_lines = file.additions
                    num_minus_lines = file.deletions
                else:
                    patch_lines = patch.splitlines(keepends=True)
                    num_plus_lines = len([line for line in patch_lines if line.startswith('+')])
                    num_minus_lines = len([line for line in patch_lines if line.startswith('-')])

                file_patch_canonical_structure = FilePatchInfo(original_file_content_str, new_file_content_str, patch,
                                                               file.filename, edit_type=edit_type,
                                                               num_plus_lines=num_plus_lines,
                                                               num_minus_lines=num_minus_lines,)
                diff_files.append(file_patch_canonical_structure)
            if invalid_files_names:
                get_logger().info(f"Filtered out files with invalid extensions: {invalid_files_names}")

            self.diff_files = diff_files
            try:
                context["diff_files"] = diff_files
            except Exception:
                pass

            return diff_files

        except Exception as e:
            get_logger().error(f"Failing to get diff files: {e}",
                               artifact={"traceback": traceback.format_exc()})
            raise RateLimitExceeded("Rate limit exceeded for GitHub API.") from e

    def publish_description(self, pr_title: str, pr_body: str):
        self.pr.edit(title=pr_title, body=pr_body)

    def get_latest_commit_url(self) -> str:
        return self.last_commit_id.html_url

    def get_comment_url(self, comment) -> str:
        return comment.html_url

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        self.publish_persistent_comment_full(pr_comment, initial_header, update_header, name, final_update_message)

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not self.pr and not self.issue_main:
            get_logger().error("Cannot publish a comment if missing PR/Issue context")
            return None

        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment: {pr_comment}")
            return None
        pr_comment = self.limit_output_characters(pr_comment, self.max_comment_chars)

        # In case this is an issue, can publish the comment on the issue.
        if self.issue_main:
            return self.issue_main.create_comment(pr_comment)

        response = self.pr.create_issue_comment(pr_comment)
        if hasattr(response, "user") and hasattr(response.user, "login"):
            self.github_user_id = response.user.login
        response.is_temporary = is_temporary
        if not hasattr(self.pr, 'comments_list'):
            self.pr.comments_list = []
        self.pr.comments_list.append(response)
        return response

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        body = self.limit_output_characters(body, self.max_comment_chars)
        self.publish_inline_comments([self.create_inline_comment(body, relevant_file, relevant_line_in_file)])


    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                              absolute_position: int = None):
        body = self.limit_output_characters(body, self.max_comment_chars)
        position, absolute_position = find_line_number_of_relevant_line_in_file(self.diff_files,
                                                                                relevant_file.strip('`'),
                                                                                relevant_line_in_file,
                                                                                absolute_position)
        if position == -1:
            get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"
        path = relevant_file.strip()
        return dict(body=body, path=path, position=position) if subject_type == "LINE" else {}

    def publish_inline_comments(self, comments: list[dict], disable_fallback: bool = False):
        try:
            # publish all comments in a single message
            self.pr.create_review(commit=self.last_commit_id, comments=comments)
        except Exception as e:
            get_logger().info(f"Initially failed to publish inline comments as committable")

            if (getattr(e, "status", None) == 422 and not disable_fallback):
                pass  # continue to try _publish_inline_comments_fallback_with_verification
            else:
                raise e # will end up with publishing the comments one by one

            try:
                self._publish_inline_comments_fallback_with_verification(comments)
            except Exception as e:
                get_logger().error(f"Failed to publish inline code comments fallback, error: {e}")
                raise e    
    
    def get_review_thread_comments(self, comment_id: int) -> list[dict]:
        """
        Retrieves all comments in the same thread as the given comment.
        
        Args:
            comment_id: Review comment ID
                
        Returns:
            List of comments in the same thread
        """
        try:
            # Fetch all comments with a single API call
            all_comments = list(self.pr.get_comments())
            
            # Find the target comment by ID
            target_comment = next((c for c in all_comments if c.id == comment_id), None)
            if not target_comment:
                return []
        
            # Get root comment id
            root_comment_id = target_comment.raw_data.get("in_reply_to_id", target_comment.id)
            # Build the thread - include the root comment and all replies to it
            thread_comments = [
                c for c in all_comments if
                c.id == root_comment_id or c.raw_data.get("in_reply_to_id") == root_comment_id
            ]
        
        
            return thread_comments
                
        except Exception as e:
            get_logger().exception(f"Failed to get review comments for an inline ask command", artifact={"comment_id": comment_id, "error": e})
            return []

    def _publish_inline_comments_fallback_with_verification(self, comments: list[dict]):
        """
        Check each inline comment separately against the GitHub API and discard of invalid comments,
        then publish all the remaining valid comments in a single review.
        For invalid comments, also try removing the suggestion part and posting the comment just on the first line.
        """
        verified_comments, invalid_comments = self._verify_code_comments(comments)

        # publish as a group the verified comments
        if verified_comments:
            try:
                self.pr.create_review(commit=self.last_commit_id, comments=verified_comments)
            except:
                pass

        # try to publish one by one the invalid comments as a one-line code comment
        if invalid_comments and get_settings().github.try_fix_invalid_inline_comments:
            fixed_comments_as_one_liner = self._try_fix_invalid_inline_comments(
                [comment for comment, _ in invalid_comments])
            for comment in fixed_comments_as_one_liner:
                try:
                    self.publish_inline_comments([comment], disable_fallback=True)
                    get_logger().info(f"Published invalid comment as a single line comment: {comment}")
                except:
                    get_logger().error(f"Failed to publish invalid comment as a single line comment: {comment}")

    def _verify_code_comment(self, comment: dict):
        is_verified = False
        e = None
        try:
            # event ="" # By leaving this blank, you set the review action state to PENDING
            input = dict(commit_id=self.last_commit_id.sha, comments=[comment])
            headers, data = self.pr._requester.requestJsonAndCheck(
                "POST", f"{self.pr.url}/reviews", input=input)
            pending_review_id = data["id"]
            is_verified = True
        except Exception as err:
            is_verified = False
            pending_review_id = None
            e = err
        if pending_review_id is not None:
            try:
                self.pr._requester.requestJsonAndCheck("DELETE", f"{self.pr.url}/reviews/{pending_review_id}")
            except Exception:
                pass
        return is_verified, e

    def _verify_code_comments(self, comments: list[dict]) -> tuple[list[dict], list[tuple[dict, Exception]]]:
        """Very each comment against the GitHub API and return 2 lists: 1 of verified and 1 of invalid comments"""
        verified_comments = []
        invalid_comments = []
        for comment in comments:
            time.sleep(1)  # for avoiding secondary rate limit
            is_verified, e = self._verify_code_comment(comment)
            if is_verified:
                verified_comments.append(comment)
            else:
                invalid_comments.append((comment, e))
        return verified_comments, invalid_comments

    def _try_fix_invalid_inline_comments(self, invalid_comments: list[dict]) -> list[dict]:
        """
        Try fixing invalid comments by removing the suggestion part and setting the comment just on the first line.
        Return only comments that have been modified in some way.
        This is a best-effort attempt to fix invalid comments, and should be verified accordingly.
        """
        import copy
        fixed_comments = []
        for comment in invalid_comments:
            try:
                fixed_comment = copy.deepcopy(comment)  # avoid modifying the original comment dict for later logging
                if "```suggestion" in comment["body"]:
                    fixed_comment["body"] = comment["body"].split("```suggestion")[0]
                if "start_line" in comment:
                    fixed_comment["line"] = comment["start_line"]
                    del fixed_comment["start_line"]
                if "start_side" in comment:
                    fixed_comment["side"] = comment["start_side"]
                    del fixed_comment["start_side"]
                if fixed_comment != comment:
                    fixed_comments.append(fixed_comment)
            except Exception as e:
                get_logger().error(f"Failed to fix inline comment, error: {e}")
        return fixed_comments

    _BOT_REVIEW_COMMENTS_QUERY = """
    query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$number) {
          reviewThreads(first:100, after:$cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              comments(first:100) {
                nodes {
                  databaseId
                  body
                  path
                  line
                  startLine
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """

    def _graphql_url(self) -> str:
        # On GHES the REST base is `.../api/v3` but GraphQL lives at `.../api/graphql`.
        # github.com has no `/v3` suffix, so the fallback covers it.
        base = self.base_url
        if base.endswith("/api/v3"):
            return base[: -len("/v3")] + "/graphql"
        return f"{base}/graphql"

    def get_bot_review_comments(self) -> list[dict]:
        """
        Return the bot's existing inline review comments on this PR.

        Uses GraphQL to expose per-thread resolution state and the thread node id
        (needed by resolve_review_thread / unresolve_review_thread). Filters by
        author to avoid matching human reviewers. Returns dicts with keys:
        id, thread_id, body, path, line, start_line, is_resolved.
        """
        try:
            our_app_name = (get_settings().get("GITHUB.APP_NAME", "") or "").lower()
            bot_user_id = (self.get_user_id() or "").lower() if self.deployment_type == "user" else ""
            owner, _, name = self.repo.partition("/")
            number = self.pr.number

            out: list[dict] = []
            cursor: str | None = None
            while True:
                _, data = self.pr._requester.requestJsonAndCheck(
                    "POST",
                    self._graphql_url(),
                    input={
                        "query": self._BOT_REVIEW_COMMENTS_QUERY,
                        "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                    },
                )
                if not data or data.get("errors"):
                    get_logger().warning(
                        f"get_bot_review_comments GraphQL errors: {(data or {}).get('errors')}"
                    )
                    return []
                threads = (((data.get("data") or {}).get("repository") or {})
                           .get("pullRequest") or {}).get("reviewThreads") or {}
                page_info = threads.get("pageInfo") or {}
                for t in threads.get("nodes") or []:
                    thread_id = t.get("id")
                    is_resolved = bool(t.get("isResolved"))
                    for c in ((t.get("comments") or {}).get("nodes") or []):
                        login = ((c.get("author") or {}).get("login") or "").lower()
                        same_author = False
                        if self.deployment_type == "app":
                            same_author = bool(our_app_name) and our_app_name in login
                        elif self.deployment_type == "user":
                            same_author = bool(bot_user_id) and login == bot_user_id
                        if not same_author:
                            continue
                        comment_id = c.get("databaseId")
                        if not _is_valid_review_comment_id(comment_id):
                            get_logger().warning(
                                f"Skipping GitHub review comment with invalid databaseId: {comment_id!r}"
                            )
                            continue
                        out.append({
                            "id": comment_id,
                            "thread_id": thread_id,
                            "body": c.get("body") or "",
                            "path": c.get("path"),
                            "line": c.get("line"),
                            "start_line": c.get("startLine"),
                            "is_resolved": is_resolved,
                        })
                cursor = page_info.get("endCursor")
                if not page_info.get("hasNextPage") or not cursor:
                    break
            return out
        except Exception as e:
            get_logger().warning(f"Failed to list GitHub review comments via GraphQL: {e}")
            return []

    def edit_review_comment(self, comment_id, body: str) -> bool:
        if not _is_valid_review_comment_id(comment_id):
            get_logger().warning(f"Skipping GitHub review comment edit with invalid id: {comment_id!r}")
            return False
        try:
            body = self.limit_output_characters(body, self.max_comment_chars)
            self.pr._requester.requestJsonAndCheck(
                "PATCH",
                f"{self.base_url}/repos/{self.repo}/pulls/comments/{comment_id}",
                input={"body": body},
            )
            return True
        except Exception as e:
            get_logger().warning(f"Failed to edit GitHub review comment {comment_id}: {e}")
            return False

    _RESOLVE_THREAD_MUTATION = """
    mutation($threadId:ID!) {
      resolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
    }
    """

    _UNRESOLVE_THREAD_MUTATION = """
    mutation($threadId:ID!) {
      unresolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
    }
    """

    def _run_thread_mutation(self, query: str, comment: dict) -> bool:
        thread_id = comment.get("thread_id")
        if not thread_id:
            return False
        try:
            _, data = self.pr._requester.requestJsonAndCheck(
                "POST",
                self._graphql_url(),
                input={"query": query, "variables": {"threadId": thread_id}},
            )
            if not data or data.get("errors"):
                get_logger().warning(
                    f"GitHub thread mutation errors for {thread_id}: {(data or {}).get('errors')}"
                )
                return False
            return True
        except Exception as e:
            get_logger().warning(f"GitHub thread mutation failed for {thread_id}: {e}")
            return False

    def resolve_review_thread(self, comment: dict) -> bool:
        return self._run_thread_mutation(self._RESOLVE_THREAD_MUTATION, comment)

    def unresolve_review_thread(self, comment: dict) -> bool:
        return self._run_thread_mutation(self._UNRESOLVE_THREAD_MUTATION, comment)

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as review comments on the PR.

        When `pr_code_suggestions.persistent_inline_comments` is 'update' (default)
        or 'skip', a stable marker is embedded in each body so subsequent runs
        can recognize and update (or skip) the existing comment rather than
        creating duplicates.
        """
        code_suggestions_validated = self.validate_comments_inside_hunks(code_suggestions)

        mode = normalize_persistent_mode(
            get_settings().pr_code_suggestions.get("persistent_inline_comments", PERSISTENT_MODE_OFF)
        )
        resolve_outdated = _normalize_bool_config(
            get_settings().pr_code_suggestions.get("resolve_outdated_inline_comments", True),
            default=True,
            setting_name="pr_code_suggestions.resolve_outdated_inline_comments",
        )

        existing_index: dict[str, list[dict]] = {}
        if mode != PERSISTENT_MODE_OFF:
            try:
                existing_index = build_marker_index(self.get_bot_review_comments())
            except Exception as e:
                get_logger().warning(f"persistent_inline_comments: fetch failed, falling back to create-new: {e}")
                existing_index = {}

        reused_comment_ids: set[int] = set()
        post_parameters_list = []
        for suggestion in code_suggestions_validated:
            body = suggestion["body"]
            relevant_file = suggestion["relevant_file"]
            relevant_lines_start = suggestion["relevant_lines_start"]
            relevant_lines_end = suggestion["relevant_lines_end"]

            if not relevant_lines_start or relevant_lines_start == -1:
                get_logger().exception(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}")
                continue
            if relevant_lines_end < relevant_lines_start:
                get_logger().exception(
                    f"Failed to publish code suggestion, "
                    f"relevant_lines_end is {relevant_lines_end} and "
                    f"relevant_lines_start is {relevant_lines_start}")
                continue

            if mode != PERSISTENT_MODE_OFF:
                marker = generate_marker(suggestion.get("original_suggestion") or suggestion)
                if marker:
                    body = append_marker(body, marker)
                    marker_hash = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
                    existing = find_comment_by_location(
                        existing_index.get(marker_hash, []),
                        relevant_file,
                        relevant_lines_start,
                        relevant_lines_end,
                    )
                    if existing is not None:
                        existing_id = existing.get("id")
                        if not _is_valid_review_comment_id(existing_id):
                            get_logger().warning(
                                f"persistent_inline_comments: ignoring existing GitHub comment with invalid id "
                                f"{existing_id!r} on {relevant_file}"
                            )
                            existing = None
                        else:
                            existing_id = int(existing_id)
                    if existing is not None:
                        if mode == PERSISTENT_MODE_SKIP:
                            if resolve_outdated and (
                                existing.get("is_resolved")
                                or RESOLVED_BODY_MARKER in (existing.get("body") or "")
                            ):
                                if self.edit_review_comment(existing_id, body):
                                    reused_comment_ids.add(existing_id)
                                    if existing.get("is_resolved"):
                                        self.unresolve_review_thread(existing)
                                    continue
                                get_logger().info(
                                    f"persistent_inline_comments=skip: reopen failed for {existing_id}; "
                                    f"falling back to create-new"
                                )
                            else:
                                reused_comment_ids.add(existing_id)
                                get_logger().info(
                                    f"persistent_inline_comments=skip: existing comment {existing_id} "
                                    f"on {relevant_file}; not re-posting")
                                continue
                        else:
                            # mode == update
                            if self.edit_review_comment(existing_id, body):
                                reused_comment_ids.add(existing_id)
                                # If we previously auto-resolved this thread but the
                                # suggestion is back, unresolve it.
                                if resolve_outdated and existing.get("is_resolved"):
                                    self.unresolve_review_thread(existing)
                                continue
                            get_logger().info(
                                f"persistent_inline_comments=update: edit failed for {existing_id}; "
                                f"falling back to create-new")
                    elif mode == PERSISTENT_MODE_SKIP:
                        # No same-location match, so allow a new inline comment and
                        # let the outdated pass resolve any stale thread with this marker.
                        pass

            if relevant_lines_end > relevant_lines_start:
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_end,
                    "start_line": relevant_lines_start,
                    "start_side": "RIGHT",
                }
            else:
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_start,
                    "side": "RIGHT",
                }
            post_parameters_list.append(post_parameters)

        # ---- Outdated pass: resolve threads whose marker is no longer emitted ----
        if mode != PERSISTENT_MODE_OFF and resolve_outdated:
            for candidates in existing_index.values():
                for c in candidates:
                    comment_id = c.get("id")
                    if not _is_valid_review_comment_id(comment_id):
                        get_logger().warning(
                            f"resolve_outdated_inline_comments: skipping GitHub comment with invalid id "
                            f"{comment_id!r}"
                        )
                        continue
                    comment_id = int(comment_id)
                    if comment_id in reused_comment_ids:
                        continue
                    if c.get("is_resolved"):
                        continue
                    if RESOLVED_BODY_MARKER in (c.get("body") or ""):
                        continue
                    # Per-iteration guard so a single failure cannot propagate; criterion 8 of resolve-outdated-inline-comments.
                    try:
                        if not self.resolve_review_thread(c):
                            continue
                        if not self.edit_review_comment(comment_id, format_resolved_body(c.get("body") or "")):
                            get_logger().warning(
                                f"resolve_outdated_inline_comments: failed to write resolved marker for "
                                f"GitHub comment {comment_id}; attempting to unresolve thread"
                            )
                            if not self.unresolve_review_thread(c):
                                get_logger().warning(
                                    f"resolve_outdated_inline_comments: failed to unresolve GitHub thread "
                                    f"after marker write failed for comment {comment_id}"
                                )
                    except Exception as e:
                        get_logger().warning(
                            f"resolve_outdated_inline_comments: outdated pass failed for {comment_id}: {e}"
                        )

        if not post_parameters_list:
            return True

        try:
            self.publish_inline_comments(post_parameters_list)
            return True
        except Exception as e:
            get_logger().error(f"Failed to publish code suggestion, error: {e}")
            return False

    def edit_comment(self, comment, body: str):
        try:
            body = self.limit_output_characters(body, self.max_comment_chars)
            comment.edit(body=body)
        except GithubException as e:
            if hasattr(e, "status") and e.status == 403:
                # Log as warning for permission-related issues (usually due to polling)
                get_logger().warning(
                    "Failed to edit github comment due to permission restrictions",
                    artifact={"error": e})
            else:
                get_logger().exception(f"Failed to edit github comment", artifact={"error": e})

    def edit_comment_from_comment_id(self, comment_id: int, body: str):
        try:
            # self.pr.get_issue_comment(comment_id).edit(body)
            body = self.limit_output_characters(body, self.max_comment_chars)
            headers, data_patch = self.pr._requester.requestJsonAndCheck(
                "PATCH", f"{self.base_url}/repos/{self.repo}/issues/comments/{comment_id}",
                input={"body": body}
            )
        except Exception as e:
            get_logger().exception(f"Failed to edit comment, error: {e}")

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        try:
            # self.pr.get_issue_comment(comment_id).edit(body)
            body = self.limit_output_characters(body, self.max_comment_chars)
            headers, data_patch = self.pr._requester.requestJsonAndCheck(
                "POST", f"{self.base_url}/repos/{self.repo}/pulls/{self.pr_num}/comments/{comment_id}/replies",
                input={"body": body}
            )
        except Exception as e:
            get_logger().exception(f"Failed to reply comment, error: {e}")

    def get_comment_body_from_comment_id(self, comment_id: int):
        try:
            # self.pr.get_issue_comment(comment_id).edit(body)
            headers, data_patch = self.pr._requester.requestJsonAndCheck(
                "GET", f"{self.base_url}/repos/{self.repo}/issues/comments/{comment_id}"
            )
            return data_patch.get("body","")
        except Exception as e:
            get_logger().exception(f"Failed to edit comment, error: {e}")
            return None

    def publish_file_comments(self, file_comments: list) -> bool:
        try:
            headers, existing_comments = self.pr._requester.requestJsonAndCheck(
                "GET", f"{self.pr.url}/comments"
            )
            for comment in file_comments:
                comment['commit_id'] = self.last_commit_id.sha
                comment['body'] = self.limit_output_characters(comment['body'], self.max_comment_chars)

                found = False
                for existing_comment in existing_comments:
                    comment['commit_id'] = self.last_commit_id.sha
                    our_app_name = get_settings().get("GITHUB.APP_NAME", "")
                    same_comment_creator = False
                    if self.deployment_type == 'app':
                        same_comment_creator = our_app_name.lower() in existing_comment['user']['login'].lower()
                    elif self.deployment_type == 'user':
                        same_comment_creator = self.github_user_id == existing_comment['user']['login']
                    if existing_comment['subject_type'] == 'file' and comment['path'] == existing_comment['path'] and same_comment_creator:

                        headers, data_patch = self.pr._requester.requestJsonAndCheck(
                            "PATCH", f"{self.base_url}/repos/{self.repo}/pulls/comments/{existing_comment['id']}", input={"body":comment['body']}
                        )
                        found = True
                        break
                if not found:
                    headers, data_post = self.pr._requester.requestJsonAndCheck(
                        "POST", f"{self.pr.url}/comments", input=comment
                    )
            return True
        except Exception as e:
            get_logger().error(f"Failed to publish diffview file summary, error: {e}")
            return False

    def remove_initial_comment(self):
        try:
            for comment in getattr(self.pr, 'comments_list', []):
                if comment.is_temporary:
                    self.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove initial comment, error: {e}")

    def remove_comment(self, comment):
        try:
            comment.delete()
        except Exception as e:
            get_logger().exception(f"Failed to remove comment, error: {e}")

    def get_title(self):
        return self.pr.title

    def get_languages(self):
        languages = self._get_repo().get_languages()
        return languages

    def get_pr_branch(self):
        return self.pr.head.ref

    def get_pr_owner_id(self) -> str | None:
        if not self.repo:
            return None
        return self.repo.split('/')[0]

    def get_pr_description_full(self):
        return self.pr.body

    def get_user_id(self):
        if not self.github_user_id:
            try:
                self.github_user_id = self.github_client.get_user().raw_data['login']
            except Exception as e:
                self.github_user_id = ""
                # logging.exception(f"Failed to get user id, error: {e}")
        return self.github_user_id

    def get_notifications(self, since: datetime):
        deployment_type = get_settings().get("GITHUB.DEPLOYMENT_TYPE", "user")

        if deployment_type != 'user':
            raise ValueError("Deployment mode must be set to 'user' to get notifications")

        notifications = self.github_client.get_user().get_notifications(since=since)
        return notifications

    def get_issue_comments(self):
        return self.pr.get_issue_comments()

    def get_repo_settings(self):
        try:
            # contents = self.repo_obj.get_contents(".pr_agent.toml", ref=self.pr.head.sha).decoded_content

            # more logical to take 'pr_agent.toml' from the default branch
            contents = self.repo_obj.get_contents(".pr_agent.toml").decoded_content
            return contents
        except Exception as e:
            get_logger().warning(f"Failed to load .pr_agent.toml file, error: {e}")
            return ""

    def get_repo_file_content(self, file_path: str):
        try:
            contents = self.repo_obj.get_contents(file_path).decoded_content
            if isinstance(contents, bytes):
                return contents.decode("utf-8", errors="replace")
            return contents
        except Exception as e:
            get_logger().warning(f"Failed to load repo file: {file_path}, error: {e}")
            return ""

    def get_workspace_name(self):
        return self.repo.split('/')[0]

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        if disable_eyes:
            return None
        try:
            headers, data_patch = self.pr._requester.requestJsonAndCheck(
                "POST", f"{self.base_url}/repos/{self.repo}/issues/comments/{issue_comment_id}/reactions",
                input={"content": "eyes"}
            )
            return data_patch.get("id", None)
        except Exception as e:
            get_logger().warning(f"Failed to add eyes reaction, error: {e}")
            return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: str) -> bool:
        try:
            # self.pr.get_issue_comment(issue_comment_id).delete_reaction(reaction_id)
            headers, data_patch = self.pr._requester.requestJsonAndCheck(
                "DELETE",
                f"{self.base_url}/repos/{self.repo}/issues/comments/{issue_comment_id}/reactions/{reaction_id}"
            )
            return True
        except Exception as e:
            get_logger().exception(f"Failed to remove eyes reaction, error: {e}")
            return False

    def _parse_pr_url(self, pr_url: str) -> Tuple[str, int]:
        parsed_url = urlparse(pr_url)

        if parsed_url.path.startswith('/api/v3'):
            parsed_url = urlparse(pr_url.replace("/api/v3", ""))

        path_parts = parsed_url.path.strip('/').split('/')
        if 'api.github.com' in parsed_url.netloc or '/api/v3' in pr_url:
            if len(path_parts) < 5 or path_parts[3] != 'pulls':
                raise ValueError("The provided URL does not appear to be a GitHub PR URL")
            repo_name = '/'.join(path_parts[1:3])
            try:
                pr_number = int(path_parts[4])
            except ValueError as e:
                raise ValueError("Unable to convert PR number to integer") from e
            return repo_name, pr_number

        if len(path_parts) < 4 or path_parts[2] != 'pull':
            raise ValueError("The provided URL does not appear to be a GitHub PR URL")

        repo_name = '/'.join(path_parts[:2])
        try:
            pr_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError("Unable to convert PR number to integer") from e

        return repo_name, pr_number

    def _parse_issue_url(self, issue_url: str) -> Tuple[str, int]:
        parsed_url = urlparse(issue_url)

        if parsed_url.path.startswith('/api/v3'): #Check if came from github app
            parsed_url = urlparse(issue_url.replace("/api/v3", ""))

        path_parts = parsed_url.path.strip('/').split('/')
        if 'api.github.com' in parsed_url.netloc or '/api/v3' in issue_url: #Check if came from github app
            if len(path_parts) < 5 or path_parts[3] != 'issues':
                raise ValueError("The provided URL does not appear to be a GitHub ISSUE URL")
            repo_name = '/'.join(path_parts[1:3])
            try:
                issue_number = int(path_parts[4])
            except ValueError as e:
                raise ValueError("Unable to convert issue number to integer") from e
            return repo_name, issue_number

        if len(path_parts) < 4 or path_parts[2] != 'issues':
            raise ValueError("The provided URL does not appear to be a GitHub PR issue")

        repo_name = '/'.join(path_parts[:2])
        try:
            issue_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError("Unable to convert issue number to integer") from e

        return repo_name, issue_number

    def _get_github_client(self):
        self.deployment_type = get_settings().get("GITHUB.DEPLOYMENT_TYPE", "user")
        self.auth = None
        if self.deployment_type == 'app':
            try:
                private_key = get_settings().github.private_key
                app_id = get_settings().github.app_id
            except AttributeError as e:
                raise ValueError("GitHub app ID and private key are required when using GitHub app deployment") from e
            if not self.installation_id:
                raise ValueError("GitHub app installation ID is required when using GitHub app deployment")
            auth = AppAuthentication(app_id=app_id, private_key=private_key,
                                     installation_id=self.installation_id)
            self.auth = auth
        elif self.deployment_type == 'user':
            try:
                token = get_settings().github.user_token
            except AttributeError as e:
                raise ValueError(
                    "GitHub token is required when using user deployment. See: "
                    "https://github.com/Codium-ai/pr-agent#method-2-run-from-source") from e
            self.auth = Auth.Token(token)
        if self.auth:
            return Github(auth=self.auth, base_url=self.base_url)
        else:
            raise ValueError("Could not authenticate to GitHub")

    def _get_repo(self):
        if hasattr(self, 'repo_obj') and \
                hasattr(self.repo_obj, 'full_name') and \
                self.repo_obj.full_name == self.repo:
            return self.repo_obj
        else:
            self.repo_obj = self.github_client.get_repo(self.repo)
            return self.repo_obj


    def _get_pr(self):
        return self._get_repo().get_pull(self.pr_num)

    def get_pr_file_content(self, file_path: str, branch: str) -> str:
        try:
            file_content_str = str(
                self._get_repo()
                .get_contents(file_path, ref=branch)
                .decoded_content.decode()
            )
        except Exception:
            file_content_str = ""
        return file_content_str

    def create_or_update_pr_file(
        self, file_path: str, branch: str, contents="", message=""
    ) -> None:
        try:
            file_obj = self._get_repo().get_contents(file_path, ref=branch)
            sha1=file_obj.sha
        except Exception:
            sha1=""
        self.repo_obj.update_file(
            path=file_path,
            message=message,
            content=contents,
            sha=sha1,
            branch=branch,
        )

    def _get_pr_file_content(self, file: FilePatchInfo, sha: str) -> str:
        return self.get_pr_file_content(file.filename, sha)

    def publish_labels(self, pr_types):
        try:
            label_color_map = {"Bug fix": "1d76db", "Tests": "e99695", "Bug fix with tests": "c5def5",
                               "Enhancement": "bfd4f2", "Documentation": "d4c5f9",
                               "Other": "d1bcf9"}
            post_parameters = []
            for p in pr_types:
                color = label_color_map.get(p, "d1bcf9")  # default to "Other" color
                post_parameters.append({"name": p, "color": color})
            headers, data = self.pr._requester.requestJsonAndCheck(
                "PUT", f"{self.pr.issue_url}/labels", input=post_parameters
            )
        except Exception as e:
            get_logger().warning(f"Failed to publish labels, error: {e}")

    def get_pr_labels(self, update=False):
        try:
            if not update:
                labels =self.pr.labels
                return [label.name for label in labels]
            else: # obtain the latest labels. Maybe they changed while the AI was running
                headers, labels = self.pr._requester.requestJsonAndCheck(
                    "GET", f"{self.pr.issue_url}/labels")
                return [label['name'] for label in labels]

        except Exception as e:
            get_logger().exception(f"Failed to get labels, error: {e}")
            return []

    def get_repo_labels(self):
        labels = self.repo_obj.get_labels()
        return [label for label in itertools.islice(labels, 50)]

    def get_commit_messages(self):
        """
        Retrieves the commit messages of a pull request.

        Returns:
            str: A string containing the commit messages of the pull request.
        """
        max_tokens = get_settings().get("CONFIG.MAX_COMMITS_TOKENS", None)
        try:
            commit_list = self.pr.get_commits()
            commit_messages = [commit.commit.message for commit in commit_list]
            commit_messages_str = "\n".join([f"{i + 1}. {message}" for i, message in enumerate(commit_messages)])
        except Exception:
            commit_messages_str = ""
        if max_tokens:
            commit_messages_str = clip_tokens(commit_messages_str, max_tokens)
        return commit_messages_str

    def generate_link_to_relevant_line_number(self, suggestion) -> str:
        try:
            relevant_file = suggestion['relevant_file'].strip('`').strip("'").strip('\n')
            relevant_line_str = suggestion['relevant_line'].strip('\n')
            if not relevant_line_str:
                return ""

            position, absolute_position = find_line_number_of_relevant_line_in_file \
                (self.diff_files, relevant_file, relevant_line_str)

            if absolute_position != -1:
                # # link to right file only
                # link = f"https://github.com/{self.repo}/blob/{self.pr.head.sha}/{relevant_file}" \
                #        + "#" + f"L{absolute_position}"

                # link to diff
                sha_file = hashlib.sha256(relevant_file.encode('utf-8')).hexdigest()
                link = f"{self.base_url_html}/{self.repo}/pull/{self.pr_num}/files#diff-{sha_file}R{absolute_position}"
                return link
        except Exception as e:
            get_logger().info(f"Failed adding line link, error: {e}")

        return ""

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        sha_file = hashlib.sha256(relevant_file.encode('utf-8')).hexdigest()
        if relevant_line_start == -1:
            link = f"{self.base_url_html}/{self.repo}/pull/{self.pr_num}/files#diff-{sha_file}"
        elif relevant_line_end:
            link = f"{self.base_url_html}/{self.repo}/pull/{self.pr_num}/files#diff-{sha_file}R{relevant_line_start}-R{relevant_line_end}"
        else:
            link = f"{self.base_url_html}/{self.repo}/pull/{self.pr_num}/files#diff-{sha_file}R{relevant_line_start}"
        return link

    def get_lines_link_original_file(self, filepath: str, component_range: Range) -> str:
        """
        Returns the link to the original file on GitHub that corresponds to the given filepath and component range.

        Args:
            filepath (str): The path of the file.
            component_range (Range): The range of lines that represent the component.

        Returns:
            str: The link to the original file on GitHub.

        Example:
            >>> filepath = "path/to/file.py"
            >>> component_range = Range(line_start=10, line_end=20)
            >>> link = get_lines_link_original_file(filepath, component_range)
            >>> print(link)
            "https://github.com/{repo}/blob/{commit_sha}/{filepath}/#L11-L21"
        """
        line_start = component_range.line_start + 1
        line_end = component_range.line_end + 1
        # link = (f"https://github.com/{self.repo}/blob/{self.last_commit_id.sha}/{filepath}/"
        #         f"#L{line_start}-L{line_end}")
        link = (f"{self.base_url_html}/{self.repo}/blob/{self.last_commit_id.sha}/{filepath}/"
                f"#L{line_start}-L{line_end}")

        return link

    def get_pr_id(self):
        try:
            pr_id = f"{self.repo}/{self.pr_num}"
            return pr_id
        except:
            return ""

    def fetch_sub_issues(self, issue_url):
        """
        Fetch sub-issues linked to the given GitHub issue URL using GraphQL via PyGitHub.
        """
        sub_issues = set()

        # Extract owner, repo, and issue number from URL
        parts = issue_url.rstrip("/").split("/")
        owner, repo, issue_number = parts[-4], parts[-3], parts[-1]

        try:
            # Gets Issue ID from Issue Number
            query = f"""
            query {{
                repository(owner: "{owner}", name: "{repo}") {{
                    issue(number: {issue_number}) {{
                        id
                    }}
                }}
            }}
            """
            response_tuple = self.github_client._Github__requester.requestJson("POST", "/graphql",
                                                                               input={"query": query})

            # Extract the JSON response from the tuple and parses it
            if isinstance(response_tuple, tuple) and len(response_tuple) == 3:
                response_json = json.loads(response_tuple[2])
            else:
                get_logger().error(f"Unexpected response format: {response_tuple}")
                return sub_issues


            issue_id = response_json.get("data", {}).get("repository", {}).get("issue", {}).get("id")

            if not issue_id:
                get_logger().warning(f"Issue ID not found for {issue_url}")
                return sub_issues

            # Fetch Sub-Issues
            sub_issues_query = f"""
            query {{
                node(id: "{issue_id}") {{
                    ... on Issue {{
                        subIssues(first: 10) {{
                            nodes {{
                                url
                            }}
                        }}
                    }}
                }}
            }}
            """
            sub_issues_response_tuple = self.github_client._Github__requester.requestJson("POST", "/graphql", input={
                "query": sub_issues_query})

            # Extract the JSON response from the tuple and parses it
            if isinstance(sub_issues_response_tuple, tuple) and len(sub_issues_response_tuple) == 3:
                sub_issues_response_json = json.loads(sub_issues_response_tuple[2])
            else:
                get_logger().error("Unexpected sub-issues response format", artifact={"response": sub_issues_response_tuple})
                return sub_issues

            if not sub_issues_response_json.get("data", {}).get("node", {}).get("subIssues"):
                get_logger().error("Invalid sub-issues response structure")
                return sub_issues
    
            nodes = sub_issues_response_json.get("data", {}).get("node", {}).get("subIssues", {}).get("nodes", [])
            get_logger().info(f"Github Sub-issues fetched: {len(nodes)}", artifact={"nodes": nodes})

            for sub_issue in nodes:
                if "url" in sub_issue:
                    sub_issues.add(sub_issue["url"])

        except Exception as e:
            get_logger().exception(f"Failed to fetch sub-issues. Error: {e}")

        return sub_issues

    def auto_approve(self) -> bool:
        try:
            res = self.pr.create_review(event="APPROVE")
            if res.state == "APPROVED":
                return True
            return False
        except Exception as e:
            get_logger().exception(f"Failed to auto-approve, error: {e}")
            return False

    def calc_pr_statistics(self, pull_request_data: dict):
            return {}

    def validate_comments_inside_hunks(self, code_suggestions):
        """
        validate that all committable comments are inside PR hunks - this is a must for committable comments in GitHub
        """
        code_suggestions_copy = copy.deepcopy(code_suggestions)
        diff_files = self.get_diff_files()
        RE_HUNK_HEADER = re.compile(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")

        diff_files = set_file_languages(diff_files)

        for suggestion in code_suggestions_copy:
            try:
                relevant_file_path = suggestion['relevant_file']
                for file in diff_files:
                    if file.filename == relevant_file_path:

                        # generate on-demand the patches range for the relevant file
                        patch_str = file.patch
                        if not hasattr(file, 'patches_range'):
                            file.patches_range = []
                            patch_lines = patch_str.splitlines()
                            for i, line in enumerate(patch_lines):
                                if line.startswith('@@'):
                                    match = RE_HUNK_HEADER.match(line)
                                    # identify hunk header
                                    if match:
                                        section_header, size1, size2, start1, start2 = extract_hunk_headers(match)
                                        file.patches_range.append({'start': start2, 'end': start2 + size2 - 1})

                        patches_range = file.patches_range
                        comment_start_line = suggestion.get('relevant_lines_start', None)
                        comment_end_line = suggestion.get('relevant_lines_end', None)
                        original_suggestion = suggestion.get('original_suggestion', None) # needed for diff code
                        if not comment_start_line or not comment_end_line or not original_suggestion:
                            continue

                        # check if the comment is inside a valid hunk
                        is_valid_hunk = False
                        min_distance = float('inf')
                        patch_range_min = None
                        # find the hunk that contains the comment, or the closest one
                        for i, patch_range in enumerate(patches_range):
                            d1 = comment_start_line - patch_range['start']
                            d2 = patch_range['end'] - comment_end_line
                            if d1 >= 0 and d2 >= 0:  # found a valid hunk
                                is_valid_hunk = True
                                min_distance = 0
                                patch_range_min = patch_range
                                break
                            elif d1 * d2 <= 0:  # comment is possibly inside the hunk
                                d1_clip = abs(min(0, d1))
                                d2_clip = abs(min(0, d2))
                                d = max(d1_clip, d2_clip)
                                if d < min_distance:
                                    patch_range_min = patch_range
                                    min_distance = min(min_distance, d)
                        if not is_valid_hunk:
                            if min_distance < 10:  # 10 lines - a reasonable distance to consider the comment inside the hunk
                                # make the suggestion non-committable, yet multi line
                                suggestion['relevant_lines_start'] = max(suggestion['relevant_lines_start'], patch_range_min['start'])
                                suggestion['relevant_lines_end'] = min(suggestion['relevant_lines_end'], patch_range_min['end'])
                                body = suggestion['body'].strip()

                                # present new diff code in collapsible
                                existing_code = original_suggestion['existing_code'].rstrip() + "\n"
                                improved_code = original_suggestion['improved_code'].rstrip() + "\n"
                                diff = difflib.unified_diff(existing_code.split('\n'),
                                                            improved_code.split('\n'), n=999)
                                patch_orig = "\n".join(diff)
                                patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')
                                diff_code = f"\n\n<details><summary>New proposed code:</summary>\n\n```diff\n{patch.rstrip()}\n```"
                                # replace ```suggestion ... ``` with diff_code, using regex:
                                body = re.sub(r'```suggestion.*?```', diff_code, body, flags=re.DOTALL)
                                body += "\n\n</details>"
                                suggestion['body'] = body
                                get_logger().info(f"Comment was moved to a valid hunk, "
                                                  f"start_line={suggestion['relevant_lines_start']}, end_line={suggestion['relevant_lines_end']}, file={file.filename}")
                            else:
                                get_logger().error(f"Comment is not inside a valid hunk, "
                                                   f"start_line={suggestion['relevant_lines_start']}, end_line={suggestion['relevant_lines_end']}, file={file.filename}")
            except Exception as e:
                get_logger().error(f"Failed to process patch for committable comment, error: {e}")
        return code_suggestions_copy

    #Clone related
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        scheme = "https://"

        #For example, to clone:
        #https://github.com/Codium-ai/pr-agent-pro.git
        #Need to embed inside the github token:
        #https://<token>@github.com/Codium-ai/pr-agent-pro.git

        github_token = self.auth.token
        github_base_url = self.base_url_html
        if not all([github_token, github_base_url]):
            get_logger().error("Either missing auth token or missing base url")
            return None
        if scheme not in github_base_url:
            get_logger().error(f"Base url: {github_base_url} is missing prefix: {scheme}")
            return None
        github_com = github_base_url.split(scheme)[1]  # e.g. 'github.com' or github.<org>.com
        if not github_com:
            get_logger().error(f"Base url: {github_base_url} has an empty base url")
            return None
        if github_com not in repo_url_to_clone:
            get_logger().error(f"url to clone: {repo_url_to_clone} does not contain {github_com}")
            return None
        repo_full_name = repo_url_to_clone.split(github_com)[-1]
        if not repo_full_name:
            get_logger().error(f"url to clone: {repo_url_to_clone} is malformed")
            return None

        clone_url = scheme
        if self.deployment_type == 'app':
            clone_url += "git:"
        clone_url += f"{github_token}@{github_com}{repo_full_name}"
        return clone_url
