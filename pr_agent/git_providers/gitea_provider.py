import json
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import giteapy
from giteapy.rest import ApiException

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import (clip_tokens,
                                 find_line_number_of_relevant_line_in_file)
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


    def publish_inline_comments(self, comments: List[Dict[str, Any]],body : str = "Inline comment") -> None:
        response = self.repo_api.create_inline_comment(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number if self.enabled_pr else self.issue_number,
            body=body,
            commit_id=self.last_commit.sha if self.last_commit else "",
            comments=comments
        )

        if not response:
            self.logger.error("Failed to publish inline comment")
            return

        self.logger.info("Inline comment published")

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

    def remove_reaction(self, comment_id: int) -> None:
        """Remove reaction from a comment"""
        try:
            response = self.repo_api.remove_reaction_comment(
                owner=self.owner,
                repo=self.repo,
                comment_id=comment_id
            )
            if not response:
                self.logger.error("Failed to remove reaction")
        except ApiException as e:
            self.logger.error(f"Error removing reaction: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")

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

    def _get_file_content_from_base(self, filename: str) -> str:
        return self.repo_api.get_file_content(
            owner=self.owner,
            repo=self.repo,
            commit_sha=self.base_sha,
            filepath=filename
        )

    def _get_file_content_from_latest_commit(self, filename: str) -> str:
        return self.repo_api.get_file_content(
            owner=self.owner,
            repo=self.repo,
            commit_sha=self.last_commit.sha,
            filepath=filename
        )

    def get_diff_files(self) -> List[FilePatchInfo]:
        """Get files that were modified in the PR"""
        if self.diff_files:
            return self.diff_files

        invalid_files_names = []
        counter_valid = 0
        diff_files = []
        for file in self.git_files:
            filename = file.get("filename")
            if not filename:
                continue

            if not is_valid_file(filename):
                invalid_files_names.append(filename)
                continue

            counter_valid += 1
            avoid_load = False
            patch = self.file_diffs.get(filename,"")
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
                head_file = self.file_contents.get(filename,"")

            if self.incremental.is_incremental and self.unreviewed_files_set:
                base_file = self._get_file_content_from_latest_commit(filename)
                self.unreviewed_files_set[filename] = patch
            else:
                if avoid_load:
                    base_file = ""
                else:
                    base_file = self._get_file_content_from_base(filename)

            num_plus_lines = file.get("additions",0)
            num_minus_lines = file.get("deletions",0)
            status = file.get("status","")

            if status == 'added':
                edit_type = EDIT_TYPE.ADDED
            elif status == 'removed' or status == 'deleted':
                edit_type = EDIT_TYPE.DELETED
            elif status == 'renamed':
                edit_type = EDIT_TYPE.RENAMED
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
                edit_type=edit_type
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

    def get_repo_settings(self) -> str:
        """Get repository settings"""
        if not self.repo_settings:
            self.logger.error("Repository settings not found")
            return ""

        response = self.repo_api.get_file_content(
            owner=self.owner,
            repo=self.repo,
            commit_sha=self.sha,
            filepath=self.repo_settings
        )
        if not response:
            self.logger.error("Failed to get repository settings")
            return ""

        return response

    def get_user_id(self) -> str:
        """Get the ID of the authenticated user"""
        return f"{self.pr.user.id}" if self.pr else ""

    def is_supported(self, capability) -> bool:
        """Check if the provider is supported"""
        return True

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        return f"{self.base_url}/{self.owner}/{self.repo}.git" #base_url / <OWNER>/<REPO>.git

    def publish_description(self, pr_title: str, pr_body: str) -> None:
        """Publish PR description"""
        response = self.repo_api.edit_pull_request(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number if self.enabled_pr else self.issue_number,
            title=pr_title,
            body=pr_body
        )

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

    def publish_labels(self, labels: List[int]) -> None:
        """Publish labels to the PR"""
        if not labels:
            self.logger.error("No labels provided to publish")
            return None

        response = self.repo_api.add_labels(
            owner=self.owner,
            repo=self.repo,
            issue_number=self.pr_number if self.enabled_pr else self.issue_number,
            labels=labels
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
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/pulls/{pr_number}/reviews',
            'POST',
            path_params={'owner': owner, 'repo': repo, 'pr_number': pr_number},
            body=body,
            response_type='Repository',
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
                return raw_data.decode('utf-8')
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return raw_data.decode('utf-8')
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

            if hasattr(response, 'data'):
                raw_data = response.data.read()
                return raw_data.decode('utf-8')
            elif isinstance(response, tuple):
                raw_data = response[0].read()
                return raw_data.decode('utf-8')

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

    def remove_reaction_comment(self, owner: str, repo: str, comment_id: int):
        return self.api_client.call_api(
            '/repos/{owner}/{repo}/issues/comments/{id}/reactions',
            'DELETE',
            path_params={'owner': owner, 'repo': repo, 'id': comment_id},
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
