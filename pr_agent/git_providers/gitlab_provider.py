import difflib
import hashlib
import re
import urllib.parse
from typing import Any, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

import gitlab
import requests
from gitlab import (GitlabAuthenticationError, GitlabCreateError,
                    GitlabGetError, GitlabUpdateError)

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

from ..algo.file_filter import filter_ignored
from ..algo.git_patch_processing import decode_if_bytes
from ..algo.language_handler import is_valid_file
from ..algo.utils import (clip_tokens,
                          find_line_number_of_relevant_line_in_file,
                          load_large_diff)
from ..config_loader import get_settings
from ..log import get_logger
from .git_provider import MAX_FILES_ALLOWED_FULL, GitProvider


class DiffNotFoundError(Exception):
    """Raised when the diff for a merge request cannot be found."""
    pass

class GitLabProvider(GitProvider):

    def __init__(self, merge_request_url: Optional[str] = None, incremental: Optional[bool] = False):
        gitlab_url = get_settings().get("GITLAB.URL", None)
        if not gitlab_url:
            raise ValueError("GitLab URL is not set in the config file")
        self.gitlab_url = gitlab_url
        ssl_verify = get_settings().get("GITLAB.SSL_VERIFY", True)
        gitlab_access_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", None)
        if not gitlab_access_token:
            raise ValueError("GitLab personal access token is not set in the config file")
        # Authentication method selection via configuration
        auth_method = get_settings().get("GITLAB.AUTH_TYPE", "oauth_token")

        # Basic validation of authentication type
        if auth_method not in ["oauth_token", "private_token"]:
            raise ValueError(f"Unsupported GITLAB.AUTH_TYPE: '{auth_method}'. "
                           f"Must be 'oauth_token' or 'private_token'.")

        # Create GitLab instance based on authentication method
        try:
            if auth_method == "oauth_token":
                self.gl = gitlab.Gitlab(
                    url=gitlab_url,
                    oauth_token=gitlab_access_token,
                    ssl_verify=ssl_verify
                )
            else:  # private_token
                self.gl = gitlab.Gitlab(
                    url=gitlab_url,
                    private_token=gitlab_access_token,
                    ssl_verify=ssl_verify
                )
        except Exception as e:
            get_logger().error(f"Failed to create GitLab instance: {e}")
            raise ValueError(f"Unable to authenticate with GitLab: {e}")
        self.max_comment_chars = 65000
        self.id_project = None
        self.id_mr = None
        self.mr = None
        self.diff_files = None
        self.git_files = None
        self.temp_comments = []
        self._submodule_cache: dict[tuple[str, str, str], list[dict]] = {}
        self.pr_url = merge_request_url
        self._set_merge_request(merge_request_url)
        self.RE_HUNK_HEADER = re.compile(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
        self.incremental = incremental

    # --- submodule expansion helpers (opt-in) ---
    def _get_gitmodules_map(self) -> dict[str, str]:
        """
        Return {submodule_path -> repo_url} from '.gitmodules' (best effort).
        Tries target branch first, then source branch. Always returns text.
        """
        try:
            proj = self.gl.projects.get(self.id_project)
        except Exception:
            return {}

        import base64

        def _read_text(ref: str | None) -> str | None:
            if not ref:
                return None
            try:
                f = proj.files.get(file_path=".gitmodules", ref=ref)
            except Exception:
                return None

            # 1) python-gitlab File.decode() – usually returns BYTES
            try:
                raw = f.decode()
                if isinstance(raw, (bytes, bytearray)):
                    return raw.decode("utf-8", "ignore")
                if isinstance(raw, str):
                    return raw
            except Exception:
                pass

            # 2) fallback: base64 decode f.content
            try:
                c = getattr(f, "content", None)
                if c:
                    return base64.b64decode(c).decode("utf-8", "ignore")
            except Exception:
                pass

            return None

        content = (
            _read_text(getattr(self.mr, "target_branch", None))
            or _read_text(getattr(self.mr, "source_branch", None))
        )
        if not content:
            return {}

        import configparser

        parser = configparser.ConfigParser(
            delimiters=("=",),
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            strict=False,
        )
        try:
            parser.read_string(content)
        except Exception:
            return {}

        out: dict[str, str] = {}
        for section in parser.sections():
            if not section.lower().startswith("submodule"):
                continue
            path = parser.get(section, "path", fallback=None)
            url = parser.get(section, "url", fallback=None)
            if path and url:
                path = path.strip().strip('"').strip("'")
                url = url.strip().strip('"').strip("'")
                out[path] = url
        return out

    def _url_to_project_path(self, url: str) -> str | None:
        """
        Convert ssh/https GitLab URL to 'group/subgroup/repo' project path.
        """
        try:
            if url.startswith("git@") and ":" in url:
                path = url.split(":", 1)[1]
            else:
                path = urllib.parse.urlparse(url).path.lstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return path or None
        except Exception:
            return None

    def _project_by_path(self, proj_path: str):
        """
        Resolve a project by path with multiple strategies:
        1) URL-encoded path_with_namespace
        2) Raw path_with_namespace
        3) Search fallback + exact match on path_with_namespace (case-insensitive)
        Returns a project object or None.
        """
        if not proj_path:
            return None

        # 1) Encoded
        try:
            enc = urllib.parse.quote_plus(proj_path)
            return self.gl.projects.get(enc)
        except Exception:
            pass

        # 2) Raw
        try:
            return self.gl.projects.get(proj_path)
        except Exception:
            pass

        # 3) Search fallback
        try:
            name = proj_path.split("/")[-1]
            # membership=True so we don't leak other people's repos
            matches = self.gl.projects.list(search=name, simple=True, membership=True, per_page=100)
            # prefer exact path_with_namespace match (case-insensitive)
            for p in matches:
                pwn = getattr(p, "path_with_namespace", "")
                if pwn.lower() == proj_path.lower():
                    return self.gl.projects.get(p.id)
            if matches:
                get_logger().warning(f"[submodule] no exact match for {proj_path} (skip)")
        except Exception:
            pass

        return None

    def _compare_submodule(self, proj_path: str, old_sha: str, new_sha: str) -> list[dict]:
        """
        Call repository_compare on submodule project; return list of diffs.
        """
        key = (proj_path, old_sha, new_sha)
        if key in self._submodule_cache:
            return self._submodule_cache[key]
        try:
            proj = self._project_by_path(proj_path)
            if proj is None:
                get_logger().warning(f"[submodule] resolve failed for {proj_path}")
                self._submodule_cache[key] = []
                return []
            cmp = proj.repository_compare(old_sha, new_sha)
            if isinstance(cmp, dict):
                diffs = cmp.get("diffs", []) or []
            else:
                diffs = []
            self._submodule_cache[key] = diffs
            return diffs
        except Exception as e:
            get_logger().warning(f"[submodule] compare failed for {proj_path} {old_sha}..{new_sha}: {e}")
            self._submodule_cache[key] = []
            return []

    def _expand_submodule_changes(self, changes: list[dict]) -> list[dict]:
        """
        If enabled, expand 'Subproject commit' bumps into real file diffs from the submodule.
        Soft-fail on any issue.
        """
        try:
            if not bool(get_settings().get("GITLAB.EXPAND_SUBMODULE_DIFFS", False)):
                return changes
        except Exception:
            return changes

        gitmodules = self._get_gitmodules_map()
        if not gitmodules:
            return changes

        out = list(changes)
        for ch in changes:
            patch = ch.get("diff") or ""
            if "Subproject commit" not in patch:
                continue

            # Extract old/new SHAs from the hunk
            old_m = re.search(r"^-Subproject commit ([0-9a-f]{7,40})", patch, re.M)
            new_m = re.search(r"^\+Subproject commit ([0-9a-f]{7,40})", patch, re.M)
            if not (old_m and new_m):
                continue
            old_sha, new_sha = old_m.group(1), new_m.group(1)

            sub_path = ch.get("new_path") or ch.get("old_path") or ""
            repo_url = gitmodules.get(sub_path)
            if not repo_url:
                get_logger().warning(f"[submodule] no url for '{sub_path}' in .gitmodules (skip)")
                continue

            proj_path = self._url_to_project_path(repo_url)
            if not proj_path:
                get_logger().warning(f"[submodule] cannot parse project path from url '{repo_url}' (skip)")
                continue

            get_logger().info(f"[submodule] {sub_path} url={repo_url} -> proj_path={proj_path}")
            sub_diffs = self._compare_submodule(proj_path, old_sha, new_sha)
            for sd in sub_diffs:
                sd_diff = sd.get("diff") or ""
                sd_old = sd.get("old_path") or sd.get("a_path") or ""
                sd_new = sd.get("new_path") or sd.get("b_path") or sd_old
                out.append({
                    "old_path": f"{sub_path}/{sd_old}" if sd_old else sub_path,
                    "new_path": f"{sub_path}/{sd_new}" if sd_new else sub_path,
                    "diff": sd_diff,
                    "new_file": sd.get("new_file", False),
                    "deleted_file": sd.get("deleted_file", False),
                    "renamed_file": sd.get("renamed_file", False),
                })
        return out

    def is_supported(self, capability: str) -> bool:
        if capability in ['get_issue_comments', 'create_inline_comment', 'publish_inline_comments',
            'publish_file_comments']: # gfm_markdown is supported in gitlab !
            return False
        return True

    def _get_project_path_from_pr_or_issue_url(self, pr_or_issue_url: str) -> str:
        repo_project_path = None
        if 'issues' in pr_or_issue_url:
            #replace 'issues' with 'merge_requests', since gitlab provider does not support issue urls, just to get the git repo url:
            pr_or_issue_url = pr_or_issue_url.replace('issues', 'merge_requests')
        if 'merge_requests' in pr_or_issue_url:
            repo_project_path, _ = self._parse_merge_request_url(pr_or_issue_url)
        if not repo_project_path:
            get_logger().error(f"url is not a valid merge requests url: {pr_or_issue_url}")
            return ""
        return repo_project_path

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        provider_url = issues_or_pr_url
        repo_path = self._get_project_path_from_pr_or_issue_url(provider_url)
        if not repo_path or repo_path not in issues_or_pr_url:
            get_logger().error(f"Unable to retrieve project path from url: {issues_or_pr_url}")
            return ""
        return f"{issues_or_pr_url.split(repo_path)[0]}{repo_path}.git"

    # Given a git repo url, return prefix and suffix of the provider in order to view a given file belonging to that repo.
    # Example: https://gitlab.com/pragent/pr-agent.git and branch: t1 -> prefix: "https://gitlab.com/pragent/pr-agent/-/blob/t1", suffix: "?ref_type=heads"
    # In case git url is not provided, provider will use PR context (which includes branch) to determine the prefix and suffix.
    def get_canonical_url_parts(self, repo_git_url:str=None, desired_branch:str=None) -> Tuple[str, str]:
        repo_path = ""
        if not repo_git_url and not self.pr_url:
            get_logger().error("Cannot get canonical URL parts: missing either context PR URL or a repo GIT URL")
            return ("", "")
        if not repo_git_url: #Use PR url as context
            repo_path = self._get_project_path_from_pr_or_issue_url(self.pr_url)
            try:
                desired_branch = self.gl.projects.get(self.id_project).default_branch
            except Exception as e:
                get_logger().exception(f"Cannot get PR: {self.pr_url} default branch. Tried project ID: {self.id_project}")
                return ("", "")
        else: #Use repo git url
            repo_path = repo_git_url.split('.git')[0].split('.com/')[-1]
        prefix = f"{self.gitlab_url}/{repo_path}/-/blob/{desired_branch}"
        suffix = "?ref_type=heads"  # gitlab cloud adds this suffix. gitlab server does not, but it is harmless.
        return (prefix, suffix)

    @property
    def pr(self):
        '''The GitLab terminology is merge request (MR) instead of pull request (PR)'''
        return self.mr

    def _set_merge_request(self, merge_request_url: str):
        self.id_project, self.id_mr = self._parse_merge_request_url(merge_request_url)
        self.mr = self._get_merge_request()
        try:
            self.last_diff = self.mr.diffs.list(get_all=True)[-1]
        except IndexError as e:
            get_logger().error(f"Could not get diff for merge request {self.id_mr}")
            raise DiffNotFoundError(f"Could not get diff for merge request {self.id_mr}") from e

    def get_pr_file_content(self, file_path: str, branch: str) -> str:
        try:
            file_obj = self.gl.projects.get(self.id_project).files.get(file_path, branch)
            content = file_obj.decode()
            return decode_if_bytes(content)
        except GitlabGetError:
            # In case of file creation the method returns GitlabGetError (404 file not found).
            # In this case we return an empty string for the diff.
            return ''
        except Exception as e:
            get_logger().warning(f"Error retrieving file {file_path} from branch {branch}: {e}")
            return ''

    def create_or_update_pr_file(self, file_path: str, branch: str, contents="", message="") -> None:
        """Create or update a file in the GitLab repository."""
        try:
            project = self.gl.projects.get(self.id_project)

            if not message:
                action = "Update" if contents else "Create"
                message = f"{action} {file_path}"

            try:
                existing_file = project.files.get(file_path, branch)
                existing_file.content = contents
                existing_file.save(branch=branch, commit_message=message)
                get_logger().debug(f"Updated file {file_path} in branch {branch}")
            except GitlabGetError:
                project.files.create({
                    'file_path': file_path,
                    'branch': branch,
                    'content': contents,
                    'commit_message': message
                })
                get_logger().debug(f"Created file {file_path} in branch {branch}")
        except GitlabAuthenticationError as e:
            get_logger().error(f"Authentication failed while creating/updating file {file_path} in branch {branch}: {e}")
            raise
        except (GitlabCreateError, GitlabUpdateError) as e:
            get_logger().error(f"Permission denied or validation error for file {file_path} in branch {branch}: {e}")
            raise
        except Exception as e:
            get_logger().exception(f"Unexpected error creating/updating file {file_path} in branch {branch}: {e}")
            raise

    def get_diff_files(self) -> list[FilePatchInfo]:
        """
        Retrieves the list of files that have been modified, added, deleted, or renamed in a pull request in GitLab,
        along with their content and patch information.

        Returns:
            diff_files (List[FilePatchInfo]): List of FilePatchInfo objects representing the modified, added, deleted,
            or renamed files in the merge request.
        """

        if self.diff_files:
            return self.diff_files

        # filter files using [ignore] patterns
        raw_changes = self.mr.changes().get('changes', [])
        raw_changes = self._expand_submodule_changes(raw_changes)
        diffs_original = raw_changes
        diffs = filter_ignored(diffs_original, 'gitlab')
        if diffs != diffs_original:
            try:
                names_original = [diff['new_path'] for diff in diffs_original]
                names_filtered = [diff['new_path'] for diff in diffs]
                get_logger().info(f"Filtered out [ignore] files for merge request {self.id_mr}", extra={
                    'original_files': names_original,
                    'filtered_files': names_filtered
                })
            except Exception as e:
                pass

        diff_files = []
        invalid_files_names = []
        counter_valid = 0
        for diff in diffs:
            if not is_valid_file(diff['new_path']):
                invalid_files_names.append(diff['new_path'])
                continue

            # allow only a limited number of files to be fully loaded. We can manage the rest with diffs only
            counter_valid += 1
            if counter_valid < MAX_FILES_ALLOWED_FULL or not diff['diff']:
                original_file_content_str = self.get_pr_file_content(diff['old_path'], self.mr.diff_refs['base_sha'])
                new_file_content_str = self.get_pr_file_content(diff['new_path'], self.mr.diff_refs['head_sha'])
            else:
                if counter_valid == MAX_FILES_ALLOWED_FULL:
                    get_logger().info(f"Too many files in PR, will avoid loading full content for rest of files")
                original_file_content_str = ''
                new_file_content_str = ''

            # Ensure content is properly decoded
            original_file_content_str = decode_if_bytes(original_file_content_str)
            new_file_content_str = decode_if_bytes(new_file_content_str)

            edit_type = EDIT_TYPE.MODIFIED
            if diff['new_file']:
                edit_type = EDIT_TYPE.ADDED
            elif diff['deleted_file']:
                edit_type = EDIT_TYPE.DELETED
            elif diff['renamed_file']:
                edit_type = EDIT_TYPE.RENAMED

            filename = diff['new_path']
            patch = diff['diff']
            if not patch:
                patch = load_large_diff(filename, new_file_content_str, original_file_content_str)


            # count number of lines added and removed
            patch_lines = patch.splitlines(keepends=True)
            num_plus_lines = len([line for line in patch_lines if line.startswith('+')])
            num_minus_lines = len([line for line in patch_lines if line.startswith('-')])
            diff_files.append(
                FilePatchInfo(original_file_content_str, new_file_content_str,
                              patch=patch,
                              filename=filename,
                              edit_type=edit_type,
                              old_filename=None if diff['old_path'] == diff['new_path'] else diff['old_path'],
                              num_plus_lines=num_plus_lines,
                              num_minus_lines=num_minus_lines, ))
        if invalid_files_names:
            get_logger().info(f"Filtered out files with invalid extensions: {invalid_files_names}")

        self.diff_files = diff_files
        return diff_files

    def get_files(self) -> list:
        if not self.git_files:
            raw_changes = self.mr.changes().get('changes', [])
            raw_changes = self._expand_submodule_changes(raw_changes)
            self.git_files = [c.get('new_path') for c in raw_changes if c.get('new_path')]
        return self.git_files

    def publish_description(self, pr_title: str, pr_body: str):
        try:
            self.mr.title = pr_title
            self.mr.description = pr_body
            self.mr.save()
        except Exception as e:
            get_logger().exception(f"Could not update merge request {self.id_mr} description: {e}")

    def get_latest_commit_url(self):
        try:
            return self.mr.commits().next().web_url
        except StopIteration: # no commits
            return ""
        except Exception as e:
            get_logger().exception(f"Could not get latest commit URL: {e}")
            return ""

    def get_comment_url(self, comment):
        return f"{self.mr.web_url}#note_{comment.id}"

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        self.publish_persistent_comment_full(pr_comment, initial_header, update_header, name, final_update_message)

    def publish_comment(self, mr_comment: str, is_temporary: bool = False):
        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment: {mr_comment}")
            return None
        mr_comment = self.limit_output_characters(mr_comment, self.max_comment_chars)
        comment = self.mr.notes.create({'body': mr_comment})
        if is_temporary:
            self.temp_comments.append(comment)
        return comment

    def edit_comment(self, comment, body: str):
        body = self.limit_output_characters(body, self.max_comment_chars)
        self.mr.notes.update(comment.id,{'body': body} )

    def edit_comment_from_comment_id(self, comment_id: int, body: str):
        body = self.limit_output_characters(body, self.max_comment_chars)
        comment = self.mr.notes.get(comment_id)
        comment.body = body
        comment.save()

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        body = self.limit_output_characters(body, self.max_comment_chars)
        discussion = self.mr.discussions.get(comment_id)
        discussion.notes.create({'body': body})

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        body = self.limit_output_characters(body, self.max_comment_chars)
        edit_type, found, source_line_no, target_file, target_line_no = self.search_line(relevant_file,
                                                                                         relevant_line_in_file)
        self.send_inline_comment(body, edit_type, found, relevant_file, relevant_line_in_file, source_line_no,
                                 target_file, target_line_no, original_suggestion)

    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, absolute_position: int = None):
        raise NotImplementedError("Gitlab provider does not support creating inline comments yet")

    def create_inline_comments(self, comments: list[dict]):
        raise NotImplementedError("Gitlab provider does not support publishing inline comments yet")

    def get_comment_body_from_comment_id(self, comment_id: int):
        comment = self.mr.notes.get(comment_id).body
        return comment

    def send_inline_comment(self, body: str, edit_type: str, found: bool, relevant_file: str,
                            relevant_line_in_file: str,
                            source_line_no: int, target_file: str, target_line_no: int,
                            original_suggestion=None) -> None:
        if not found:
            get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
        else:
            # in order to have exact sha's we have to find correct diff for this change
            diff = self.get_relevant_diff(relevant_file, relevant_line_in_file)
            if diff is None:
                get_logger().error(f"Could not get diff for merge request {self.id_mr}")
                raise DiffNotFoundError(f"Could not get diff for merge request {self.id_mr}")
            pos_obj = {'position_type': 'text',
                       'new_path': target_file.filename,
                       'old_path': target_file.old_filename if target_file.old_filename else target_file.filename,
                       'base_sha': diff.base_commit_sha, 'start_sha': diff.start_commit_sha, 'head_sha': diff.head_commit_sha}
            if edit_type == 'deletion':
                pos_obj['old_line'] = source_line_no - 1
            elif edit_type == 'addition':
                pos_obj['new_line'] = target_line_no - 1
            else:
                pos_obj['new_line'] = target_line_no - 1
                pos_obj['old_line'] = source_line_no - 1
            get_logger().debug(f"Creating comment in MR {self.id_mr} with body {body} and position {pos_obj}")
            try:
                self.mr.discussions.create({'body': body, 'position': pos_obj})
            except Exception as e:
                try:
                    # fallback - create a general note on the file in the MR
                    if 'suggestion_orig_location' in original_suggestion:
                        line_start = original_suggestion['suggestion_orig_location']['start_line']
                        line_end = original_suggestion['suggestion_orig_location']['end_line']
                        old_code_snippet = original_suggestion['prev_code_snippet']
                        new_code_snippet = original_suggestion['new_code_snippet']
                        content = original_suggestion['suggestion_summary']
                        label = original_suggestion['category']
                        if 'score' in original_suggestion:
                            score = original_suggestion['score']
                        else:
                            score = 7
                    else:
                        line_start = original_suggestion['relevant_lines_start']
                        line_end = original_suggestion['relevant_lines_end']
                        old_code_snippet = original_suggestion['existing_code']
                        new_code_snippet = original_suggestion['improved_code']
                        content = original_suggestion['suggestion_content']
                        label = original_suggestion['label']
                        score = original_suggestion.get('score', 7)

                    if hasattr(self, 'main_language'):
                        language = self.main_language
                    else:
                        language = ''
                    link = self.get_line_link(relevant_file, line_start, line_end)
                    body_fallback =f"**Suggestion:** {content} [{label}, importance: {score}]\n\n"
                    body_fallback +=f"\n\n<details><summary>[{target_file.filename} [{line_start}-{line_end}]]({link}):</summary>\n\n"
                    body_fallback += f"\n\n___\n\n`(Cannot implement directly - GitLab API allows committable suggestions strictly on MR diff lines)`"
                    body_fallback+="</details>\n\n"
                    diff_patch = difflib.unified_diff(old_code_snippet.split('\n'),
                                                new_code_snippet.split('\n'), n=999)
                    patch_orig = "\n".join(diff_patch)
                    patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')
                    diff_code = f"\n\n```diff\n{patch.rstrip()}\n```"
                    body_fallback += diff_code

                    # Create a general note on the file in the MR
                    self.mr.notes.create({
                        'body': body_fallback,
                        'position': {
                            'base_sha': diff.base_commit_sha,
                            'start_sha': diff.start_commit_sha,
                            'head_sha': diff.head_commit_sha,
                            'position_type': 'text',
                            'file_path': f'{target_file.filename}',
                        }
                    })
                    get_logger().debug(f"Created fallback comment in MR {self.id_mr} with position {pos_obj}")

                    # get_logger().debug(
                    #     f"Failed to create comment in MR {self.id_mr} with position {pos_obj} (probably not a '+' line)")
                except Exception as e:
                    get_logger().exception(f"Failed to create comment in MR {self.id_mr}")

    def get_relevant_diff(self, relevant_file: str, relevant_line_in_file: str) -> Optional[dict]:
        _changes = self.mr.changes()  # dict
        _changes['changes'] = self._expand_submodule_changes(_changes.get('changes', []))
        changes = _changes
        if not changes:
            get_logger().error('No changes found for the merge request.')
            return None
        all_diffs = self.mr.diffs.list(get_all=True)
        if not all_diffs:
            get_logger().error('No diffs found for the merge request.')
            return None
        for diff in all_diffs:
            for change in changes['changes']:
                if change['new_path'] == relevant_file and relevant_line_in_file in change['diff']:
                    return diff
            get_logger().debug(
                f'No relevant diff found for {relevant_file} {relevant_line_in_file}. Falling back to last diff.')
        return self.last_diff  # fallback to last_diff if no relevant diff is found

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        for suggestion in code_suggestions:
            try:
                if suggestion and 'original_suggestion' in suggestion:
                    original_suggestion = suggestion['original_suggestion']
                else:
                    original_suggestion = suggestion
                body = suggestion['body']
                relevant_file = suggestion['relevant_file']
                relevant_lines_start = suggestion['relevant_lines_start']
                relevant_lines_end = suggestion['relevant_lines_end']

                diff_files = self.get_diff_files()
                target_file = None
                for file in diff_files:
                    if file.filename == relevant_file:
                        if file.filename == relevant_file:
                            target_file = file
                            break
                range = relevant_lines_end - relevant_lines_start # no need to add 1
                body = body.replace('```suggestion', f'```suggestion:-0+{range}')
                lines = target_file.head_file.splitlines()
                relevant_line_in_file = lines[relevant_lines_start - 1]

                # edit_type, found, source_line_no, target_file, target_line_no = self.find_in_file(target_file,
                #                                                                            relevant_line_in_file)
                # for code suggestions, we want to edit the new code
                source_line_no = -1
                target_line_no = relevant_lines_start + 1
                found = True
                edit_type = 'addition'

                self.send_inline_comment(body, edit_type, found, relevant_file, relevant_line_in_file, source_line_no,
                                         target_file, target_line_no, original_suggestion)
            except Exception as e:
                get_logger().exception(f"Could not publish code suggestion:\nsuggestion: {suggestion}\nerror: {e}")

        # note that we publish suggestions one-by-one. so, if one fails, the rest will still be published
        return True

    def publish_file_comments(self, file_comments: list) -> bool:
        pass

    def search_line(self, relevant_file, relevant_line_in_file):
        target_file = None

        edit_type = self.get_edit_type(relevant_line_in_file)
        for file in self.get_diff_files():
            if file.filename == relevant_file:
                edit_type, found, source_line_no, target_file, target_line_no = self.find_in_file(file,
                                                                                                  relevant_line_in_file)
        return edit_type, found, source_line_no, target_file, target_line_no

    def find_in_file(self, file, relevant_line_in_file):
        edit_type = 'context'
        source_line_no = 0
        target_line_no = 0
        found = False
        target_file = file
        patch = file.patch
        patch_lines = patch.splitlines()
        for line in patch_lines:
            if line.startswith('@@'):
                match = self.RE_HUNK_HEADER.match(line)
                if not match:
                    continue
                start_old, size_old, start_new, size_new, _ = match.groups()
                source_line_no = int(start_old)
                target_line_no = int(start_new)
                continue
            if line.startswith('-'):
                source_line_no += 1
            elif line.startswith('+'):
                target_line_no += 1
            elif line.startswith(' '):
                source_line_no += 1
                target_line_no += 1
            if relevant_line_in_file in line:
                found = True
                edit_type = self.get_edit_type(line)
                break
            elif relevant_line_in_file[0] == '+' and relevant_line_in_file[1:].lstrip() in line:
                # The model often adds a '+' to the beginning of the relevant_line_in_file even if originally
                # it's a context line
                found = True
                edit_type = self.get_edit_type(line)
                break
        return edit_type, found, source_line_no, target_file, target_line_no

    def get_edit_type(self, relevant_line_in_file):
        edit_type = 'context'
        if relevant_line_in_file[0] == '-':
            edit_type = 'deletion'
        elif relevant_line_in_file[0] == '+':
            edit_type = 'addition'
        return edit_type

    def remove_initial_comment(self):
        try:
            for comment in self.temp_comments:
                self.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def remove_comment(self, comment):
        try:
            comment.delete()
        except Exception as e:
            get_logger().exception(f"Failed to remove comment, error: {e}")

    def get_title(self):
        return self.mr.title

    def get_languages(self):
        languages = self.gl.projects.get(self.id_project).languages()
        return languages

    def get_pr_branch(self):
        return self.mr.source_branch

    def get_pr_owner_id(self) -> str | None:
        if not self.gitlab_url or 'gitlab.com' in self.gitlab_url:
            if not self.id_project:
                return None
            return self.id_project.split('/')[0]
        # extract host name
        host = urlparse(self.gitlab_url).hostname
        return host

    def get_pr_description_full(self):
        return self.mr.description

    def get_issue_comments(self):
        return self.mr.notes.list(get_all=True)[::-1]

    def get_repo_settings(self):
        try:
            main_branch = self.gl.projects.get(self.id_project).default_branch
            contents = self.gl.projects.get(self.id_project).files.get(file_path='.pr_agent.toml', ref=main_branch).decode()
            return contents
        except Exception:
            return ""

    def get_workspace_name(self):
        return self.id_project.split('/')[0]

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        if disable_eyes:
            return None
        try:
            if not self.id_mr:
                get_logger().warning("Cannot add eyes reaction: merge request ID is not set.")
                return None

            mr = self.gl.projects.get(self.id_project).mergerequests.get(self.id_mr)
            comment = mr.notes.get(issue_comment_id)

            if not comment:
                get_logger().warning(f"Comment with ID {issue_comment_id} not found in merge request {self.id_mr}.")
                return None

            award_emoji = comment.awardemojis.create({
                'name': 'eyes'
            })
            return award_emoji.id
        except Exception as e:
            get_logger().warning(f"Failed to add eyes reaction, error: {e}")
            return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: str) -> bool:
        try:
            if not self.id_mr:
                get_logger().warning("Cannot remove reaction: merge request ID is not set.")
                return False

            mr = self.gl.projects.get(self.id_project).mergerequests.get(self.id_mr)
            comment = mr.notes.get(issue_comment_id)

            if not comment:
                get_logger().warning(f"Comment with ID {issue_comment_id} not found in merge request {self.id_mr}.")
                return False

            reactions = comment.awardemojis.list()
            for reaction in reactions:
                if reaction.name == reaction_id:
                    reaction.delete()
                    return True

            get_logger().warning(f"Reaction '{reaction_id}' not found in comment {issue_comment_id}.")
            return False
        except Exception as e:
            get_logger().warning(f"Failed to remove reaction, error: {e}")
            return False

    def _parse_merge_request_url(self, merge_request_url: str) -> Tuple[str, int]:
        parsed_url = urlparse(merge_request_url)

        path_parts = parsed_url.path.strip('/').split('/')
        if 'merge_requests' not in path_parts:
            raise ValueError("The provided URL does not appear to be a GitLab merge request URL")

        mr_index = path_parts.index('merge_requests')
        # Ensure there is an ID after 'merge_requests'
        if len(path_parts) <= mr_index + 1:
            raise ValueError("The provided URL does not contain a merge request ID")

        try:
            mr_id = int(path_parts[mr_index + 1])
        except ValueError as e:
            raise ValueError("Unable to convert merge request ID to integer") from e

        # Handle special delimiter (-)
        project_path = "/".join(path_parts[:mr_index])
        if project_path.endswith('/-'):
            project_path = project_path[:-2]

        # Return the path before 'merge_requests' and the ID
        return project_path, mr_id

    def _get_merge_request(self):
        mr = self.gl.projects.get(self.id_project).mergerequests.get(self.id_mr)
        return mr

    def get_user_id(self):
        return None

    def publish_labels(self, pr_types):
        try:
            self.mr.labels = list(set(pr_types))
            self.mr.save()
        except Exception as e:
            get_logger().warning(f"Failed to publish labels, error: {e}")

    def publish_inline_comments(self, comments: list[dict]):
        pass

    def get_pr_labels(self, update=False):
        return self.mr.labels

    def get_repo_labels(self):
        return self.gl.projects.get(self.id_project).labels.list()

    def get_commit_messages(self):
        """
        Retrieves the commit messages of a pull request.

        Returns:
            str: A string containing the commit messages of the pull request.
        """
        max_tokens = get_settings().get("CONFIG.MAX_COMMITS_TOKENS", None)
        try:
            commit_messages_list = [commit['message'] for commit in self.mr.commits()._list]
            commit_messages_str = "\n".join([f"{i + 1}. {message}" for i, message in enumerate(commit_messages_list)])
        except Exception:
            commit_messages_str = ""
        if max_tokens:
            commit_messages_str = clip_tokens(commit_messages_str, max_tokens)
        return commit_messages_str

    def get_pr_id(self):
        try:
            pr_id = self.mr.web_url
            return pr_id
        except:
            return ""

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        if relevant_line_start == -1:
            link = f"{self.gl.url}/{self.id_project}/-/blob/{self.mr.source_branch}/{relevant_file}?ref_type=heads"
        elif relevant_line_end:
            link = f"{self.gl.url}/{self.id_project}/-/blob/{self.mr.source_branch}/{relevant_file}?ref_type=heads#L{relevant_line_start}-{relevant_line_end}"
        else:
            link = f"{self.gl.url}/{self.id_project}/-/blob/{self.mr.source_branch}/{relevant_file}?ref_type=heads#L{relevant_line_start}"
        return link


    def generate_link_to_relevant_line_number(self, suggestion) -> str:
        try:
            relevant_file = suggestion['relevant_file'].strip('`').strip("'").rstrip()
            relevant_line_str = suggestion['relevant_line'].rstrip()
            if not relevant_line_str:
                return ""

            position, absolute_position = find_line_number_of_relevant_line_in_file \
                (self.diff_files, relevant_file, relevant_line_str)

            if absolute_position != -1:
                # link to right file only
                link = f"{self.gl.url}/{self.id_project}/-/blob/{self.mr.source_branch}/{relevant_file}?ref_type=heads#L{absolute_position}"

                # # link to diff
                # sha_file = hashlib.sha1(relevant_file.encode('utf-8')).hexdigest()
                # link = f"{self.pr.web_url}/diffs#{sha_file}_{absolute_position}_{absolute_position}"
                return link
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Failed adding line link, error: {e}")

        return ""
    #Clone related
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        if "gitlab." not in repo_url_to_clone:
            get_logger().error(f"Repo URL: {repo_url_to_clone} is not a valid gitlab URL.")
            return None
        (scheme, base_url) = repo_url_to_clone.split("gitlab.")
        access_token = getattr(self.gl, 'oauth_token', None) or getattr(self.gl, 'private_token', None)
        if not all([scheme, access_token, base_url]):
            get_logger().error(f"Either no access token found, or repo URL: {repo_url_to_clone} "
                               f"is missing prefix: {scheme} and/or base URL: {base_url}.")
            return None

        #Note that the ""official"" method found here:
        # https://docs.gitlab.com/user/profile/personal_access_tokens/#clone-repository-using-personal-access-token
        # requires a username, which may not be applicable.
        # The following solution is taken from: https://stackoverflow.com/questions/25409700/using-gitlab-token-to-clone-without-authentication/35003812#35003812
        # For example: For repo url: https://gitlab.codium-inc.com/qodo/autoscraper.git
        # Then to clone one will issue: 'git clone https://oauth2:<access token>@gitlab.codium-inc.com/qodo/autoscraper.git'

        clone_url = f"{scheme}oauth2:{access_token}@gitlab.{base_url}"
        return clone_url
