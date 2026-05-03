from abc import ABC, abstractmethod
# enum EDIT_TYPE (ADDED, DELETED, MODIFIED, RENAMED)
import os
import shutil
import subprocess
from typing import Optional, Tuple

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import Range, process_description
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

MAX_FILES_ALLOWED_FULL = 50

def get_git_ssl_env() -> dict[str, str]:
    """
    Get git SSL configuration arguments for per-command use.
    This fixes SSL certificate issues when cloning repos with self-signed certificates.
    Returns the current environment with the addition of SSL config changes if any such SSL certificates exist.
    """
    ssl_cert_file = os.environ.get('SSL_CERT_FILE')
    requests_ca_bundle = os.environ.get('REQUESTS_CA_BUNDLE')
    git_ssl_ca_info = os.environ.get('GIT_SSL_CAINFO')

    chosen_cert_file = ""

    # Try SSL_CERT_FILE first
    if ssl_cert_file:
        if os.path.exists(ssl_cert_file):
            if ((requests_ca_bundle and requests_ca_bundle != ssl_cert_file)
                    or (git_ssl_ca_info and git_ssl_ca_info != ssl_cert_file)):
                get_logger().warning(f"Found mismatch among: SSL_CERT_FILE, REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO. "
                                     f"Using the SSL_CERT_FILE to resolve ambiguity.",
                                  artifact={"ssl_cert_file": ssl_cert_file, "requests_ca_bundle": requests_ca_bundle,
                                            'git_ssl_ca_info': git_ssl_ca_info})
            else:
                get_logger().info(f"Using SSL certificate bundle for git operations", artifact={"ssl_cert_file": ssl_cert_file})
            chosen_cert_file = ssl_cert_file
        else:
            get_logger().warning("SSL certificate bundle not found for git operations", artifact={"ssl_cert_file": ssl_cert_file})

    # Fallback to REQUESTS_CA_BUNDLE
    elif requests_ca_bundle:
        if os.path.exists(requests_ca_bundle):
            if (git_ssl_ca_info and git_ssl_ca_info != requests_ca_bundle):
                get_logger().warning(f"Found mismatch between: REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO. "
                                     f"Using the REQUESTS_CA_BUNDLE to resolve ambiguity.",
                artifact = {"requests_ca_bundle": requests_ca_bundle, 'git_ssl_ca_info': git_ssl_ca_info})
            else:
                get_logger().info("Using SSL certificate bundle from REQUESTS_CA_BUNDLE for git operations",
                                  artifact={"requests_ca_bundle": requests_ca_bundle})
            chosen_cert_file = requests_ca_bundle
        else:
            get_logger().warning("requests CA bundle not found for git operations", artifact={"requests_ca_bundle": requests_ca_bundle})

    #Fallback to GIT CA:
    elif git_ssl_ca_info:
        if os.path.exists(git_ssl_ca_info):
            get_logger().info("Using git SSL CA info from GIT_SSL_CAINFO for git operations",
                              artifact={"git_ssl_ca_info": git_ssl_ca_info})
            chosen_cert_file = git_ssl_ca_info
        else:
            get_logger().warning("git SSL CA info not found for git operations", artifact={"git_ssl_ca_info": git_ssl_ca_info})

    else:
        get_logger().warning("Neither SSL_CERT_FILE nor REQUESTS_CA_BUNDLE nor GIT_SSL_CAINFO are defined, or they are defined but not found. Returning environment without SSL configuration")

    returned_env = os.environ.copy()
    if chosen_cert_file:
        returned_env.update({"GIT_SSL_CAINFO": chosen_cert_file, "REQUESTS_CA_BUNDLE": chosen_cert_file})
    return returned_env


class GitProvider(ABC):
    @abstractmethod
    def is_supported(self, capability: str) -> bool:
        pass

    #Given a url (issues or PR/MR) - get the .git repo url to which they belong. Needs to be implemented by the provider.
    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        get_logger().warning("Not implemented! Returning empty url")
        return ""

    # Given a git repo url, return prefix and suffix of the provider in order to view a given file belonging to that repo. Needs to be implemented by the provider.
    # For example: For a git: https://git_provider.com/MY_PROJECT/MY_REPO.git and desired branch: <MY_BRANCH> then it should return ('https://git_provider.com/projects/MY_PROJECT/repos/MY_REPO/.../<MY_BRANCH>', '?=<SOME HEADER>')
    # so that to properly view the file: docs/readme.md -> <PREFIX>/docs/readme.md<SUFFIX> -> https://git_provider.com/projects/MY_PROJECT/repos/MY_REPO/<MY_BRANCH>/docs/readme.md?=<SOME HEADER>)
    def get_canonical_url_parts(self, repo_git_url:str, desired_branch:str) -> Tuple[str, str]:
        get_logger().warning("Not implemented! Returning empty prefix and suffix")
        return ("", "")


    #Clone related API
    #An object which ensures deletion of a cloned repo, once it becomes out of scope.
    # Example usage:
    #    with TemporaryDirectory() as tmp_dir:
    #            returned_obj: GitProvider.ScopedClonedRepo = self.git_provider.clone(self.repo_url, tmp_dir, remove_dest_folder=False)
    #            print(returned_obj.path) #Use returned_obj.path.
    #    #From this point, returned_obj.path may be deleted at any point and therefore must not be used.
    class ScopedClonedRepo(object):
        def __init__(self, dest_folder):
            self.path = dest_folder

        def __del__(self):
            if self.path and os.path.exists(self.path):
                shutil.rmtree(self.path, ignore_errors=True)

    #Method to allow implementors to manipulate the repo url to clone (such as embedding tokens in the url string). Needs to be implemented by the provider.
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        get_logger().warning("Not implemented! Returning None")
        return None

    # Does a shallow clone, using a forked process to support a timeout guard.
    # In case operation has failed, it is expected to throw an exception as this method does not return a value.
    def _clone_inner(self, repo_url: str, dest_folder: str, operation_timeout_in_seconds: int=None) -> None:
        #The following ought to be equivalent to:
        # #Repo.clone_from(repo_url, dest_folder)
        # , but with throwing an exception upon timeout.
        # Note: This can only be used in context that supports using pipes.
        try:
            ssl_env = get_git_ssl_env()
        except Exception as e:
            get_logger().exception(
                "Failed to prepare SSL environment for git operations, falling back to default env",
                artifact={"error": e}
            )
            ssl_env = os.environ.copy()

        subprocess.run([
            "git", "clone",
            "--filter=blob:none",
            "--depth", "1",
            repo_url, dest_folder
        ], env=ssl_env, check=True,  # check=True will raise an exception if the command fails
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=operation_timeout_in_seconds)

    CLONE_TIMEOUT_SEC = 20
    # Clone a given url to a destination folder. If successful, returns an object that wraps the destination folder,
    # deleting it once it is garbage collected. See: GitProvider.ScopedClonedRepo for more details.
    def clone(self, repo_url_to_clone: str, dest_folder: str, remove_dest_folder: bool = True,
              operation_timeout_in_seconds: int=CLONE_TIMEOUT_SEC) -> ScopedClonedRepo|None:
        returned_obj = None
        clone_url = self._prepare_clone_url_with_token(repo_url_to_clone)
        if not clone_url:
            get_logger().error("Clone failed: Unable to obtain url to clone.")
            return returned_obj
        try:
            if remove_dest_folder and os.path.exists(dest_folder) and os.path.isdir(dest_folder):
                shutil.rmtree(dest_folder)
            self._clone_inner(clone_url, dest_folder, operation_timeout_in_seconds)
            returned_obj = GitProvider.ScopedClonedRepo(dest_folder)
        except Exception as e:
            get_logger().exception(f"Clone failed: Could not clone url.",
                artifact={"error": str(e), "url": clone_url, "dest_folder": dest_folder})
        finally:
            return returned_obj

    @abstractmethod
    def get_files(self) -> list:
        pass

    @abstractmethod
    def get_diff_files(self) -> list[FilePatchInfo]:
        pass

    def get_incremental_commits(self, is_incremental):
        pass

    @abstractmethod
    def publish_description(self, pr_title: str, pr_body: str):
        pass

    @abstractmethod
    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        pass

    @abstractmethod
    def get_languages(self):
        pass

    @abstractmethod
    def get_pr_branch(self):
        pass

    @abstractmethod
    def get_user_id(self):
        pass

    @abstractmethod
    def get_pr_description_full(self) -> str:
        pass

    def edit_comment(self, comment, body: str):
        pass

    def edit_comment_from_comment_id(self, comment_id: int, body: str):
        pass

    def get_comment_body_from_comment_id(self, comment_id: int) -> str:
        pass

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        pass

    def get_pr_description(self, full: bool = True, split_changes_walkthrough=False) -> str | tuple:
        from pr_agent.algo.utils import clip_tokens
        from pr_agent.config_loader import get_settings
        max_tokens_description = get_settings().get("CONFIG.MAX_DESCRIPTION_TOKENS", None)
        description = self.get_pr_description_full() if full else self.get_user_description()
        if split_changes_walkthrough:
            description, files = process_description(description)
            if max_tokens_description:
                description = clip_tokens(description, max_tokens_description)
            return description, files
        else:
            if max_tokens_description:
                description = clip_tokens(description, max_tokens_description)
            return description

    def get_user_description(self) -> str:
        if hasattr(self, 'user_description') and not (self.user_description is None):
            return self.user_description

        description = (self.get_pr_description_full() or "").strip()
        description_lowercase = description.lower()
        get_logger().debug(f"Existing description", description=description_lowercase)

        # if the existing description wasn't generated by the pr-agent, just return it as-is
        if not self._is_generated_by_pr_agent(description_lowercase):
            get_logger().info(f"Existing description was not generated by the pr-agent")
            self.user_description = description
            return description

        # if the existing description was generated by the pr-agent, but it doesn't contain a user description,
        # return nothing (empty string) because it means there is no user description
        user_description_header = "### **user description**"
        if user_description_header not in description_lowercase:
            get_logger().info(f"Existing description was generated by the pr-agent, but it doesn't contain a user description")
            return ""

        # otherwise, extract the original user description from the existing pr-agent description and return it
        # user_description_start_position = description_lowercase.find(user_description_header) + len(user_description_header)
        # return description[user_description_start_position:].split("\n", 1)[-1].strip()

        # the 'user description' is in the beginning. extract and return it
        possible_headers = self._possible_headers()
        start_position = description_lowercase.find(user_description_header) + len(user_description_header)
        end_position = len(description)
        for header in possible_headers: # try to clip at the next header
            if header != user_description_header and header in description_lowercase:
                end_position = min(end_position, description_lowercase.find(header))
        if end_position != len(description) and end_position > start_position:
            original_user_description = description[start_position:end_position].strip()
            if original_user_description.endswith("___"):
                original_user_description = original_user_description[:-3].strip()
        else:
            original_user_description = description.split("___")[0].strip()
            if original_user_description.lower().startswith(user_description_header):
                original_user_description = original_user_description[len(user_description_header):].strip()

        get_logger().info(f"Extracted user description from existing description",
                          description=original_user_description)
        self.user_description = original_user_description
        return original_user_description

    def _possible_headers(self):
        return ("### **user description**", "### **pr type**", "### **pr description**", "### **pr labels**", "### **type**", "### **description**",
                "### **labels**", "### 🤖 generated by pr agent")

    def _is_generated_by_pr_agent(self, description_lowercase: str) -> bool:
        possible_headers = self._possible_headers()
        return any(description_lowercase.startswith(header) for header in possible_headers)

    @abstractmethod
    def get_repo_settings(self):
        pass

    def get_repo_file(self, file_path: str) -> str:
        """
        Read a text file from the PR's head branch root directory.

        Args:
            file_path: Relative path to the file from the repository root.

        Returns:
            The file content as a UTF-8 string, or "" if the file does not exist
            or cannot be read.
        """
        return ""

    def get_workspace_name(self):
        return ""

    def get_pr_id(self):
        return ""

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        return ""

    def get_lines_link_original_file(self, filepath:str, component_range: Range) -> str:
        return ""

    #### comments operations ####
    @abstractmethod
    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        pass

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        return self.publish_comment(pr_comment)

    def publish_persistent_comment_full(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        try:
            prev_comments = list(self.get_issue_comments())
            for comment in prev_comments:
                if comment.body.startswith(initial_header):
                    latest_commit_url = self.get_latest_commit_url()
                    comment_url = self.get_comment_url(comment)
                    if update_header:
                        updated_header = f"{initial_header}\n\n#### ({name.capitalize()} updated until commit {latest_commit_url})\n"
                        pr_comment_updated = pr_comment.replace(initial_header, updated_header)
                    else:
                        pr_comment_updated = pr_comment
                    get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
                    # response = self.mr.notes.update(comment.id, {'body': pr_comment_updated})
                    self.edit_comment(comment, pr_comment_updated)
                    if final_update_message:
                        return self.publish_comment(
                            f"**[Persistent {name}]({comment_url})** updated to latest commit {latest_commit_url}")
                    return comment
        except Exception as e:
            get_logger().exception(f"Failed to update persistent review, error: {e}")
            pass
        return self.publish_comment(pr_comment)

    @abstractmethod
    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        pass

    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                              absolute_position: int = None):
        raise NotImplementedError("This git provider does not support creating inline comments yet")

    @abstractmethod
    def publish_inline_comments(self, comments: list[dict]):
        pass

    @abstractmethod
    def remove_initial_comment(self):
        pass

    @abstractmethod
    def remove_comment(self, comment):
        pass

    @abstractmethod
    def get_issue_comments(self):
        pass

    def get_comment_url(self, comment) -> str:
        return ""

    def get_review_thread_comments(self, comment_id: int) -> list[dict]:
        pass

    #### labels operations ####
    @abstractmethod
    def publish_labels(self, labels):
        pass

    @abstractmethod
    def get_pr_labels(self, update=False):
        pass

    def get_repo_labels(self):
        pass

    @abstractmethod
    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        pass

    @abstractmethod
    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        pass

    #### commits operations ####
    @abstractmethod
    def get_commit_messages(self):
        pass

    def get_pr_url(self) -> str:
        if hasattr(self, 'pr_url'):
            return self.pr_url
        return ""

    def get_latest_commit_url(self) -> str:
        return ""

    def auto_approve(self) -> bool:
        return False

    def calc_pr_statistics(self, pull_request_data: dict):
        return {}

    def get_num_of_files(self):
        try:
            return len(self.get_diff_files())
        except Exception as e:
            return -1

    def limit_output_characters(self, output: str, max_chars: int):
        return output[:max_chars] + '...' if len(output) > max_chars else output


def get_main_pr_language(languages, files) -> str:
    """
    Get the main language of the commit. Return an empty string if cannot determine.
    """
    main_language_str = ""
    if not languages:
        get_logger().info("No languages detected")
        return main_language_str
    if not files:
        get_logger().info("No files in diff")
        return main_language_str

    try:
        top_language = max(languages, key=languages.get).lower()

        # validate that the specific commit uses the main language
        extension_list = []
        for file in files:
            if not file:
                continue
            if isinstance(file, str):
                file = FilePatchInfo(base_file=None, head_file=None, patch=None, filename=file)
            extension_list.append(file.filename.rsplit('.')[-1])

        # get the most common extension
        most_common_extension = '.' + max(set(extension_list), key=extension_list.count)
        try:
            language_extension_map_org = get_settings().language_extension_map_org
            language_extension_map = {k.lower(): v for k, v in language_extension_map_org.items()}

            if top_language in language_extension_map and most_common_extension in language_extension_map[top_language]:
                main_language_str = top_language
            else:
                for language, extensions in language_extension_map.items():
                    if most_common_extension in extensions:
                        main_language_str = language
                        break
        except Exception as e:
            get_logger().exception(f"Failed to get main language: {e}")

        ## old approach:
        # most_common_extension = max(set(extension_list), key=extension_list.count)
        # if most_common_extension == 'py' and top_language == 'python' or \
        #         most_common_extension == 'js' and top_language == 'javascript' or \
        #         most_common_extension == 'ts' and top_language == 'typescript' or \
        #         most_common_extension == 'tsx' and top_language == 'typescript' or \
        #         most_common_extension == 'go' and top_language == 'go' or \
        #         most_common_extension == 'java' and top_language == 'java' or \
        #         most_common_extension == 'c' and top_language == 'c' or \
        #         most_common_extension == 'cpp' and top_language == 'c++' or \
        #         most_common_extension == 'cs' and top_language == 'c#' or \
        #         most_common_extension == 'swift' and top_language == 'swift' or \
        #         most_common_extension == 'php' and top_language == 'php' or \
        #         most_common_extension == 'rb' and top_language == 'ruby' or \
        #         most_common_extension == 'rs' and top_language == 'rust' or \
        #         most_common_extension == 'scala' and top_language == 'scala' or \
        #         most_common_extension == 'kt' and top_language == 'kotlin' or \
        #         most_common_extension == 'pl' and top_language == 'perl' or \
        #         most_common_extension == top_language:
        #     main_language_str = top_language

    except Exception as e:
        get_logger().exception(e)

    return main_language_str




class IncrementalPR:
    def __init__(self, is_incremental: bool = False):
        self.is_incremental = is_incremental
        self.commits_range = None
        self.first_new_commit = None
        self.last_seen_commit = None

    @property
    def first_new_commit_sha(self):
        return None if self.first_new_commit is None else self.first_new_commit.sha

    @property
    def last_seen_commit_sha(self):
        return None if self.last_seen_commit is None else self.last_seen_commit.sha
