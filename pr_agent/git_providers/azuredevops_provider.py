from __future__ import annotations

import os
from typing import Optional, Tuple
from urllib.parse import urlparse

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

from ..algo.file_filter import filter_ignored
from ..algo.language_handler import is_valid_file
from ..algo.utils import (PRDescriptionHeader, clip_tokens,
                          find_line_number_of_relevant_line_in_file,
                          load_large_diff)
from ..config_loader import get_settings
from ..log import get_logger
from .git_provider import GitProvider

AZURE_DEVOPS_AVAILABLE = True
ADO_APP_CLIENT_DEFAULT_ID = "499b84ac-1321-427f-aa17-267ca6975798/.default"
MAX_PR_DESCRIPTION_AZURE_LENGTH = 4000-1

try:
    # noinspection PyUnresolvedReferences
    from azure.devops.connection import Connection
    # noinspection PyUnresolvedReferences
    from azure.devops.released.git import (Comment, CommentThread, GitPullRequest, GitVersionDescriptor, GitClient, CommentThreadContext, CommentPosition)
    from azure.devops.released.work_item_tracking import WorkItemTrackingClient
    # noinspection PyUnresolvedReferences
    from azure.identity import DefaultAzureCredential
    from msrest.authentication import BasicAuthentication
except ImportError:
    AZURE_DEVOPS_AVAILABLE = False


class AzureDevopsProvider(GitProvider):

    def __init__(
            self, pr_url: Optional[str] = None, incremental: Optional[bool] = False
    ):
        if not AZURE_DEVOPS_AVAILABLE:
            raise ImportError(
                "Azure DevOps provider is not available. Please install the required dependencies."
            )

        self.azure_devops_client, self.azure_devops_board_client = self._get_azure_devops_client()
        self.diff_files = None
        self.workspace_slug = None
        self.repo_slug = None
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.temp_comments = []
        self.incremental = incremental
        if pr_url:
            self.set_pr(pr_url)

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as comments on the PR.
        """
        post_parameters_list = []
        status = get_settings().azure_devops.get("default_comment_status", "closed")
        for suggestion in code_suggestions:
            body = suggestion['body']
            relevant_file = suggestion['relevant_file']
            relevant_lines_start = suggestion['relevant_lines_start']
            relevant_lines_end = suggestion['relevant_lines_end']

            if not relevant_lines_start or relevant_lines_start == -1:
                get_logger().warning(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}")
                continue

            if relevant_lines_end < relevant_lines_start:
                get_logger().warning(f"Failed to publish code suggestion, "
                                       f"relevant_lines_end is {relevant_lines_end} and "
                                       f"relevant_lines_start is {relevant_lines_start}")
                continue

            thread_context = CommentThreadContext(
                file_path=relevant_file,
                right_file_start=CommentPosition(offset=1, line=relevant_lines_start),
                right_file_end=CommentPosition(offset=1, line=relevant_lines_end))
            comment = Comment(content=body, comment_type=1)
            thread = CommentThread(comments=[comment], thread_context=thread_context, status=status)
            try:
                self.azure_devops_client.create_thread(
                    comment_thread=thread,
                    project=self.workspace_slug,
                    repository_id=self.repo_slug,
                    pull_request_id=self.pr_num
                )
            except Exception as e:
                get_logger().error(f"Azure failed to publish code suggestion, error: {e}", suggestion=suggestion)
        return True

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str, is_temporary: bool = False) -> Comment:
        # comment_id is actually thread_id
        return self.reply_to_thread(comment_id, body, is_temporary)

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
        except Exception as e:
            get_logger().exception(f"Failed to edit comment, error: {e}")

    def remove_comment(self, comment: Comment):
        try:
            self.azure_devops_client.delete_comment(
                repository_id=self.repo_slug,
                pull_request_id=self.pr_num,
                thread_id=comment.thread_id,
                comment_id=comment.id,
                project=self.workspace_slug,
            )
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

    def set_pr(self, pr_url: str):
        self.pr_url = pr_url
        self.workspace_slug, self.repo_slug, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

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
            return list(contents)[0]
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().error(f"Failed to get repo settings, error: {e}")
            return ""

    def get_files(self):
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

            for c in changes_obj.changes:
                files.append(c["item"]["path"])
        return list(set(files))

    def get_diff_files(self) -> list[FilePatchInfo]:
        try:

            if self.diff_files:
                return self.diff_files

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
            diffs = filter_ignored(diffs_original, 'azure')
            if diffs_original != diffs:
                try:
                    get_logger().info(f"Filtered out [ignore] files for pull request:", extra=
                    {"files": diffs_original,  # diffs is just a list of names
                     "filtered_files": diffs})
                except Exception:
                    pass

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
                    get_logger().error(f"Failed to retrieve new file content of {file} at version {version}", error=error)
                    # get_logger().error(
                    #     "Failed to retrieve new file content of %s at version %s. Error: %s",
                    #     file,
                    #     version,
                    #     str(error),
                    # )
                    new_file_content_str = ""

                edit_type = EDIT_TYPE.MODIFIED
                if diff_types[file] == "add":
                    edit_type = EDIT_TYPE.ADDED
                elif diff_types[file] == "delete":
                    edit_type = EDIT_TYPE.DELETED
                elif "rename" in diff_types[file]: # diff_type can be `rename` | `edit, rename`
                    edit_type = EDIT_TYPE.RENAMED

                version = GitVersionDescriptor(
                    version=base_sha.commit_id, version_type="commit"
                )
                if edit_type == EDIT_TYPE.ADDED or edit_type == EDIT_TYPE.RENAMED:
                    original_file_content_str = ""
                else:
                    try:
                        original_file_content_str = self.azure_devops_client.get_item(
                            repository_id=self.repo_slug,
                            path=file,
                            project=self.workspace_slug,
                            version_descriptor=version,
                            download=False,
                            include_content=True,
                        )
                        original_file_content_str = original_file_content_str.content
                    except Exception as error:
                        get_logger().error(f"Failed to retrieve original file content of {file} at version {version}", error=error)
                        original_file_content_str = ""

                patch = load_large_diff(
                    file, new_file_content_str, original_file_content_str, show_warning=False
                ).rstrip()

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
        created_comment = thread_response.comments[0]
        created_comment.thread_id = thread_response.id
        if is_temporary:
            self.temp_comments.append(created_comment)
        return created_comment

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        return self.publish_persistent_comment_full(pr_comment, initial_header, update_header, name, final_update_message)

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
        position, absolute_position = find_line_number_of_relevant_line_in_file(self.get_diff_files(),
                                                                                relevant_file.strip('`'),
                                                                                relevant_line_in_file,
                                                                                absolute_position)
        if position == -1:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"
        path = relevant_file.strip()
        return dict(body=body, path=path, position=position, absolute_position=absolute_position) if subject_type == "LINE" else {}

    def publish_inline_comments(self, comments: list[dict], disable_fallback: bool = False):
            overall_success = True
            for comment in comments:
                try:
                    self.publish_comment(comment["body"],
                                        thread_context={
                                            "filePath": comment["path"],
                                            "rightFileStart": {
                                                "line": comment["absolute_position"],
                                                "offset": comment["position"],
                                            },
                                            "rightFileEnd": {
                                                "line": comment["absolute_position"],
                                                "offset": comment["position"],
                                            },
                                        })
                    if get_settings().config.verbosity_level >= 2:
                        get_logger().info(
                            f"Published code suggestion on {self.pr_num} at {comment['path']}"
                        )
                except Exception as e:
                    if get_settings().config.verbosity_level >= 2:
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

    def get_issue_comments(self) -> list[Comment]:
        threads = self.azure_devops_client.get_threads(repository_id=self.repo_slug, pull_request_id=self.pr_num, project=self.workspace_slug)
        threads.reverse()
        comment_list = []
        for thread in threads:
            for comment in thread.comments:
                if comment.content and comment not in comment_list:
                    comment.body = comment.content
                    comment.thread_id = thread.id
                    comment_list.append(comment)
        return comment_list

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        return True

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    def set_like(self, thread_id: int, comment_id: int, create: bool = True):
        if create:
            self.azure_devops_client.create_like(self.repo_slug, self.pr_num, thread_id, comment_id, project=self.workspace_slug)
        else:
            self.azure_devops_client.delete_like(self.repo_slug, self.pr_num, thread_id, comment_id, project=self.workspace_slug)
            
    def set_thread_status(self, thread_id: int, status: str):
        try:
            self.azure_devops_client.update_thread(CommentThread(status=status), self.repo_slug, self.pr_num, thread_id, self.workspace_slug)
        except Exception as e:
            get_logger().exception(f"Failed to set thread status, error: {e}")
            
    def reply_to_thread(self, thread_id: int, body: str, is_temporary: bool = False) -> Comment:
        try:
            comment = Comment(content=body)
            response = self.azure_devops_client.create_comment(comment, self.repo_slug, self.pr_num, thread_id, self.workspace_slug)
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

        workspace_slug = path_parts[num_parts - 5]
        repo_slug = path_parts[num_parts - 3]
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

    def get_commit_messages(self):
        return ""  # not implemented yet

    def get_pr_id(self):
        try:
            pr_id = f"{self.workspace_slug}/{self.repo_slug}/{self.pr_num}"
            return pr_id
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
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
        last = commits[0]
        url = self.azure_devops_client.normalized_url + "/" + self.workspace_slug + "/_git/" + self.repo_slug + "/commit/" + last.commit_id
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
