import json
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import giteapy
from giteapy.rest import ApiException

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.git_patch_processing import decode_if_bytes
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import (PRReviewHeader, clip_tokens,
                                 find_line_number_of_relevant_line_in_file,
                                 load_large_diff)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (MAX_FILES_ALLOWED_FULL,
                                                 FilePatchInfo, GitProvider,
                                                 IncrementalPR)
from pr_agent.log import get_logger


class GiteaProvider(GitProvider):
    def __init__(self, url: Optional[str] = None):
        super().__init__()
        self.logger = get_logger()

        if not url:
            self.logger.error("PR URL not provided.")
            raise ValueError("PR URL not provided.")

        self.base_url = get_settings().get("GITEA.URL", "https://gitea.com").rstrip("/")
        self.pr_url = ""
        self.issue_url = ""

        self.gitea_access_token = get_settings().get("GITEA.PERSONAL_ACCESS_TOKEN", None)
        if not self.gitea_access_token:
            self.logger.error("Gitea access token not found in settings.")
            raise ValueError("Gitea access token not found in settings.")

        self.repo_settings = get_settings().get("GITEA.REPO_SETTING", None)
        configuration = giteapy.Configuration()
        configuration.host = "{}/api/v1".format(self.base_url)
        configuration.api_key['Authorization'] = f'token {self.gitea_access_token}'

        if get_settings().get("GITEA.SKIP_SSL_VERIFICATION", False):
            configuration.verify_ssl = False

        # Use custom cert (self-signed)
        configuration.ssl_ca_cert = get_settings().get("GITEA.SSL_CA_CERT", None)

        client = giteapy.ApiClient(configuration)
        self.repo_api = RepoApi(client)
        self.owner = None
        self.repo = None
        self.pr_number = None
        self.issue_number = None
        self.max_comment_chars = 65000
        self.enabled_pr = False
        self.enabled_issue = False
        self.temp_comments = []
        self.pr = None
        self.git_files = []
        self.file_contents = {}
        self.file_diffs = {}
        self.sha = None
        self.diff_files = []
        self.incremental = IncrementalPR(False)
        self.comments_list = []
        self.unreviewed_files_set = dict()

        if "pulls" in url:
            self.pr_url = url
            self.__set_repo_and_owner_from_pr()
            self.enabled_pr = True
            self.pr = self.repo_api.get_pull_request(
                owner=self.owner,
                repo=self.repo,
                pr_number=self.pr_number
            )
            self.git_files = self.repo_api.get_change_file_pull_request(
                owner=self.owner,
                repo=self.repo,
                pr_number=self.pr_number
            )
            # Optional ignore with user custom
            self.git_files = filter_ignored(self.git_files, platform="gitea")

            self.sha = self.pr.head.sha if self.pr.head.sha else ""
            self.__add_file_content()
            self.__add_file_diff()
            self.pr_commits = self.repo_api.list_all_commits(
                owner=self.owner,
                repo=self.repo
            )
            self.last_commit = self.pr_commits[-1]
            self.last_commit_id = self.last_commit
            self.base_sha = self.pr.base.sha if self.pr.base.sha else ""
            self.base_ref = self.pr.base.ref if self.pr.base.ref else ""
            # Gitea's PR object exposes the merge base directly. Prefer it for
            # reading *base* file content: the base branch may have advanced
            # (parallel merges) so base.sha no longer reflects the point the PR
            # actually diverged from. Mirrors github_provider, which reads
            # original content from compare.merge_base_commit.sha. Falls back to
            # base_sha when the field is absent (older Gitea).
            self.merge_base_sha = getattr(self.pr, "merge_base", None) or self.base_sha
        elif "issues" in url:
            self.issue_url = url
            self.__set_repo_and_owner_from_issue()
            self.enabled_issue = True
        else:
            self.pr_commits = None

    def __add_file_content(self):
        for file in self.git_files:
            file_path = file.get("filename")
            # Ignore file from default settings
            if not is_valid_file(file_path):
                continue

            if file_path and self.sha:
                try:
                    content = self.repo_api.get_file_content(
                        owner=self.owner,
                        repo=self.repo,
                        commit_sha=self.sha,
                        filepath=file_path
                    )
                    self.file_contents[file_path] = content
                except ApiException as e:
                    self.logger.error(f"Error getting file content for {file_path}: {str(e)}")
                    self.file_contents[file_path] = ""

    def __add_file_diff(self):
        try:
            diff_contents = self.repo_api.get_pull_request_diff(
                    owner=self.owner,
                    repo=self.repo,
                    pr_number=self.pr_number
            )

            lines = diff_contents.splitlines()
            current_file = None
            current_patch = []
            file_patches = {}
            for line in lines:
                if line.startswith('diff --git'):
                    if current_file and current_patch:
                        file_patches[current_file] = '\n'.join(current_patch)
                        current_patch = []
                    current_file = line.split(' b/')[-1]
                elif line.startswith('@@') and not current_patch:
                    current_patch = [line]
                elif current_patch:
                    current_patch.append(line)

            if current_file and current_patch:
                file_patches[current_file] = '\n'.join(current_patch)

            self.file_diffs = file_patches
        except Exception as e:
            self.logger.error(f"Error getting diff content: {str(e)}")

    def _parse_pr_url(self, pr_url: str) -> Tuple[str, str, int]:
        parsed_url = urlparse(pr_url)

        if parsed_url.path.startswith('/api/v1'):
            parsed_url = urlparse(pr_url.replace("/api/v1", ""))

        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) < 4 or path_parts[2] != 'pulls':
            raise ValueError("The provided URL does not appear to be a Gitea PR URL")

        try:
            pr_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError("Unable to convert PR number to integer") from e

        owner = path_parts[0]
        repo = path_parts[1]

        return owner, repo, pr_number

    def _parse_issue_url(self, issue_url: str) -> Tuple[str, str, int]:
        parsed_url = urlparse(issue_url)

        if parsed_url.path.startswith('/api/v1'):
            parsed_url = urlparse(issue_url.replace("/api/v1", ""))

        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) < 4 or path_parts[2] != 'issues':
            raise ValueError("The provided URL does not appear to be a Gitea issue URL")

        try:
            issue_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError("Unable to convert issue number to integer") from e

        owner = path_parts[0]
        repo = path_parts[1]

        return owner, repo, issue_number

    def __set_repo_and_owner_from_pr(self):
        """Extract owner and repo from the PR URL"""
        try:
            owner, repo, pr_number = self._parse_pr_url(self.pr_url)
            self.owner = owner
            self.repo = repo
            self.pr_number = pr_number
            self.logger.info(f"Owner: {self.owner}, Repo: {self.repo}, PR Number: {self.pr_number}")
        except ValueError as e:
            self.logger.error(f"Error parsing PR URL: {str(e)}")
        except Exception as e:
            self.logger.error(f"Unexpected error: {str(e)}")

    def __set_repo_and_owner_from_issue(self):
        """Extract owner and repo from the issue URL"""
        try:
            owner, repo, issue_number = self._parse_issue_url(self.issue_url)
            self.owner = owner
            self.repo = repo
            self.issue_number = issue_number
            self.logger.info(f"Owner: {self.owner}, Repo: {self.repo}, Issue Number: {self.issue_number}")
        except ValueError as e:
            self.logger.error(f"Error parsing issue URL: {str(e)}")
        except Exception as e:
            self.logger.error(f"Unexpected error: {str(e)}")

    def get_pr_url(self) -> str:
        return self.pr_url

    def get_issue_url(self) -> str:
        return self.issue_url

    def get_latest_commit_url(self) -> str:
        return self.last_commit.html_url

    def get_comment_url(self, comment) -> str:
        return comment.html_url

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        self.publish_persistent_comment_full(pr_comment, initial_header, update_header, name, final_update_message)

    def publish_comment(self, comment: str,is_temporary: bool = False) -> None:
        """Publish a comment to the pull request"""
        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment")
            return None

        if self.enabled_issue:
            index = self.issue_number
        elif self.enabled_pr:
            index = self.pr_number
        else:
            self.logger.error("Neither PR nor issue URL provided.")
            return None

        comment = self.limit_output_characters(comment, self.max_comment_chars)
        response = self.repo_api.create_comment(
            owner=self.owner,
            repo=self.repo,
            index=index,
            comment=comment
        )

        if not response:
            self.logger.error("Failed to publish comment")
            return None

        if is_temporary:
            self.temp_comments.append(comment)

        comment_obj = {
            "is_temporary": is_temporary,
            "comment": comment,
            "comment_id": response.id if isinstance(response, tuple) else response.id
        }
        self.comments_list.append(comment_obj)
        self.logger.info("Comment published")
        return comment_obj

    def edit_comment(self, comment, body : str):
        body = self.limit_output_characters(body, self.max_comment_chars)
        try:
            self.repo_api.edit_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment.get("comment_id") if isinstance(comment, dict) else comment.id,
                comment=body
            )
        except ApiException as e:
            self.logger.error(f"Error editing comment: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return None

    def edit_comment_from_comment_id(self, comment_id: int, body: str):
        """Edit an existing issue/PR comment addressed by its id.

        Mirrors GithubProvider.edit_comment_from_comment_id; unlocks the inline
        ``/ask`` flow, which edits its own placeholder comment in place.
        """
        body = self.limit_output_characters(body, self.max_comment_chars)
        try:
            self.repo_api.edit_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment_id,
                comment=body
            )
        except ApiException as e:
            self.logger.error(f"Error editing comment {comment_id}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")

    def get_comment_body_from_comment_id(self, comment_id: int) -> str:
        """Return the body text of an issue/PR comment addressed by its id."""
        try:
            comment = self.repo_api.get_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment_id
            )
            return comment.get("body", "") if isinstance(comment, dict) else ""
        except ApiException as e:
            self.logger.error(f"Error getting comment {comment_id}: {e}")
            return ""
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return ""

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        """Reply to a comment.

        Gitea has no reply-to-a-specific-comment endpoint (unlike GitHub's
        review-comment replies), so the reply is posted as a new comment on
        the PR — which is where the inline ``/ask`` answer surfaces.
        """
        self.publish_comment(body)

    def get_review_thread_comments(self, comment_id: int) -> List[Dict[str, Any]]:
        """Return the comments in the thread of the given comment.

        Gitea does not expose a thread-by-comment lookup, so this returns the
        single target comment (best-effort) so inline ``/ask`` still has the
        original comment as context.
        """
        try:
            comment = self.repo_api.get_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment_id
            )
            return [comment] if comment else []
        except Exception as e:
            self.logger.error(f"Error getting review thread comments for {comment_id}: {e}")
            return []


    def publish_inline_comment(self,body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        """Publish an inline comment on a specific line"""
        body = self.limit_output_characters(body, self.max_comment_chars)
        position, absolute_position = find_line_number_of_relevant_line_in_file(self.diff_files,
                                                                                relevant_file.strip('`'),
                                                                                relevant_line_in_file,
                                                                                )
        if position == -1:
            get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"

        path = relevant_file.strip()
        payload = dict(body=body, path=path, old_position=position,new_position = absolute_position) if subject_type == "LINE" else {}
        self.publish_inline_comments([payload])


    def publish_inline_comments(self, comments: List[Dict[str, Any]], body: str = "Inline comment") -> None:
        """Publish inline comments as a *submitted* COMMENT review.

        Previously this called ``create_inline_comment`` with no ``event``,
        which created an unsubmitted PENDING draft review — the inline
        ``/review``/``/improve`` suggestions never actually posted. We submit
        with ``event="COMMENT"`` so the comments post immediately, mirroring
        GithubProvider (``pr.create_review(comments=...)`` submits a review).

        This is deliberately kept separate from ``submit_review``: the formal
        verdict (APPROVED / REQUEST_CHANGES via ``submit_review``/``auto_approve``)
        is its own submitted review with an empty ``comments`` list, so inline
        suggestions can never accidentally carry an APPROVED event, and an
        approval can never smuggle in inline comments.

        Maps to ``POST /repos/{owner}/{repo}/pulls/{index}/reviews`` with body
        ``{body, event: "COMMENT", comments[]}`` where each comment is
        ``{body, path, new_position, old_position}`` (Gitea CreatePullReviewOptions).

        ``commit_id`` is deliberately omitted (same reasoning as
        ``submit_review``): ``self.last_commit`` comes from
        ``repo_get_all_commits`` — the repository/default-branch commit list, not
        the PR's commits — so its sha is unrelated to the PR head. Pinning it
        would anchor the inline positions to the wrong commit; omitting it lets
        Gitea default to the PR head, which is where the positions were computed.
        """
        response = self.repo_api.create_review(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number if self.enabled_pr else self.issue_number,
            event="COMMENT",
            body=body,
            comments=comments,
        )

        # ``create_review`` runs with ``_preload_content=False``, so a successful
        # call returns the ``(data, status, headers)`` tuple rather than a truthy
        # body; a non-2xx status raises ``ApiException`` before we get here.
        # Inspect the status code instead of the (always-falsy) body.
        status = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        if status is not None and not (200 <= status < 300):
            self.logger.error(f"Failed to publish inline comment (status {status})")
            return

        self.logger.info("Inline comment published")

    def submit_review(self, event: str, body: str = "") -> bool:
        """Submit a formal Gitea review on the PR.

        Unlike ``publish_comment`` (an issue comment) this occupies a reviewer
        slot and can gate merges via branch protection. ``event`` is one of
        APPROVED / REQUEST_CHANGES / COMMENT / PENDING. Gitea (like GitHub)
        rejects a review whose reviewer is the PR author, so the configured
        token must belong to a dedicated bot account distinct from PR authors.

        ``commit_id`` is deliberately omitted so Gitea anchors the review to the
        PR head: ``self.last_commit`` comes from ``repo_get_all_commits`` (the
        repository/default-branch commit list, not the PR's commits), so its sha
        is unrelated to the PR head and could mis-attach or be rejected under
        stricter branch protection.

        Returns True on success; failures are logged and swallowed so a failed
        formal review never breaks the underlying ``/review`` comment.
        """
        if not self.enabled_pr:
            self.logger.error("Cannot submit a review: not a pull request")
            return False
        try:
            self.repo_api.create_review(
                owner=self.owner,
                repo=self.repo,
                pr_number=self.pr_number,
                event=event,
                body=body,
            )
            self.logger.info(
                f"Submitted Gitea review event={event} on {self.owner}/{self.repo}#{self.pr_number}"
            )
            return True
        except Exception as e:
            self.logger.error(
                f"Failed to submit Gitea review event={event} on "
                f"{self.owner}/{self.repo}#{self.pr_number}: {e}"
            )
            return False

    def auto_approve(self) -> bool:
        """Approve the PR by submitting a formal APPROVED review.

        Mirrors GithubProvider.auto_approve so that ``/review auto_approve``
        works on Gitea (the base class is a no-op that returns False).
        """
        return self.submit_review("APPROVED")

    def publish_code_suggestions(self, suggestions: List[Dict[str, Any]]):
        """Publish code suggestions"""
        for suggestion in suggestions:
            body = suggestion.get("body","")
            if not body:
                self.logger.error("No body provided for the suggestion")
                continue

            path = suggestion.get("relevant_file","")
            new_position = suggestion.get("relevant_lines_start",0)
            old_position = suggestion.get("relevant_lines_start",0) if "original_suggestion" not in suggestion else suggestion["original_suggestion"].get("relevant_lines_start",0)
            title_body = suggestion["original_suggestion"].get("suggestion_content","") if "original_suggestion" in suggestion else ""
            payload = dict(body=body, path=path, old_position=old_position,new_position = new_position)
            if title_body:
                title_body = f"**Suggestion:** {title_body}"
                self.publish_inline_comments([payload],title_body)
            else:
                self.publish_inline_comments([payload])

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        """Add eyes reaction to a comment"""
        try:
            if disable_eyes:
                return None

            comments = self.repo_api.list_all_comments(
                owner=self.owner,
                repo=self.repo,
                index=self.pr_number if self.enabled_pr else self.issue_number
            )

            comment_ids = [comment.id for comment in comments]
            if issue_comment_id not in comment_ids:
                self.logger.error(f"Comment ID {issue_comment_id} not found. Available IDs: {comment_ids}")
                return None

            response = self.repo_api.add_reaction_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=issue_comment_id,
                reaction="eyes"
            )

            if not response:
                self.logger.error("Failed to add eyes reaction")
                return None

            return response[0].id if isinstance(response, tuple) else response.id

        except ApiException as e:
            self.logger.error(f"Error adding eyes reaction: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        """Remove the eyes reaction from a comment.

        Signature mirrors the base/GithubProvider contract
        ``remove_reaction(issue_comment_id, reaction_id)`` — the base class and
        every caller pass two arguments, so the previous single-argument
        Gitea signature raised a ``TypeError`` at runtime. Gitea deletes a
        reaction by its content (not by a reaction id), so ``reaction_id`` is
        accepted for contract compatibility but the "eyes" content is what is
        removed. Returns True on success.
        """
        try:
            self.repo_api.remove_reaction_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=issue_comment_id,
                reaction="eyes"
            )
            return True
        except ApiException as e:
            self.logger.error(f"Error removing reaction: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return False

    def get_commit_messages(self)-> str:
        """Get commit messages for the PR"""
        max_tokens = get_settings().get("CONFIG.MAX_COMMITS_TOKENS", None)
        pr_commits = self.repo_api.get_pr_commits(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number
        )

        if not pr_commits:
            self.logger.error("Failed to get commit messages")
            return ""

        try:
            commit_messages = [commit["commit"]["message"] for commit in pr_commits if commit]

            if not commit_messages:
                self.logger.error("No commit messages found")
                return ""

            commit_message = "".join(commit_messages)
            if max_tokens:
                commit_message = clip_tokens(commit_message, max_tokens)

            return commit_message
        except Exception as e:
            self.logger.error(f"Error processing commit messages: {str(e)}")
            return ""

    def _get_merge_base_ref(self) -> str:
        """Ref to read *base* file content from.

        Uses the PR's merge base (``self.merge_base_sha``, from Gitea's
        ``PullRequest.merge_base``) rather than the raw ``base.sha``: the base
        branch may have advanced via parallel merges, so ``base.sha`` no longer
        points at where the PR diverged. This mirrors github_provider reading
        original content from ``compare.merge_base_commit.sha``. Falls back to
        ``base_sha`` when the merge base is unknown (older Gitea), so it never
        regresses.
        """
        return getattr(self, "merge_base_sha", "") or self.base_sha

    # ------------------------------------------------------------------
    # Incremental review (re-review only the commits pushed since the last
    # review). Ports github_provider's get_incremental_commits / get_commit_range
    # / get_previous_review to Gitea's REST API. Enabled via `/review -i`.
    # ------------------------------------------------------------------
    def get_incremental_commits(self, incremental=None):
        """Prepare state for an incremental review.

        Mirrors GithubProvider.get_incremental_commits: records the
        ``IncrementalPR`` and, when incremental, resets the unreviewed-files set
        and computes the new-commit range. A no-op for a full review.
        """
        if incremental is None:
            incremental = IncrementalPR(False)
        self.incremental = incremental
        if self.incremental.is_incremental:
            self.unreviewed_files_set = dict()
            self._get_incremental_commits()

    def _get_incremental_commits(self):
        """Compute which commits (and files) are new since the last review.

        Uses the PR's commit list (``GET .../pulls/{index}/commits``) and the
        most recent prior review comment (``get_previous_review``). Files changed
        across the new-commit range are collected via the compare API
        (``GET .../compare/{last_seen}...{head}``), whose per-commit ``files``
        arrays list every affected path. If no previous review exists, we fall
        back to a full review (``is_incremental = False``), matching
        github_provider.
        """
        if not getattr(self, "pr_commits", None):
            self.pr_commits = self.repo_api.get_pr_commits(
                owner=self.owner, repo=self.repo, pr_number=self.pr_number
            )
        # Normalize to adapters exposing .sha and .commit.author.date, so the
        # IncrementalPR accessors used by pr_reviewer work unchanged.
        self.pr_commits = [self._as_commit(c) for c in (self.pr_commits or [])]

        self.previous_review = self.get_previous_review(full=True, incremental=True)
        push_before = get_settings().get("gitea.incremental_push_before_sha", None)
        if self.previous_review:
            self.incremental.commits_range = self.get_commit_range()
            head = self.sha
            base = self.incremental.last_seen_commit_sha or self.base_sha
            changed = self._get_changed_files_in_range(base, head)
            self.unreviewed_files_set.update(changed)
        elif push_before:
            # No prior review comment to diff against, but a push webhook gave us
            # the pre-push head SHA. Anchor the incremental review to that range
            # (before...head) so a push re-review still only looks at the new
            # commits. Set last_seen_commit so pr_reviewer's threshold checks and
            # the diff base both use it.
            self.logger.info(f"No previous review found; using push before-SHA {push_before} as incremental base")
            self.incremental.last_seen_commit = self._as_commit({"sha": push_before})
            self.incremental.commits_range = self._commits_after_sha(push_before)
            if self.incremental.commits_range:
                self.incremental.first_new_commit = self.incremental.commits_range[0]
            changed = self._get_changed_files_in_range(push_before, self.sha)
            self.unreviewed_files_set.update(changed)
            if not self.unreviewed_files_set:
                self.logger.info("Push range produced no changed files, will review the entire PR")
                self.incremental.is_incremental = False
        else:
            self.logger.info("No previous review found, will review the entire PR")
            self.incremental.is_incremental = False

    def _commits_after_sha(self, sha: str) -> List[Any]:
        """Return the PR commits that come strictly after ``sha`` (exclusive).

        Used to size the new-commit range for a push-anchored incremental review
        when there is no prior review comment. If ``sha`` is not found in the PR
        commit list, returns [] (the caller then falls back to a full review).
        """
        shas = [c.sha for c in self.pr_commits]
        if sha in shas:
            idx = shas.index(sha)
            return self.pr_commits[idx + 1:]
        return []

    def get_commit_range(self):
        """Return the slice of ``pr_commits`` authored after the last review.

        Walks the commits newest-first; commits authored after the previous
        review's timestamp are "new", and the first commit at/older than that
        timestamp is the ``last_seen_commit`` (the incremental diff base). Mirrors
        github_provider.get_commit_range.
        """
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
        """Return the most recent prior review comment, or None.

        Matches on the review header prefixes (regular / incremental) exactly
        like github_provider, scanning the PR's issue comments newest-first.
        """
        if not (full or incremental):
            raise ValueError("At least one of full or incremental must be True")
        if not getattr(self, "comments", None):
            self.comments = self.repo_api.list_all_comments(
                owner=self.owner, repo=self.repo, index=self.pr_number
            ) or []
        prefixes = []
        if full:
            prefixes.append(PRReviewHeader.REGULAR.value)
        if incremental:
            prefixes.append(PRReviewHeader.INCREMENTAL.value)
        for index in range(len(self.comments) - 1, -1, -1):
            body = self._comment_body(self.comments[index])
            if any(body.startswith(prefix) for prefix in prefixes):
                return self.comments[index]
        return None

    def _get_changed_files_in_range(self, base: str, head: str) -> Dict[str, Any]:
        """Map ``{filename: filename}`` for every file touched between two refs.

        Uses the compare API's per-commit ``files`` arrays. Returns an empty
        mapping (triggering the "no new files" path in pr_reviewer) if the
        compare is unavailable, rather than falsely reporting the whole PR.
        """
        changed: Dict[str, Any] = {}
        if not base or not head:
            return changed
        compare = self.repo_api.get_compare(
            owner=self.owner, repo=self.repo, base=base, head=head
        )
        for commit in (compare or {}).get("commits", []) or []:
            for f in (commit.get("files") or []):
                name = f.get("filename")
                if name:
                    changed[name] = name
        return changed

    @staticmethod
    def _comment_body(comment) -> str:
        if isinstance(comment, dict):
            return comment.get("body", "") or ""
        return getattr(comment, "body", "") or ""

    @staticmethod
    def _as_commit(commit):
        """Wrap a Gitea commit (dict or object) so ``.sha`` and
        ``.commit.author.date`` (a ``datetime``) are always available, matching
        what the ``IncrementalPR`` accessors and pr_reviewer expect from a
        PyGithub commit."""
        if not isinstance(commit, dict):
            return commit  # already an object with the needed attributes

        from datetime import datetime, timezone

        def _parse_date(value):
            if not value:
                return datetime.min.replace(tzinfo=timezone.utc)
            try:
                # Gitea emits RFC 3339 (e.g. 2024-01-02T03:04:05Z).
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        commit_obj = commit.get("commit") or {}
        author = commit_obj.get("author") or {}
        date = _parse_date(author.get("date"))
        author_ns = SimpleNamespace(date=date, name=author.get("name", ""))
        commit_ns = SimpleNamespace(author=author_ns, message=commit_obj.get("message", ""))
        return SimpleNamespace(sha=commit.get("sha", ""), commit=commit_ns)

    def _get_file_content_from_base(self, filename: str) -> str:
        return self.repo_api.get_file_content(
            owner=self.owner,
            repo=self.repo,
            commit_sha=self._get_merge_base_ref(),
            filepath=filename
        )

    def _build_compare_patches(self, base: str, head: str) -> Dict[str, str]:
        """Return ``{filename: patch}`` from the merge-base-relative compare diff.

        Uses ``GET .../compare/{base}...{head}?output=diff`` (three-dot, so it is
        relative to the merge base, exactly like github_provider's compare).
        Parsed with the same hunk-extraction logic as the PR ``.diff`` so the
        patch shape is identical. Returns ``{}`` when the compare API is
        unavailable, so the caller falls back to the PR ``.diff`` and never
        regresses.
        """
        try:
            diff_text = self.repo_api.get_compare_diff(
                owner=self.owner, repo=self.repo, base=base, head=head
            )
        except Exception as e:
            self.logger.warning(f"Compare diff unavailable ({base}...{head}): {e}; falling back to PR diff")
            return {}
        if not diff_text:
            return {}
        return self._parse_unified_diff(diff_text)

    @staticmethod
    def _parse_unified_diff(diff_text: str) -> Dict[str, str]:
        """Parse a unified diff into ``{filename: patch}`` (hunks only).

        Shared by the PR ``.diff`` path and the compare-diff path. The patch for
        each file starts at its first ``@@`` hunk header (headers like ``index``/
        ``---``/``+++`` are dropped), matching the shape the rest of pr-agent
        expects. Files with no ``@@`` hunk (pure renames, mode changes, binaries)
        produce no entry here; ``get_diff_files`` synthesizes a patch for those
        via ``load_large_diff`` so they are never silently dropped.
        """
        lines = diff_text.splitlines()
        current_file = None
        current_patch = []
        file_patches: Dict[str, str] = {}
        for line in lines:
            if line.startswith('diff --git'):
                if current_file and current_patch:
                    file_patches[current_file] = '\n'.join(current_patch)
                current_patch = []
                current_file = line.split(' b/')[-1]
            elif line.startswith('@@') and not current_patch:
                current_patch = [line]
            elif current_patch:
                current_patch.append(line)
        if current_file and current_patch:
            file_patches[current_file] = '\n'.join(current_patch)
        return file_patches

    def get_diff_files(self) -> List[FilePatchInfo]:
        """Get files that were modified in the PR.

        The per-file *list* (with status/additions/deletions and, for renames,
        ``previous_filename``) comes from ``GET .../pulls/{index}/files`` — it
        already includes renames, mode changes and binary files. Patches come
        from the merge-base-relative compare diff when available (falling back to
        the PR ``.diff``); any changed file still missing a patch (pure rename,
        mode change, binary, or a diff the parser could not attribute) gets a
        synthesized patch via ``load_large_diff`` so it is never dropped from the
        review input.
        """
        if self.diff_files:
            return self.diff_files

        incremental = bool(self.incremental.is_incremental and self.unreviewed_files_set)

        # Patch source. Full review: merge-base-relative compare diff of
        # base...head (falling back to the PR .diff already parsed into
        # self.file_diffs). Incremental review: only the diff introduced by the
        # new commits, i.e. last_seen_commit...head, so re-reviews on push don't
        # re-diff the whole PR. Both fall back conservatively.
        file_diffs = self.file_diffs
        if incremental:
            base_ref = self.incremental.last_seen_commit_sha or self.base_sha
            if base_ref and self.sha:
                compare_patches = self._build_compare_patches(base_ref, self.sha)
                if compare_patches:
                    file_diffs = compare_patches
        elif self.base_sha and self.sha:
            compare_patches = self._build_compare_patches(self.base_sha, self.sha)
            if compare_patches:
                file_diffs = compare_patches

        # In incremental mode review only the files touched by the new commits.
        files_iter = self.git_files
        if incremental:
            files_iter = [f for f in self.git_files
                          if f.get("filename") in self.unreviewed_files_set]

        invalid_files_names = []
        counter_valid = 0
        diff_files = []
        for file in files_iter:
            filename = file.get("filename")
            if not filename:
                continue

            if not is_valid_file(filename):
                invalid_files_names.append(filename)
                continue

            counter_valid += 1
            avoid_load = False
            patch = file_diffs.get(filename, "")
            # For renames, the base content lives under the OLD path; ChangedFile
            # exposes it as previous_filename. Fall back to the new filename.
            previous_filename = file.get("previous_filename") or filename
            head_file = ""
            base_file = ""

            if counter_valid >= MAX_FILES_ALLOWED_FULL and patch and not self.incremental.is_incremental:
                avoid_load = True
                if counter_valid == MAX_FILES_ALLOWED_FULL:
                    self.logger.info("Too many files in PR, will avoid loading full content for rest of files")

            if avoid_load:
                head_file = ""
            else:
                # Get file content from this pr
                head_file = self.file_contents.get(filename, "")

            if incremental:
                # Incremental base = the last already-reviewed commit, so the
                # diff reflects only what the new commits changed.
                base_ref = self.incremental.last_seen_commit_sha or self.base_sha
                base_file = self.repo_api.get_file_content(
                    owner=self.owner, repo=self.repo,
                    commit_sha=base_ref, filepath=previous_filename,
                )
            else:
                if avoid_load:
                    base_file = ""
                else:
                    base_file = self._get_file_content_from_base(previous_filename)

            # Synthesize a patch for any changed file the diff parser could not
            # attribute (pure rename, mode change, binary, multi-hunk gap). This
            # mirrors github_provider, which calls load_large_diff whenever
            # file.patch is empty. Skipped when content was intentionally not
            # loaded (avoid_load) — there is nothing to diff against.
            if not patch and not avoid_load and (head_file or base_file):
                patch = load_large_diff(filename, head_file, base_file, show_warning=False)

            if self.incremental.is_incremental and self.unreviewed_files_set:
                self.unreviewed_files_set[filename] = patch

            num_plus_lines = file.get("additions", 0)
            num_minus_lines = file.get("deletions", 0)
            status = file.get("status", "")

            if status == 'added':
                edit_type = EDIT_TYPE.ADDED
            elif status == 'removed' or status == 'deleted':
                edit_type = EDIT_TYPE.DELETED
            elif status == 'renamed':
                edit_type = EDIT_TYPE.RENAMED
            elif status == 'copied':
                edit_type = EDIT_TYPE.ADDED
            elif status == 'modified' or status == 'changed':
                edit_type = EDIT_TYPE.MODIFIED
            else:
                self.logger.error(f"Unknown edit type: {status}")
                edit_type = EDIT_TYPE.UNKNOWN

            file_patch_info = FilePatchInfo(
                base_file=base_file,
                head_file=head_file,
                patch=patch,
                filename=filename,
                num_minus_lines=num_minus_lines,
                num_plus_lines=num_plus_lines,
                edit_type=edit_type,
                old_filename=None if previous_filename == filename else previous_filename,
            )
            diff_files.append(file_patch_info)

        if invalid_files_names:
            self.logger.info(f"Filtered out files with invalid extensions: {invalid_files_names}")

        self.diff_files = diff_files
        return diff_files

    def get_line_link(self, relevant_file, relevant_line_start, relevant_line_end = None) -> str:
        if relevant_line_start == -1:
            link = f"{self.base_url}/{self.owner}/{self.repo}/src/branch/{self.get_pr_branch()}/{relevant_file}"
        elif relevant_line_end:
            link = f"{self.base_url}/{self.owner}/{self.repo}/src/branch/{self.get_pr_branch()}/{relevant_file}#L{relevant_line_start}-L{relevant_line_end}"
        else:
            link = f"{self.base_url}/{self.owner}/{self.repo}/src/branch/{self.get_pr_branch()}/{relevant_file}#L{relevant_line_start}"

        self.logger.info(f"Generated link: {link}")
        return link

    def get_pr_id(self):
        try:
            pr_id = f"{self.repo}/{self.pr_number}"
            return pr_id
        except:
            return ""

    def get_files(self) -> List[Dict[str, Any]]:
        """Get all files in the PR"""
        return [file.get("filename","") for file in self.git_files]

    def get_num_of_files(self) -> int:
        """Get number of files changed in the PR"""
        return len(self.git_files)

    def get_issue_comments(self) -> List[Dict[str, Any]]:
        """Get all comments in the PR"""
        index = self.issue_number if self.enabled_issue else self.pr_number
        comments = self.repo_api.list_all_comments(
            owner=self.owner,
            repo=self.repo,
            index=index
        )
        if not comments:
            self.logger.error("Failed to get comments")
            return []

        return comments

    def get_languages(self) -> Set[str]:
        """Get programming languages used in the repository"""
        languages = self.repo_api.get_languages(
            owner=self.owner,
            repo=self.repo
        )

        return languages

    def get_pr_branch(self) -> str:
        """Get the branch name of the PR"""
        if not self.pr:
            self.logger.error("Failed to get PR branch")
            return ""

        if not self.pr.head:
            self.logger.error("PR head not found")
            return ""

        return self.pr.head.ref if self.pr.head.ref else ""

    def get_pr_description_full(self) -> str:
        """Get full PR description with metadata"""
        if not self.pr:
            self.logger.error("Failed to get PR description")
            return ""

        return self.pr.body if self.pr.body else ""

    def get_pr_labels(self,update=False) -> List[str]:
        """Get labels assigned to the PR"""
        if not update:
            if not self.pr.labels:
                self.logger.error("Failed to get PR labels")
                return []
            return [label.name for label in self.pr.labels]

        labels = self.repo_api.get_issue_labels(
            owner=self.owner,
            repo=self.repo,
            issue_number=self.pr_number
        )
        if not labels:
            self.logger.error("Failed to get PR labels")
            return []

        return [label.name for label in labels]

    def get_repo_settings(self) -> bytes:
        """Get repository settings.

        When a config branch is set (via ``config.config_branch`` or the
        ``PR_AGENT_CONFIG_BRANCH`` env var, mirroring GithubProvider), the
        settings file is read from that branch first; a missing branch/file
        falls back to the PR head commit. ``get_file_content`` passes the ref
        through Gitea's ``?ref=`` query param, which accepts a branch name as
        well as a commit sha.
        """
        if not self.repo_settings:
            self.logger.error("Repository settings not found")
            return b""

        settings_branch = get_settings().get("CONFIG.CONFIG_BRANCH", None)
        settings_branch = settings_branch.strip() if isinstance(settings_branch, str) else ""
        env_branch = (os.environ.get("PR_AGENT_CONFIG_BRANCH") or "").strip()
        config_branch = settings_branch or env_branch

        response = ""
        if config_branch:
            response = self.repo_api.get_file_content(
                owner=self.owner,
                repo=self.repo,
                commit_sha=config_branch,
                filepath=self.repo_settings
            )
            if not response:
                self.logger.warning(
                    f"Failed to load {self.repo_settings} from branch '{config_branch}', "
                    "falling back to the PR head commit"
                )

        if not response:
            response = self.repo_api.get_file_content(
                owner=self.owner,
                repo=self.repo,
                commit_sha=self.sha,
                filepath=self.repo_settings
            )
        if not response:
            self.logger.error("Failed to get repository settings")
            return b""

        # utils.apply_repo_settings() writes this via os.write() and later
        # calls .decode() on it, so it must be bytes to match the GitHub/
        # GitLab/Bitbucket contract. get_file_content() decodes the raw bytes
        # to str, so re-encode here (see issue #2347).
        return response.encode('utf-8')

    def get_user_id(self) -> str:
        """Get the ID of the authenticated user"""
        return f"{self.pr.user.id}" if self.pr else ""

    def is_supported(self, capability) -> bool:
        """Report whether a capability is available.

        Mirrors GithubProvider: in restricted mode the provider must not claim
        it can push code, so callers skip operations that need elevated
        permissions instead of failing at the API.
        """
        if capability == "push_code" and get_settings().config.restricted_mode:
            return False
        return True

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        return f"{self.base_url}/{self.owner}/{self.repo}.git" #base_url / <OWNER>/<REPO>.git

    def publish_description(self, pr_title: str, pr_body: str) -> None:
        """Publish PR description"""
        edit_kwargs = dict(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number if self.enabled_pr else self.issue_number,
            body=pr_body,
        )
        if pr_title is not None:
            edit_kwargs["title"] = pr_title
        response = self.repo_api.edit_pull_request(**edit_kwargs)

        if not response:
            self.logger.error("Failed to publish PR description")
            return None

        self.logger.info("PR description published successfully")
        if self.enabled_pr:
            self.pr = self.repo_api.get_pull_request(
                owner=self.owner,
                repo=self.repo,
                pr_number=self.pr_number
            )

    def get_repo_labels(self) -> List[Dict[str, Any]]:
        """Get all labels defined in the repository.

        Returns the raw label objects (each ``{id, name, color, ...}``) so
        callers can resolve a label name to the numeric id Gitea's
        issue-label endpoint requires. Mirrors GithubProvider.get_repo_labels.
        """
        labels = self.repo_api.get_repo_labels(
            owner=self.owner,
            repo=self.repo
        )
        if not labels:
            self.logger.error("Failed to get repository labels")
            return []

        return labels

    def publish_labels(self, labels: List[str]) -> None:
        """Publish labels to the PR.

        pr-agent's tools hand this method label *names* (e.g. "Bug fix"), but
        Gitea's issue-label endpoint takes numeric label *ids*. Resolve the
        names against the repository's labels before posting; names with no
        matching repository label are skipped (Gitea can only attach labels
        that already exist in the repo).
        """
        if not labels:
            self.logger.error("No labels provided to publish")
            return None

        repo_labels = self.get_repo_labels()
        name_to_id = {
            (label.get("name") if isinstance(label, dict) else getattr(label, "name", None)):
            (label.get("id") if isinstance(label, dict) else getattr(label, "id", None))
            for label in repo_labels
        }
        label_ids = [name_to_id[name] for name in labels if name in name_to_id]
        missing = [name for name in labels if name not in name_to_id]
        if missing:
            self.logger.warning(f"Skipping labels not defined in the repository: {missing}")
        if not label_ids:
            self.logger.error("None of the provided labels exist in the repository")
            return None

        response = self.repo_api.add_labels(
            owner=self.owner,
            repo=self.repo,
            issue_number=self.pr_number if self.enabled_pr else self.issue_number,
            labels=label_ids
        )

        if response:
            self.logger.info("Labels added successfully")

    def remove_comment(self, comment) -> None:
        """Remove a specific comment"""
        if not comment:
            return

        try:
            comment_id = comment.get("comment_id") if isinstance(comment, dict) else comment.id
            if not comment_id:
                self.logger.error("Comment ID not found")
                return None
            self.repo_api.remove_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment_id
            )

            if self.comments_list and comment in self.comments_list:
                self.comments_list.remove(comment)

            self.logger.info(f"Comment removed successfully: {comment}")
        except ApiException as e:
            self.logger.error(f"Error removing comment: {e}")
            raise e

    def remove_initial_comment(self) -> None:
        """Remove the initial comment"""
        for comment in self.comments_list:
            try:
                if not comment.get("is_temporary"):
                    continue
                self.remove_comment(comment)
            except Exception as e:
                self.logger.error(f"Error removing comment: {e}")
                continue
            self.logger.info(f"Removed initial comment: {comment.get('comment_id')}")

    #Clone related
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        #For example, to clone:
        #https://github.com/Codium-ai/pr-agent-pro.git
        #Need to embed inside the github token:
        #https://<token>@github.com/Codium-ai/pr-agent-pro.git

        gitea_token = self.gitea_access_token
        gitea_base_url = self.base_url
        scheme = gitea_base_url.split("://")[0]
        scheme += "://"
        if not all([gitea_token, gitea_base_url]):
            get_logger().error("Either missing auth token or missing base url")
            return None
        base_url = gitea_base_url.split(scheme)[1]
        if not base_url:
            get_logger().error(f"Base url: {gitea_base_url} has an empty base url")
            return None
        if base_url not in repo_url_to_clone:
            get_logger().error(f"url to clone: {repo_url_to_clone} does not contain {base_url}")
            return None
        repo_full_name = repo_url_to_clone.split(base_url)[-1]
        if not repo_full_name:
            get_logger().error(f"url to clone: {repo_url_to_clone} is malformed")
            return None

        clone_url = scheme
        clone_url += f"{gitea_token}@{base_url}{repo_full_name}"
        return clone_url

class RepoApi(giteapy.RepositoryApi):
    def __init__(self, client: giteapy.ApiClient):
        self.repository = giteapy.RepositoryApi(client)
        self.issue = giteapy.IssueApi(client)
        self.logger = get_logger()
        super().__init__(client)

    def create_inline_comment(self, owner: str, repo: str, pr_number: int, body : str ,commit_id : str, comments: List[Dict[str, Any]]):
        body = {
            "body": body,
            "comments": comments,
            "commit_id": commit_id,
        }
        # No ``event`` is sent, so the review is created as an unsubmitted
        # PENDING draft (the pre-existing ``/review`` inline-comment behavior).
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/pulls/{pr_number}/reviews',
            'POST',
            path_params={'owner': owner, 'repo': repo, 'pr_number': pr_number},
            body=body,
            response_type=None,
            _return_http_data_only=False,
            _preload_content=False,
            auth_settings=['AuthorizationHeaderToken']
        )

    def create_review(self, owner: str, repo: str, pr_number: int, event: Optional[str] = None,
                      body: str = "", commit_id: Optional[str] = None,
                      comments: Optional[List[Dict[str, Any]]] = None):
        """Create (and optionally submit) a formal pull request review.

        Maps to ``POST /repos/{owner}/{repo}/pulls/{index}/reviews`` with a
        ``CreatePullReviewOptions`` payload. ``event`` is one of APPROVED /
        REQUEST_CHANGES / COMMENT / PENDING; omitting it leaves the review as an
        unsubmitted PENDING draft. Each entry in ``comments`` is an inline
        comment ``{body, path, new_position, old_position}``.
        """
        review_body = {
            "body": body,
            "comments": comments if comments is not None else [],
        }
        if event:
            review_body["event"] = event
        if commit_id:
            review_body["commit_id"] = commit_id
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/pulls/{pr_number}/reviews',
            'POST',
            path_params={'owner': owner, 'repo': repo, 'pr_number': pr_number},
            body=review_body,
            response_type=None,
            _return_http_data_only=False,
            _preload_content=False,
            auth_settings=['AuthorizationHeaderToken']
        )

    def create_comment(self, owner: str, repo: str, index: int, comment: str):
        body = {
            "body": comment
        }
        return self.issue.issue_create_comment(
            owner=owner,
            repo=repo,
            index=index,
            body=body
        )

    def edit_comment(self, owner: str, repo: str, comment_id: int, comment: str):
        body = {
            "body": comment
        }
        return self.issue.issue_edit_comment(
            owner=owner,
            repo=repo,
            id=comment_id,
            body=body
        )

    def remove_comment(self, owner: str, repo: str, comment_id: int):
        return self.issue.issue_delete_comment(
            owner=owner,
            repo=repo,
            id=comment_id
        )

    def list_all_comments(self, owner: str, repo: str, index: int):
        return self.issue.issue_get_comments(
            owner=owner,
            repo=repo,
            index=index
        )

    def get_comment(self, owner: str, repo: str, comment_id: int):
        """Fetch a single issue/PR comment by its id.

        Maps to ``GET /repos/{owner}/{repo}/issues/comments/{id}`` and returns
        the raw comment dict (``{id, body, user, ...}``).
        """
        try:
            url = f'/repos/{owner}/{repo}/issues/comments/{comment_id}'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return json.loads(raw_data.decode('utf-8'))
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return json.loads(raw_data.decode('utf-8'))

            return {}

        except ApiException as e:
            self.logger.error(f"Error getting comment {comment_id}: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return {}

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Get the diff content of a pull request using direct API call"""
        try:
            url = f'/repos/{owner}/{repo}/pulls/{pr_number}.diff'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return raw_data.decode('utf-8', errors='replace')
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return raw_data.decode('utf-8', errors='replace')
            else:
                error_msg = f"Unexpected response format received from API: {type(response)}"
                self.logger.error(error_msg)
                raise RuntimeError(error_msg)

        except ApiException as e:
            self.logger.error(f"Error getting diff: {str(e)}")
            raise e
        except Exception as e:
            self.logger.error(f"Unexpected error: {str(e)}")
            raise e

    def get_compare(self, owner: str, repo: str, base: str, head: str) -> Dict[str, Any]:
        """Compare two refs via Gitea's compare API.

        Maps to ``GET /repos/{owner}/{repo}/compare/{base}...{head}`` (three-dot,
        merge-base relative — see docs.gitea.com repoCompareDiff). The endpoint
        is a wildcard route (``/compare/*``), so ``base...head`` is embedded in
        the path rather than passed as a path param.

        Returns Gitea's ``Compare`` object: ``{"total_commits": int, "commits":
        [Commit, ...]}`` where each commit carries a ``files`` array of
        ``{"filename", "status"}`` (CommitAffectedFiles). Note that Gitea's
        compare JSON does NOT return a merge-base sha and does NOT return a
        merged per-file patch (only per-commit affected files); the merge-base
        relative *diff text* is fetched separately via ``get_compare_diff``.

        Returns ``{}`` on failure so callers can fall back gracefully.
        """
        try:
            # base...head is merge-base relative (three-dot), matching how
            # GitHub's compare merge_base_commit is used in github_provider.
            url = f'/repos/{owner}/{repo}/compare/{base}...{head}'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return json.loads(raw_data.decode('utf-8'))
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return json.loads(raw_data.decode('utf-8'))

            return {}

        except ApiException as e:
            self.logger.error(f"Error comparing {base}...{head}: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return {}

    def get_compare_diff(self, owner: str, repo: str, base: str, head: str) -> str:
        """Get the merge-base-relative unified diff for a ref range.

        Maps to ``GET /repos/{owner}/{repo}/compare/{base}...{head}?output=diff``.
        Gitea's compare route serves the raw diff/patch when ``output=diff`` is
        requested (``downloadCompareDiffOrPatch``), computed over ``base...head``
        (three-dot), i.e. relative to the merge base — the same semantics as the
        GitHub compare used by github_provider.

        Returns "" on failure so callers can fall back to the PR ``.diff``.
        """
        try:
            url = f'/repos/{owner}/{repo}/compare/{base}...{head}'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                query_params=[('output', 'diff')],
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return raw_data.decode('utf-8', errors='replace')
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return raw_data.decode('utf-8', errors='replace')

            return ""

        except ApiException as e:
            self.logger.error(f"Error getting compare diff {base}...{head}: {e}")
            return ""
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return ""

    def get_pull_request(self, owner: str, repo: str, pr_number: int):
        """Get pull request details including description"""
        return self.repository.repo_get_pull_request(
            owner=owner,
            repo=repo,
            index=pr_number
        )

    def edit_pull_request(self, owner: str, repo: str, pr_number: int,title : str, body: str):
        """Edit pull request description"""
        body = {
            "body": body,
            "title" : title
        }
        return self.repository.repo_edit_pull_request(
            owner=owner,
            repo=repo,
            index=pr_number,
            body=body
        )

    def get_change_file_pull_request(self, owner: str, repo: str, pr_number: int):
        """Get changed files in the pull request"""
        try:
            url = f'/repos/{owner}/{repo}/pulls/{pr_number}/files'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                diff_content = raw_data.decode('utf-8')
                return json.loads(diff_content) if isinstance(diff_content, str) else diff_content
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                diff_content = raw_data.decode('utf-8')
                return json.loads(diff_content) if isinstance(diff_content, str) else diff_content

            return []

        except ApiException as e:
            self.logger.error(f"Error getting changed files: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return []

    def get_languages(self, owner: str, repo: str):
        """Get programming languages used in the repository"""
        try:
            url = f'/repos/{owner}/{repo}/languages'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return json.loads(raw_data.decode('utf-8'))
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return json.loads(raw_data.decode('utf-8'))

            return {}

        except ApiException as e:
            self.logger.error(f"Error getting languages: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return {}

    def get_file_content(self, owner: str, repo: str, commit_sha: str, filepath: str) -> str:
        """Get raw file content from a specific commit"""

        try:
            url = f'/repos/{owner}/{repo}/raw/{filepath}'
            query_params = []
            if commit_sha:
                query_params.append(('ref', commit_sha))

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                query_params=query_params,
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            # Decode via the shared fallback chain (utf-8, then iso-8859-1/latin-1/
            # ascii/utf-16) so legitimate non-UTF-8 *text* (e.g. UTF-16) is preserved
            # rather than dropped, while binary payloads no longer crash the provider.
            # decode_if_bytes returns "" only if every encoding fails; binary files are
            # filtered downstream by extension (should_skip_patch).
            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return decode_if_bytes(raw_data)
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return decode_if_bytes(raw_data)

            return ""

        except ApiException as e:
            self.logger.error(f"Error getting file: {filepath}, content: {e}")
            return ""
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return ""

    def get_issue_labels(self, owner: str, repo: str, issue_number: int):
        """Get labels assigned to the issue"""
        return self.issue.issue_get_labels(
            owner=owner,
            repo=repo,
            index=issue_number
        )

    def get_repo_labels(self, owner: str, repo: str):
        """Get all labels defined in the repository.

        Maps to ``GET /repos/{owner}/{repo}/labels``. Returns a list of label
        dicts (``{id, name, color, ...}``).
        """
        try:
            url = f'/repos/{owner}/{repo}/labels'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return json.loads(raw_data.decode('utf-8'))
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return json.loads(raw_data.decode('utf-8'))

            return []

        except ApiException as e:
            self.logger.error(f"Error getting repository labels: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return []

    def list_all_commits(self, owner: str, repo: str):
        return self.repository.repo_get_all_commits(
            owner=owner,
            repo=repo
        )

    def add_reviewer(self, owner: str, repo: str, pr_number: int, reviewers: List[str]):
        body = {
            "reviewers": reviewers
        }
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers',
            'POST',
            path_params={'owner': owner, 'repo': repo, 'pr_number': pr_number},
            body=body,
            response_type='Repository',
            auth_settings=['AuthorizationHeaderToken']
        )

    def add_reaction_comment(self, owner: str, repo: str, comment_id: int, reaction: str):
        body = {
            "content": reaction
        }
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/issues/comments/{id}/reactions',
            'POST',
            path_params={'owner': owner, 'repo': repo, 'id': comment_id},
            body=body,
            response_type='Repository',
            auth_settings=['AuthorizationHeaderToken']
        )

    def remove_reaction_comment(self, owner: str, repo: str, comment_id: int, reaction: str = "eyes"):
        body = {
            "content": reaction
        }
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/issues/comments/{id}/reactions',
            'DELETE',
            path_params={'owner': owner, 'repo': repo, 'id': comment_id},
            body=body,
            response_type='Repository',
            auth_settings=['AuthorizationHeaderToken']
        )

    def add_labels(self, owner: str, repo: str, issue_number: int, labels: List[int]):
        body = {
            "labels": labels
        }
        return self.issue.issue_add_label(
            owner=owner,
            repo=repo,
            index=issue_number,
            body=body
        )

    def get_pr_commits(self, owner: str, repo: str, pr_number: int):
        """Get all commits in a pull request"""
        try:
            url = f'/repos/{owner}/{repo}/pulls/{pr_number}/commits'

            response = self.api_client.call_api(
                url,
                'GET',
                path_params={},
                response_type=None,
                _return_http_data_only=False,
                _preload_content=False,
                auth_settings=['AuthorizationHeaderToken']
            )

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                commits_data = json.loads(raw_data.decode('utf-8'))
                return commits_data
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                commits_data = json.loads(raw_data.decode('utf-8'))
                return commits_data

            return []

        except ApiException as e:
            self.logger.error(f"Error getting PR commits: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return []
