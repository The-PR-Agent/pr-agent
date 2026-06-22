import difflib
import json
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
from atlassian.bitbucket import Cloud
from starlette_context import context

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

from ..algo.file_filter import filter_ignored
from ..algo.language_handler import is_valid_file
from ..algo.utils import find_line_number_of_relevant_line_in_file
from ..config_loader import get_settings
from ..log import get_logger
from .git_provider import MAX_FILES_ALLOWED_FULL, GitProvider


def _gef_filename(diff):
    if diff.new.path:
        return diff.new.path
    return diff.old.path


class BitbucketProvider(GitProvider):
    def __init__(
        self, pr_url: Optional[str] = None, incremental: Optional[bool] = False
    ):
        s = requests.Session()
        s.headers["Content-Type"] = "application/json"

        self.auth_type = get_settings().get("BITBUCKET.AUTH_TYPE", "bearer")

        try:
            def get_token(token_name, auth_type_name):
                token = get_settings().get(f"BITBUCKET.{token_name.upper()}", None)
                if not token:
                    raise ValueError(f"{auth_type_name} auth requires a token")
                return token

            if self.auth_type == "basic":
                self.basic_token = get_token("basic_token", "Basic")
                s.headers["Authorization"] = f"Basic {self.basic_token}"
            elif self.auth_type == "bearer":
                try:
                    self.bearer_token = context.get("bitbucket_bearer_token", None)
                except:
                    self.bearer_token = None

                if not self.bearer_token:
                    self.bearer_token = get_token("bearer_token", "Bearer")
                s.headers["Authorization"] = f"Bearer {self.bearer_token}"
            else:
                 raise ValueError(f"Unsupported auth_type: {self.auth_type}")

        except Exception as e:
            get_logger().exception(f"Failed to initialize Bitbucket authentication: {e}")
            raise

        self.headers = s.headers
        self.bitbucket_client = Cloud(session=s)
        self.max_comment_length = 31000
        self.workspace_slug = None
        self.repo_slug = None
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.pr_url = pr_url
        self.temp_comments = []
        self.incremental = incremental
        self.diff_files = None
        self.git_files = None
        if pr_url:
            self.set_pr(pr_url)
        self.bitbucket_comment_api_url = self.pr._BitbucketBase__data["links"]["comments"]["href"]
        self.bitbucket_pull_request_api_url = self.pr._BitbucketBase__data["links"]['self']['href']

    def get_repo_settings(self):
        try:
            url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/"
                   f"{self.pr.destination_branch}/.pr_agent.toml")
            response = requests.request("GET", url, headers=self.headers)
            if response.status_code == 404:  # not found
                return ""
            contents = response.text.encode('utf-8')
            return contents
        except Exception:
            return ""

    def get_git_repo_url(self, pr_url: str=None) -> str: #bitbucket does not support issue url, so ignore param
        try:
            parsed_url = urlparse(self.pr_url)
            return f"{parsed_url.scheme}://{parsed_url.netloc}/{self.workspace_slug}/{self.repo_slug}.git"
        except Exception as e:
            get_logger().exception(f"url is not a valid merge requests url: {self.pr_url}")
            return ""

    # Given a git repo url, return prefix and suffix of the provider in order to view a given file belonging to that repo.
    # Example: git clone git clone https://bitbucket.org/pragent/pr-agent.git and branch: main -> prefix: "https://bitbucket.org/pragent/pr-agent/src/main", suffix: ""
    # In case git url is not provided, provider will use PR context (which includes branch) to determine the prefix and suffix.
    def get_canonical_url_parts(self, repo_git_url:str=None, desired_branch:str=None) -> Tuple[str, str]:
        scheme_and_netloc = None
        if repo_git_url:
            parsed_git_url = urlparse(repo_git_url)
            scheme_and_netloc = parsed_git_url.scheme + "://" + parsed_git_url.netloc
            repo_path = parsed_git_url.path.split('.git')[0][1:] #/<workspace>/<repo>.git -> <workspace>/<repo>
            if repo_path.count('/') != 1:
                get_logger().error(f"repo_git_url is not a valid git repo url: {repo_git_url}")
                return ("", "")
            workspace_name, project_name = repo_path.split('/')
        else:
            desired_branch = self.get_repo_default_branch()
            parsed_pr_url = urlparse(self.pr_url)
            scheme_and_netloc = parsed_pr_url.scheme + "://" + parsed_pr_url.netloc
            workspace_name, project_name = (self.workspace_slug, self.repo_slug)
        prefix = f"{scheme_and_netloc}/{workspace_name}/{project_name}/src/{desired_branch}"
        suffix = "" #None
        return (prefix, suffix)


    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as comments on the PR.
        """
        post_parameters_list = []
        for suggestion in code_suggestions:
            body = suggestion["body"]
            original_suggestion = suggestion.get('original_suggestion', None)  # needed for diff code
            if original_suggestion:
                try:
                    existing_code = original_suggestion['existing_code'].rstrip() + "\n"
                    improved_code = original_suggestion['improved_code'].rstrip() + "\n"
                    diff = difflib.unified_diff(existing_code.split('\n'),
                                                improved_code.split('\n'), n=999)
                    patch_orig = "\n".join(diff)
                    patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')
                    diff_code = f"\n\n```diff\n{patch.rstrip()}\n```"
                    # replace ```suggestion ... ``` with diff_code, using regex:
                    body = re.sub(r'```suggestion.*?```', diff_code, body, flags=re.DOTALL)
                except Exception as e:
                    get_logger().exception(f"Bitbucket failed to get diff code for publishing, error: {e}")
                    continue

            relevant_file = suggestion["relevant_file"]
            relevant_lines_start = suggestion["relevant_lines_start"]
            relevant_lines_end = suggestion["relevant_lines_end"]

            if not relevant_lines_start or relevant_lines_start == -1:
                get_logger().exception(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}"
                )
                continue

            if relevant_lines_end < relevant_lines_start:
                get_logger().exception(
                    f"Failed to publish code suggestion, "
                    f"relevant_lines_end is {relevant_lines_end} and "
                    f"relevant_lines_start is {relevant_lines_start}"
                )
                continue

            if relevant_lines_end > relevant_lines_start:
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_end,
                    "start_line": relevant_lines_start,
                    "start_side": "RIGHT",
                }
            else:  # API is different for single line comments
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_start,
                    "side": "RIGHT",
                }
            post_parameters_list.append(post_parameters)

        try:
            self.publish_inline_comments(post_parameters_list)
            return True
        except Exception as e:
            get_logger().error(f"Bitbucket failed to publish code suggestion, error: {e}")
            return False

    def publish_file_comments(self, file_comments: list) -> bool:
        pass

    def is_supported(self, capability: str) -> bool:
        if capability in ['get_issue_comments', 'publish_inline_comments', 'get_labels', 'gfm_markdown',
                            'publish_file_comments']:
            return False
        return True

    def set_pr(self, pr_url: str):
        self.workspace_slug, self.repo_slug, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def get_files(self):
        try:
            git_files = context.get("git_files", None)
            if git_files:
                return git_files
            self.git_files = [_gef_filename(diff) for diff in self.pr.diffstat()]
            context["git_files"] = self.git_files
            return self.git_files
        except Exception:
            if not self.git_files:
                self.git_files = [_gef_filename(diff) for diff in self.pr.diffstat()]
            return self.git_files

    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            return self.diff_files

        diffs_original = list(self.pr.diffstat())
        diffs = filter_ignored(diffs_original, 'bitbucket')
        if diffs != diffs_original:
            try:
                names_original = [d.new.path for d in diffs_original]
                names_kept = [d.new.path for d in diffs]
                names_filtered = list(set(names_original) - set(names_kept))
                get_logger().info(f"Filtered out [ignore] files for PR", extra={
                    'original_files': names_original,
                    'names_kept': names_kept,
                    'names_filtered': names_filtered

                })
            except Exception as e:
                pass

        # get the pr patches
        try:
            pr_patches = self.pr.diff()
        except Exception as e:
            # Try different encodings if UTF-8 fails
            get_logger().warning(f"Failed to decode PR patch with utf-8, error: {e}")
            encodings_to_try = ['iso-8859-1', 'latin-1', 'ascii', 'utf-16']
            pr_patches = None
            for encoding in encodings_to_try:
                try:
                    pr_patches = self.pr.diff(encoding=encoding)
                    get_logger().info(f"Successfully decoded PR patch with encoding {encoding}")
                    break
                except UnicodeDecodeError:
                    continue

            if pr_patches is None:
                raise ValueError(f"Failed to decode PR patch with encodings {encodings_to_try}")

        diff_split = ["diff --git" + x for x in pr_patches.split("diff --git") if x.strip()]
        # filter all elements of 'diff_split' that are of indices in 'diffs_original' that are not in 'diffs'
        if len(diff_split) > len(diffs) and len(diffs_original) == len(diff_split):
            diff_split = [diff_split[i] for i in range(len(diff_split)) if diffs_original[i] in diffs]
        if len(diff_split) != len(diffs):
            get_logger().error(f"Error - failed to split the diff into {len(diffs)} parts")
            return []
        # bitbucket diff has a header for each file, we need to remove it:
        # "diff --git filename
        # new file mode 100644 (optional)
        #  index caa56f0..61528d7 100644
        #   --- a/pr_agent/cli_pip.py
        #  +++ b/pr_agent/cli_pip.py
        #   @@ -... @@"
        for i, _ in enumerate(diff_split):
            diff_split_lines = diff_split[i].splitlines()
            if (len(diff_split_lines) >= 6) and \
                    ((diff_split_lines[2].startswith("---") and
                      diff_split_lines[3].startswith("+++") and
                      diff_split_lines[4].startswith("@@")) or
                     (diff_split_lines[3].startswith("---") and  # new or deleted file
                      diff_split_lines[4].startswith("+++") and
                      diff_split_lines[5].startswith("@@"))):
                diff_split[i] = "\n".join(diff_split_lines[4:])
            else:
                if diffs[i].data.get('lines_added', 0) == 0 and diffs[i].data.get('lines_removed', 0) == 0:
                    diff_split[i] = ""
                elif len(diff_split_lines) <= 3:
                    diff_split[i] = ""
                    get_logger().info(f"Disregarding empty diff for file {_gef_filename(diffs[i])}")
                else:
                    get_logger().warning(f"Bitbucket failed to get diff for file {_gef_filename(diffs[i])}")
                    diff_split[i] = ""

        invalid_files_names = []
        diff_files = []
        counter_valid = 0
        # get full files
        for index, diff in enumerate(diffs):
            file_path = _gef_filename(diff)
            if not is_valid_file(file_path):
                invalid_files_names.append(file_path)
                continue

            try:
                counter_valid += 1
                if get_settings().get("bitbucket_app.avoid_full_files", False):
                    original_file_content_str = ""
                    new_file_content_str = ""
                elif counter_valid < MAX_FILES_ALLOWED_FULL // 2:  # factor 2 because bitbucket has limited API calls
                    if diff.old.get_data("links"):
                        original_file_content_str = self._get_pr_file_content(
                            diff.old.get_data("links")['self']['href'])
                    else:
                        original_file_content_str = ""
                    if diff.new.get_data("links"):
                        new_file_content_str = self._get_pr_file_content(diff.new.get_data("links")['self']['href'])
                    else:
                        new_file_content_str = ""
                else:
                    if counter_valid == MAX_FILES_ALLOWED_FULL // 2:
                        get_logger().info(
                            f"Bitbucket too many files in PR, will avoid loading full content for rest of files")
                    original_file_content_str = ""
                    new_file_content_str = ""
            except Exception as e:
                get_logger().exception(f"Error - bitbucket failed to get file content, error: {e}")
                original_file_content_str = ""
                new_file_content_str = ""

            file_patch_canonic_structure = FilePatchInfo(
                original_file_content_str,
                new_file_content_str,
                diff_split[index],
                file_path,
            )

            if diff.data['status'] == 'added':
                file_patch_canonic_structure.edit_type = EDIT_TYPE.ADDED
            elif diff.data['status'] == 'removed':
                file_patch_canonic_structure.edit_type = EDIT_TYPE.DELETED
            elif diff.data['status'] == 'modified':
                file_patch_canonic_structure.edit_type = EDIT_TYPE.MODIFIED
            elif diff.data['status'] == 'renamed':
                file_patch_canonic_structure.edit_type = EDIT_TYPE.RENAMED
            diff_files.append(file_patch_canonic_structure)

        if invalid_files_names:
            get_logger().info(f"Disregarding files with invalid extensions:\n{invalid_files_names}")

        self.diff_files = diff_files
        return diff_files

    def get_latest_commit_url(self):
        return self.pr.data['source']['commit']['links']['html']['href']

    def get_comment_url(self, comment):
        return comment.data['links']['html']['href']

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        try:
            for comment in self.pr.comments():
                body = comment.raw
                if initial_header in body:
                    latest_commit_url = self.get_latest_commit_url()
                    comment_url = self.get_comment_url(comment)
                    if update_header:
                        updated_header = f"{initial_header}\n\n#### ({name.capitalize()} updated until commit {latest_commit_url})\n"
                        pr_comment_updated = pr_comment.replace(initial_header, updated_header)
                    else:
                        pr_comment_updated = pr_comment
                    get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
                    d = {"content": {"raw": pr_comment_updated}}
                    response = comment._update_data(comment.put(None, data=d))
                    if final_update_message:
                        self.publish_comment(
                            f"**[Persistent {name}]({comment_url})** updated to latest commit {latest_commit_url}")
                    return
        except Exception as e:
            get_logger().exception(f"Failed to update persistent review, error: {e}")
            pass
        self.publish_comment(pr_comment)

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment: {pr_comment}")
            return None
        pr_comment = self.limit_output_characters(pr_comment, self.max_comment_length)
        comment = self.pr.comment(pr_comment)
        if is_temporary:
            self.temp_comments.append(comment["id"])
        return comment

    def edit_comment(self, comment, body: str):
        try:
            body = self.limit_output_characters(body, self.max_comment_length)
            comment.update(body)
        except Exception as e:
            get_logger().exception(f"Failed to update comment, error: {e}")

    def remove_initial_comment(self):
        try:
            for comment in self.temp_comments:
                self.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def remove_comment(self, comment):
        try:
            self.pr.delete(f"comments/{comment}")
        except Exception as e:
            get_logger().exception(f"Failed to remove comment, error: {e}")

    # function to create_inline_comment
    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                              absolute_position: int = None):
        body = self.limit_output_characters(body, self.max_comment_length)
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
        return dict(body=body, path=path, position=absolute_position) if subject_type == "LINE" else {}

    def publish_inline_comment(self, comment: str, from_line: int, file: str, original_suggestion=None):
        comment = self.limit_output_characters(comment, self.max_comment_length)
        payload = json.dumps({
            "content": {
                "raw": comment,
            },
            "inline": {
                "to": from_line,
                "path": file
            },
        })
        response = requests.request(
            "POST", self.bitbucket_comment_api_url, data=payload, headers=self.headers
        )
        return response

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        if relevant_line_start == -1:
            link = f"{self.pr_url}/#L{relevant_file}"
        else:
            link = f"{self.pr_url}/#L{relevant_file}T{relevant_line_start}"
        return link

    def generate_link_to_relevant_line_number(self, suggestion) -> str:
        try:
            relevant_file = suggestion['relevant_file'].strip('`').strip("'").rstrip()
            relevant_line_str = suggestion['relevant_line'].rstrip()
            if not relevant_line_str:
                return ""

            diff_files = self.get_diff_files()
            position, absolute_position = find_line_number_of_relevant_line_in_file \
                (diff_files, relevant_file, relevant_line_str)

            if absolute_position != -1 and self.pr_url:
                link = f"{self.pr_url}/#L{relevant_file}T{absolute_position}"
                return link
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Failed adding line link, error: {e}")

        return ""

    def publish_inline_comments(self, comments: list[dict]):
        for comment in comments:
            if 'position' in comment:
                self.publish_inline_comment(comment['body'], comment['position'], comment['path'])
            elif 'start_line' in comment:  # multi-line comment
                # note that bitbucket does not seem to support range - only a comment on a single line - https://community.developer.atlassian.com/t/api-post-endpoint-for-inline-pull-request-comments/60452
                self.publish_inline_comment(comment['body'], comment['start_line'], comment['path'])
            elif 'line' in comment:  # single-line comment
                self.publish_inline_comment(comment['body'], comment['line'], comment['path'])
            else:
                get_logger().error(f"Could not publish inline comment {comment}")

    def get_title(self):
        return self.pr.title

    def get_languages(self):
        languages = {self._get_repo().get_data("language"): 0}
        return languages

    def get_pr_branch(self):
        return self.pr.source_branch

    # This function attempts to get the default branch of the repository. As a fallback, uses the PR destination branch.
    # Note: Must be running from a PR context.
    def get_repo_default_branch(self):
        try:
            url_repo = f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/"
            response_repo = requests.request("GET", url_repo, headers=self.headers).json()
            return response_repo['mainbranch']['name']
        except:
            return self.pr.destination_branch

    def get_pr_owner_id(self) -> str | None:
        return self.workspace_slug

    def get_pr_description_full(self):
        return self.pr.description

    def get_user_id(self):
        return 0

    def get_issue_comments(self):
        raise NotImplementedError(
            "Bitbucket provider does not support issue comments yet"
        )

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        return True

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    @staticmethod
    def _parse_pr_url(pr_url: str) -> Tuple[str, int, int]:
        parsed_url = urlparse(pr_url)

        if "bitbucket.org" not in parsed_url.netloc:
            raise ValueError("The provided URL is not a valid Bitbucket URL")

        path_parts = parsed_url.path.strip("/").split("/")

        if len(path_parts) < 4 or path_parts[2] != "pull-requests":
            raise ValueError(
                "The provided URL does not appear to be a Bitbucket PR URL"
            )

        workspace_slug = path_parts[0]
        repo_slug = path_parts[1]
        try:
            pr_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError("Unable to convert PR number to integer") from e

        return workspace_slug, repo_slug, pr_number

    def _get_repo(self):
        if self.repo is None:
            self.repo = self.bitbucket_client.workspaces.get(
                self.workspace_slug
            ).repositories.get(self.repo_slug)
        return self.repo

    def _get_pr(self):
        return self._get_repo().pullrequests.get(self.pr_num)

    def get_pr_file_content(self, file_path: str, branch: str) -> str:
        try:
            if branch == self.pr.source_branch:
                branch = self.pr.data["source"]["commit"]["hash"]
            elif branch == self.pr.destination_branch:
                branch = self.pr.data["destination"]["commit"]["hash"]
            url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/"
                   f"{branch}/{file_path}")
            response = requests.request("GET", url, headers=self.headers)
            if response.status_code == 404:  # not found
                return ""
            contents = response.text
            return contents
        except Exception:
            return ""

    def create_or_update_pr_file(self, file_path: str, branch: str, contents="", message="") -> None:
        url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/")
        if not message:
            if contents:
                message = f"Update {file_path}"
            else:
                message = f"Create {file_path}"
        files = {file_path: contents}
        data = {
            "message": message,
            "branch": branch
        }
        headers = {'Authorization': self.headers['Authorization']} if 'Authorization' in self.headers else {}
        try:
            requests.request("POST", url, headers=headers, data=data, files=files)
        except Exception:
            get_logger().exception(f"Failed to create empty file {file_path} in branch {branch}")

    def _get_pr_file_content(self, remote_link: str):
        try:
            response = requests.request("GET", remote_link, headers=self.headers)
            if response.status_code == 404:  # not found
                return ""
            contents = response.text
            return contents
        except Exception:
            return ""

    def get_commit_messages(self):
        return ""  # not implemented yet

    # bitbucket does not support labels
    def publish_description(self, pr_title: str, description: str):
        payload = json.dumps({
            "description": description,
            "title": pr_title

        })

        response = requests.request("PUT", self.bitbucket_pull_request_api_url, headers=self.headers, data=payload)
        try:
            if response.status_code != 200:
                get_logger().info(f"Failed to update description, error code: {response.status_code}")
        except:
            pass
        return response

    # bitbucket does not support labels
    def publish_labels(self, pr_types: list):
        pass

    # bitbucket does not support labels
    def get_pr_labels(self, update=False):
        pass
    #Clone related
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        if "bitbucket.org" not in repo_url_to_clone:
            get_logger().error("Repo URL is not a valid bitbucket URL.")
            return None

        (scheme, base_url) = repo_url_to_clone.split("bitbucket.org")
        if not all([scheme, base_url]):
            get_logger().error(f"repo_url_to_clone: {repo_url_to_clone} is not a valid bitbucket URL.")
            return None

        if self.auth_type == "basic":
            # Basic auth with token
            clone_url = f"{scheme}x-token-auth:{self.basic_token}@bitbucket.org{base_url}"
        elif self.auth_type == "bearer":
            # Bearer token
            clone_url = f"{scheme}x-token-auth:{self.bearer_token}@bitbucket.org{base_url}"
        else:
            # This case should ideally not be reached if __init__ validates auth_type
            get_logger().error(f"Unsupported or uninitialized auth_type: {getattr(self, 'auth_type', 'N/A')}. Returning None")
            return None

        return clone_url
