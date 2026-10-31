import ast
import difflib
import json
import math
import re
from types import SimpleNamespace
from urllib.parse import quote, urlparse

import requests
from atlassian.bitbucket import Cloud
from starlette_context import context

from pr_agent.agent.request_policy import policy_metadata, policy_value

from ..algo.file_filter import filter_ignored
from ..algo.git_patch_processing import count_patch_lines
from ..algo.language_handler import is_valid_file
from ..algo.types import EDIT_TYPE, FilePatchInfo
from ..algo.utils import find_line_number_of_relevant_line_in_file, replace_suggestion_blocks
from ..config_loader import get_settings, get_verbosity_level
from ..log import get_logger
from .diff_parsing import to_hunk_only_patch
from .git_provider import (
    MAX_FILES_ALLOWED_FULL,
    FileContentSnapshot,
    GitProvider,
    IncompleteBitbucketPullRequestFilesError,
    cache_languages,
    redact_credentials,
)
from .request_timeout import get_http_request_timeout


def _get_identity_request_timeout() -> float:
    timeout = get_settings().get("bitbucket.identity_request_timeout")
    if isinstance(timeout, bool):
        raise ValueError("bitbucket.identity_request_timeout must be a positive finite number")
    try:
        timeout = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("bitbucket.identity_request_timeout must be a positive finite number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("bitbucket.identity_request_timeout must be a positive finite number")
    return timeout


def _gef_filename(diff):
    if diff.new.path:
        return diff.new.path
    return diff.old.path


def _split_raw_diff(raw_diff: str) -> list[str]:
    return [part for part in re.split(r"(?m)(?=^diff --git )", raw_diff) if part.startswith("diff --git ")]


_DIFF_HEADER_RE = re.compile(r'^diff --git ("(?:\\.|[^"\\])*"|\S+) ("(?:\\.|[^"\\])*"|\S+)$')


def _decode_git_path(token: str) -> str | None:
    """Decode a path taken from a diff header, undoing Git's C-style quoting."""
    try:
        path = ast.literal_eval(token) if token.startswith('"') else token
        if token.startswith('"') and re.search(r"\\[0-7]{3}", token):
            path = path.encode("latin-1").decode("utf-8")
    except (SyntaxError, ValueError, UnicodeError):
        return None
    return path


def _strip_diff_prefix(path: str) -> str:
    """Drop the ``a/`` or ``b/`` prefix Git puts in front of a path in a file header."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _paths_from_patch_headers(patch: str) -> tuple[str, str] | None:
    """Read the old and new paths from the rename and file headers of a patch.

    A ``diff --git`` header is ambiguous when a path contains a space, but the
    ``rename from``/``rename to`` and ``---``/``+++`` headers are not. Only the
    lines before the first hunk are inspected, so a removed line that looks like
    a header is not mistaken for one.
    """
    rename_old = rename_new = None
    file_old = file_new = None
    for line in patch.split("\n"):
        if line.startswith("@@"):
            break
        if line.startswith("rename from "):
            rename_old = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            rename_new = line[len("rename to "):].strip()
        elif line.startswith("--- "):
            file_old = line[len("--- "):].split("\t", 1)[0]
        elif line.startswith("+++ "):
            file_new = line[len("+++ "):].split("\t", 1)[0]

    if rename_old is not None and rename_new is not None:
        old = _decode_git_path(rename_old)
        new = _decode_git_path(rename_new)
        if old is None or new is None:
            return None
        return old, new

    if file_old is None or file_new is None:
        return None
    old = _decode_git_path(file_old)
    new = _decode_git_path(file_new)
    if old is None or new is None:
        return None
    if old == "/dev/null":
        old = new
    elif new == "/dev/null":
        new = old
    return _strip_diff_prefix(old), _strip_diff_prefix(new)


def _diff_header_paths(patch: str) -> tuple[str, str] | None:
    """Read the old and new paths from a Git diff header, including quoted paths."""
    header = patch.split("\n", 1)[0].rstrip("\r")
    match = _DIFF_HEADER_RE.fullmatch(header)
    if not match:
        # Git leaves paths with spaces unquoted, so the header cannot be split reliably.
        # The rename and file headers of the same patch carry the paths unambiguously.
        paths = _paths_from_patch_headers(patch)
        if paths is not None:
            return paths
        # With an unchanged name the header splits at its midpoint.
        rest = header[len("diff --git "):]
        mid = len(rest) // 2
        if rest.startswith("a/") and rest[mid:mid + 3] == " b/" and rest[2:mid] == rest[mid + 3:]:
            return rest[2:mid], rest[mid + 3:]
        return None
    paths = []
    for token in match.groups():
        path = _decode_git_path(token)
        if path is None or not path.startswith(("a/", "b/")):
            return None
        paths.append(path[2:])
    return paths[0], paths[1]


def _diffstat_paths(diff) -> tuple[str, str]:
    old_path = diff.old.path or diff.new.path
    new_path = diff.new.path or diff.old.path
    return old_path, new_path


def _diffstat_line_count(diff, field: str) -> int | None:
    """Return Bitbucket's own count for one diffstat ``field``, or None when it is unusable.

    ``None`` is returned per field rather than per file so the caller can fall back to
    counting the patch for that side alone, instead of reporting an unrelated side as zero.
    """
    data = getattr(diff, "data", None) or {}
    value = data.get(field)
    if value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        get_logger().warning(
            f"Bitbucket diffstat reported a non-numeric {field} for file {_gef_filename(diff)}"
        )
        return None
    if count < 0 or isinstance(value, bool) or (not isinstance(value, str) and count != value):
        get_logger().warning(
            f"Bitbucket diffstat reported an invalid {field} for file {_gef_filename(diff)}"
        )
        return None
    return count


class BitbucketProvider(GitProvider):
    def get_request_policy_metadata(self, required_fields: set[str]) -> dict:
        pr = self.pr.data
        return policy_metadata(title=policy_value(pr, "title"),
                               sender=(policy_value(pr, "author", "nickname")
                                       or policy_value(pr, "author", "display_name")),
                               repo_full_name=policy_value(pr, "destination", "repository", "full_name"),
                               source_branch=policy_value(pr, "source", "branch", "name"),
                               target_branch=policy_value(pr, "destination", "branch", "name"))

    def __init__(
        self, pr_url: str | None = None, incremental: bool | None = False
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
                except Exception:
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
        self.max_comment_chars = self.max_comment_length
        self.workspace_slug = None
        self.repo_slug = None
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.pr_url = pr_url
        self.temp_comments = []
        self._published_inline_comment_bodies = []
        self.incremental = incremental
        self.diff_files = None
        self.git_files = None
        self._pending_diffstat = None
        if pr_url:
            self.set_pr(pr_url)
        self.bitbucket_comment_api_url = self.pr._BitbucketBase__data["links"]["comments"]["href"]
        self.bitbucket_pull_request_api_url = self.pr._BitbucketBase__data["links"]['self']['href']

    def get_repo_settings(self):
        settings_files = []
        global_settings = self._get_global_repo_settings()
        if global_settings:
            settings_files.append(("global", global_settings))
        try:
            destination_commit = self.pr.data["destination"]["commit"]["hash"]
            url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/"
                   f"{destination_commit}/.pr_agent.toml")
            response = requests.request("GET", url, headers=self.headers, timeout=get_http_request_timeout())
            if response.status_code == 200:  # found
                settings_files.append(("local", response.text.encode('utf-8')))
            elif response.status_code != 404:
                # Don't treat error bodies (401/403/500/...) as TOML content.
                get_logger().warning(f"Failed to load local .pr_agent.toml file, status: {response.status_code}")
        except Exception as e:
            get_logger().warning(f"Failed to load local .pr_agent.toml file, error: {e}")
        return settings_files or ""

    def _get_global_settings_cache_key(self, workspace: str) -> str:
        return f"bitbucket:{workspace}"

    def _fetch_global_repo_settings(self, workspace, settings_repo):
        # A missing settings repo/file (404) is an expected fallback -> return "" (cached). Other
        # errors raise (via raise_for_status) so the caller does not cache a transient failure.
        repo_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{settings_repo}"
        repo_resp = requests.request("GET", repo_url, headers=self.headers, timeout=get_http_request_timeout())
        if repo_resp.status_code in (403, 404):  # missing repo or no access -> expected, cacheable
            return ""
        repo_resp.raise_for_status()
        main_branch = (repo_resp.json().get('mainbranch') or {}).get('name')
        if not main_branch:
            return ""
        ref_resp = requests.request(
            "GET", f"{repo_url}/refs/branches/{quote(main_branch, safe='')}", headers=self.headers,
            timeout=get_http_request_timeout())
        if ref_resp.status_code in (403, 404):  # missing branch or no access -> expected, cacheable
            return ""
        ref_resp.raise_for_status()
        main_branch_hash = ((ref_resp.json().get('target') or {}).get('hash'))
        if not main_branch_hash:
            raise ValueError("Bitbucket default branch response did not include a target hash")
        file_resp = requests.request(
            "GET", f"{repo_url}/src/{main_branch_hash}/.pr_agent.toml", headers=self.headers,
            timeout=get_http_request_timeout())
        if file_resp.status_code in (403, 404):  # missing file or no access -> expected, cacheable
            return ""
        file_resp.raise_for_status()
        return file_resp.text.encode('utf-8')

    def get_repo_file_content(self, file_path: str, from_default_branch: bool = False):
        # Read from the PR destination (target) branch, matching the other providers,
        # or from the repository default branch when from_default_branch is requested.
        branch = self.get_repo_context_ref(from_default_branch)
        return self.get_pr_file_content(file_path, branch, propagate_errors=True)

    def get_repo_context_ref(self, from_default_branch: bool = False) -> str | None:
        return self.get_repo_default_branch() if from_default_branch else self.pr.destination_branch

    def get_git_repo_url(self, pr_url: str=None) -> str: #bitbucket does not support issue url, so ignore param
        try:
            parsed_url = urlparse(self.pr_url)
            return f"{parsed_url.scheme}://{parsed_url.netloc}/{self.workspace_slug}/{self.repo_slug}.git"
        except Exception:
            get_logger().exception(f"url is not a valid merge requests url: {self.pr_url}")
            return ""

    # Given a git repo url, return prefix and suffix of the provider in order to view a given
    # file belonging to that repo.
    # Example: git clone https://bitbucket.org/pragent/pr-agent.git and branch: main -> prefix:
    # "https://bitbucket.org/pragent/pr-agent/src/main", suffix: ""
    # In case git url is not provided, provider will use PR context (which includes branch) to
    # determine the prefix and suffix.
    def get_canonical_url_parts(self, repo_git_url:str=None, desired_branch:str=None) -> tuple[str, str]:
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
        prefix = f"{scheme_and_netloc}/{workspace_name}/{project_name}/src/{quote(desired_branch)}"
        suffix = "" #None
        return (prefix, suffix)


    def _prepare_code_suggestion(self, suggestion: dict) -> dict | None:
        body = suggestion["body"]
        original_suggestion = suggestion.get('original_suggestion', None)  # needed for diff code
        if original_suggestion:
            try:
                existing_code = original_suggestion['existing_code'].rstrip() + "\n"
                improved_code = original_suggestion['improved_code'].rstrip() + "\n"
                diff = difflib.unified_diff(existing_code.split('\n'), improved_code.split('\n'), n=999)
                patch_orig = "\n".join(diff)
                patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')
                diff_code = f"\n\n```diff\n{patch.rstrip()}\n```"
                body = replace_suggestion_blocks(body, diff_code)
            except Exception as e:
                get_logger().exception(f"Bitbucket failed to get diff code for publishing, error: {e}")
                return None
            return {**suggestion, "body": body}
        return suggestion

    def _build_code_suggestion_payload(self, suggestion: dict) -> dict:
        body = suggestion["body"]
        relevant_lines_start = suggestion["relevant_lines_start"]
        relevant_lines_end = suggestion["relevant_lines_end"]
        if relevant_lines_end > relevant_lines_start:
            return {
                "body": body,
                "path": suggestion["relevant_file"],
                "line": relevant_lines_end,
                "start_line": relevant_lines_start,
                "start_side": "RIGHT",
            }
        return {
            "body": body,
            "path": suggestion["relevant_file"],
            "line": relevant_lines_start,
            "side": "RIGHT",
        }

    def _log_code_suggestion_publish_error(self, error: Exception) -> None:
        get_logger().error(f"Bitbucket failed to publish code suggestion, error: {error}")

    def is_supported(self, capability: str) -> bool:
        if capability in ['publish_inline_comments', 'get_labels', 'gfm_markdown']:
            return False
        if capability == "push_code":
            return not get_settings().config.restricted_mode and self._pr_head_in_base_repo()
        return True

    def _pr_head_in_base_repo(self) -> bool:
        """Check that the source branch belongs to the repository used by the write API."""
        data = getattr(self.pr, "data", None)
        if not isinstance(data, dict):
            return False
        source = data.get("source")
        destination = data.get("destination")
        if not isinstance(source, dict) or not isinstance(destination, dict):
            return False
        source_repo = source.get("repository")
        destination_repo = destination.get("repository")
        if not isinstance(source_repo, dict) or not isinstance(destination_repo, dict):
            return False

        source_uuid = source_repo.get("uuid")
        destination_uuid = destination_repo.get("uuid")
        if isinstance(source_uuid, str) and source_uuid and isinstance(destination_uuid, str) and destination_uuid:
            return source_uuid.casefold() == destination_uuid.casefold()
        source_name = source_repo.get("full_name")
        destination_name = destination_repo.get("full_name")
        return (isinstance(source_name, str) and bool(source_name)
                and isinstance(destination_name, str) and bool(destination_name)
                and source_name.casefold() == destination_name.casefold())

    def set_pr(self, pr_url: str):
        self.workspace_slug, self.repo_slug, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def _collect_diffstat(self):
        diffs = []
        git_files = []
        for diff in self.pr.diffstat():
            diffs.append(diff)
            git_files.append(_gef_filename(diff))
        return diffs, git_files

    def get_files(self):
        try:
            git_files = context.get("git_files", None)
            if git_files:
                return git_files
            diffs, git_files = self._collect_diffstat()
            self.git_files = git_files
            self._pending_diffstat = diffs or None
            context["git_files"] = self.git_files
            return self.git_files
        except Exception:
            if not self.git_files:
                diffs, git_files = self._collect_diffstat()
                self.git_files = git_files
                self._pending_diffstat = diffs or None
            return self.git_files

    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            self._pending_diffstat = None
            return self.diff_files

        diffs_original = getattr(self, "_pending_diffstat", None)
        self._pending_diffstat = None
        if diffs_original is None:
            diffs_original = list(self.pr.diffstat())
        diffs = filter_ignored(diffs_original, 'bitbucket')
        if diffs != diffs_original:
            try:
                names_original = [d.new.path for d in diffs_original]
                names_kept = [d.new.path for d in diffs]
                names_filtered = list(set(names_original) - set(names_kept))
                get_logger().info("Filtered out [ignore] files for PR", extra={
                    'original_files': names_original,
                    'names_kept': names_kept,
                    'names_filtered': names_filtered

                })
            except Exception:
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
                raise ValueError(f"Failed to decode PR patch with encodings {encodings_to_try}") from e

        diff_split = _split_raw_diff(pr_patches)
        patches_by_path = {}
        for patch in diff_split:
            paths = _diff_header_paths(patch)
            if paths is None or paths in patches_by_path:
                raise IncompleteBitbucketPullRequestFilesError(
                    "Bitbucket aggregate diff contains an invalid or duplicate file header"
                )
            patches_by_path[paths] = patch

        original_paths = {_diffstat_paths(diff) for diff in diffs_original}
        kept_paths = [_diffstat_paths(diff) for diff in diffs]
        if (len(original_paths) != len(diffs_original) or len(set(kept_paths)) != len(kept_paths)
                or not patches_by_path.keys() <= original_paths
                or not set(kept_paths) <= patches_by_path.keys()):
            get_logger().error(f"Error - failed to split the diff into {len(diffs)} parts")
            raise IncompleteBitbucketPullRequestFilesError(
                "Bitbucket aggregate diff does not match its changed-file inventory"
            )
        diff_split = [patches_by_path[paths] for paths in kept_paths]
        # Bitbucket headers vary by change type and may include mode or rename
        # metadata. Keep only the unified-diff hunks consumed downstream.
        for i, patch in enumerate(diff_split):
            diff_split[i] = to_hunk_only_patch(patch)
            if diff_split[i]:
                continue

            if diffs[i].data.get('lines_added', 0) == 0 and diffs[i].data.get('lines_removed', 0) == 0:
                continue

            if len(patch.splitlines()) <= 3:
                get_logger().info(f"Disregarding empty diff for file {_gef_filename(diffs[i])}")
            else:
                get_logger().warning(f"Bitbucket failed to get diff for file {_gef_filename(diffs[i])}")

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
                            "Bitbucket too many files in PR, will avoid loading full content for rest of files")
                    original_file_content_str = ""
                    new_file_content_str = ""
            except Exception as e:
                get_logger().exception(f"Error - bitbucket failed to get file content, error: {e}")
                original_file_content_str = ""
                new_file_content_str = ""

            # Bitbucket's diffstat carries the authoritative per-file counts, so prefer it over
            # counting the patch, field by field. The raw diff can carry no textual hunk, or a
            # truncated one, even when the diffstat reports real additions and removals, and a
            # partially populated diffstat still carries the side it does report.
            lines_added = _diffstat_line_count(diff, "lines_added")
            lines_removed = _diffstat_line_count(diff, "lines_removed")
            if lines_added is None or lines_removed is None:
                patch_added, patch_removed = count_patch_lines(diff_split[index])
                if lines_added is None:
                    lines_added = patch_added
                if lines_removed is None:
                    lines_removed = patch_removed

            file_patch_canonic_structure = FilePatchInfo(
                original_file_content_str,
                new_file_content_str,
                diff_split[index],
                file_path,
                num_plus_lines=lines_added,
                num_minus_lines=lines_removed,
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

        self.filtered_diff_file_names = invalid_files_names
        self.diff_files = diff_files
        return diff_files

    def get_latest_commit_url(self):
        return self.pr.data['source']['commit']['links']['html']['href']

    def get_pr_head_sha(self) -> str:
        try:
            head_sha = self.pr.data['source']['commit']['hash']
            return head_sha if isinstance(head_sha, str) else ""
        except (KeyError, AttributeError, TypeError) as e:
            get_logger().warning(f"Failed to get head SHA, error: {e}")
            return ""

    def _get_cloud_comment(self, comment):
        if isinstance(comment, SimpleNamespace):
            return comment._cloud_comment
        return comment

    def get_comment_url(self, comment):
        comment = self._get_cloud_comment(comment)
        return comment.data["links"]["html"]["href"]

    def supports_html_comment_markers(self) -> bool:
        return False

    def supports_review_comment_identity(self) -> bool:
        return True

    def edit_comment(self, comment, body: str):
        try:
            comment = self._get_cloud_comment(comment)
            body = self.limit_output_characters(body, self.max_comment_length)
            if isinstance(comment, dict):
                # publish_comment returns the raw API payload rather than a client object, so
                # dict.update would only rewrite the local copy and never reach Bitbucket.
                self.pr.put(f"comments/{comment['id']}", data={"content": {"raw": body}})
            else:
                comment.update(content={"raw": body})
            return True
        except Exception as e:
            get_logger().exception(f"Failed to update comment, error: {e}")
            return False

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if is_temporary and not get_settings().config.publish_output_progress:
            get_logger().debug(f"Skipping publish_comment for temporary comment: {pr_comment}")
            return None
        pr_comment = self.limit_output_characters(pr_comment, self.max_comment_length)
        comment = self.pr.comment(pr_comment)
        if is_temporary:
            self.temp_comments.append(comment["id"])
        return comment

    def remove_initial_comment(self):
        try:
            for comment in self.temp_comments:
                self.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def remove_comment(self, comment):
        try:
            comment_id = comment["id"] if isinstance(comment, dict) else comment
            self.pr.delete(f"comments/{comment_id}")
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
            if get_verbosity_level() >= 2:
                get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"
        path = relevant_file.strip()
        return dict(body=body, path=path, position=absolute_position) if subject_type == "LINE" else {}

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str | int,
                               original_suggestion=None) -> bool:
        # The base contract passes the line's text; publish_inline_comments passes an already resolved line number.
        if not isinstance(relevant_line_in_file, int):
            comment = self.create_inline_comment(body, relevant_file, relevant_line_in_file)
            if not comment:
                get_logger().error(f"Could not find line '{relevant_line_in_file}' in '{relevant_file}' "
                                   "to publish an inline comment")
                return False
            relevant_file, relevant_line_in_file = comment["path"], comment["position"]
        return self._post_inline_comment(body, relevant_file, relevant_line_in_file)

    def _post_inline_comment(self, body: str, relevant_file: str, from_line: int, to_line: int = None) -> bool:
        # Bitbucket Cloud anchors a span with 'start_to' and 'to'. Anything that is not a real
        # span is posted as a single-line comment.
        body = self.limit_output_characters(body, self.max_comment_length)
        if isinstance(to_line, int) and to_line > from_line:
            inline = {"start_to": from_line, "to": to_line, "path": relevant_file}
            location = f"lines {from_line}-{to_line}"
        else:
            inline = {"to": from_line, "path": relevant_file}
            location = f"line {from_line}"
        payload = json.dumps({
            "content": {
                "raw": body,
            },
            "inline": inline,
        })
        try:
            response = requests.request(
                "POST", self.bitbucket_comment_api_url, data=payload, headers=self.headers,
                timeout=get_http_request_timeout()
            )
            response.raise_for_status()
        except Exception as e:
            get_logger().error(
                f"Failed to publish inline comment to '{relevant_file}' at {location}, error: {e}")
            return False
        recent_bodies = getattr(self, "_published_inline_comment_bodies", None)
        if recent_bodies is None:
            recent_bodies = []
            self._published_inline_comment_bodies = recent_bodies
        if body not in recent_bodies:
            recent_bodies.append(body)
        return True

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        relevant_file = quote(relevant_file, safe="/")
        if relevant_line_start == -1:
            link = f"{self.pr_url}/#L{relevant_file}"
        else:
            link = f"{self.pr_url}/#L{relevant_file}T{relevant_line_start}"
        return link

    def publish_inline_comments(self, comments: list[dict]) -> bool:
        publishable_count = 0
        published_count = 0
        for comment in comments:
            to_line = None
            if 'position' in comment:
                from_line = comment['position']
            elif 'start_line' in comment:  # multi-line comment
                from_line = comment['start_line']
                to_line = comment.get('line')
            elif 'line' in comment:  # single-line comment
                from_line = comment['line']
            else:
                get_logger().error(f"Could not publish inline comment {comment}")
                continue

            publishable_count += 1
            if self._post_inline_comment(comment['body'], comment['path'], from_line, to_line):
                published_count += 1

        # A partial failure must not report failure: the caller republishes the whole
        # list, which would post the already-accepted suggestions a second time.
        return published_count > 0 or publishable_count == 0

    def get_title(self):
        return self.pr.title

    @cache_languages
    def get_languages(self):
        language = self._get_repo().get_data("language")
        return {language: 0} if language else {}

    def get_pr_branch(self):
        return self.pr.source_branch

    # This function attempts to get the default branch of the repository. As a fallback, uses the PR destination branch.
    # Note: Must be running from a PR context.
    def get_repo_default_branch(self):
        try:
            url_repo = f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/"
            response_repo = requests.request(
                "GET", url_repo, headers=self.headers, timeout=get_http_request_timeout())
            response_repo.raise_for_status()
            return response_repo.json()['mainbranch']['name']
        except (requests.RequestException, KeyError, TypeError) as error:
            if isinstance(error, requests.RequestException):
                get_logger().warning(
                    f"Failed to read the default branch of {self.workspace_slug}/{self.repo_slug} "
                    f"({type(error).__name__}); using the pull request's destination branch instead")
            return self.pr.destination_branch

    def get_owning_namespace(self) -> str | None:
        if not getattr(self, "headers", None):
            return None
        return self.workspace_slug

    def get_pr_description_full(self):
        return self.pr.description

    def get_user_id(self):
        return 0

    def _get_authenticated_account_id(self) -> str:
        agent_account_id = getattr(self, "_agent_account_id", None)
        if isinstance(agent_account_id, str) and agent_account_id.strip():
            return agent_account_id

        response = requests.request(
            "GET",
            "https://api.bitbucket.org/2.0/user",
            headers=self.headers,
            timeout=_get_identity_request_timeout(),
        )
        response.raise_for_status()
        account_data = response.json()
        agent_account_id = account_data.get("account_id") if isinstance(account_data, dict) else None
        if not isinstance(agent_account_id, str) or not agent_account_id.strip():
            raise RuntimeError("Bitbucket authenticated account cannot be verified")
        self._agent_account_id = agent_account_id
        return agent_account_id

    def is_comment_authored_by_pr_agent(self, comment) -> bool:
        """Verify a Bitbucket Cloud comment belongs to this authenticated account."""
        cloud_comment = self._get_cloud_comment(comment)
        comment_data = cloud_comment if isinstance(cloud_comment, dict) else getattr(cloud_comment, "data", None)
        if not isinstance(comment_data, dict):
            raise RuntimeError("Bitbucket comment author cannot be verified")
        author = comment_data.get("user") or comment_data.get("author")
        comment_account_id = author.get("account_id") if isinstance(author, dict) else None
        if not isinstance(comment_account_id, str) or not comment_account_id.strip():
            raise RuntimeError("Bitbucket comment author cannot be verified")

        agent_account_id = self._get_authenticated_account_id()
        return comment_account_id.casefold() == agent_account_id.casefold()

    def get_issue_comments(self):
        comments = []

        for comment in self.pr.comments():
            raw_body = getattr(comment, "raw", None)
            if not isinstance(raw_body, str):
                continue

            comments.append(
                SimpleNamespace(
                    body=raw_body,
                    _cloud_comment=comment,
                )
            )

        return comments

    def get_persistent_comment_bodies(self) -> list[str]:
        """Return existing Bitbucket Cloud comment bodies for inline deduplication."""
        bodies = list(getattr(self, "_published_inline_comment_bodies", []))
        for comment in self.get_issue_comments():
            body = getattr(comment, "body", "")
            if body and body not in bodies:
                bodies.append(body)
        return bodies

    def get_recent_inline_comment_bodies(self) -> list[str]:
        """Return inline comment bodies published during this provider run."""
        return list(getattr(self, "_published_inline_comment_bodies", []))

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    @staticmethod
    def _parse_pr_url(pr_url: str) -> tuple[str, int, int]:
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

    def get_pr_file_content(self, file_path: str, branch: str, propagate_errors: bool = True) -> str:
        try:
            if branch == self.pr.source_branch:
                branch = self.pr.data["source"]["commit"]["hash"]
            elif branch == self.pr.destination_branch:
                branch = self.pr.data["destination"]["commit"]["hash"]
            url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/"
                   f"{branch}/{quote(file_path, safe='/')}")
            response = requests.request("GET", url, headers=self.headers, timeout=get_http_request_timeout())
            if response.status_code == 404:  # not found
                return ""
            # Distinguish an unavailable file from a failed request to prevent an error response
            # body from being treated as repository instructions or changelog content.
            response.raise_for_status()
            contents = response.text
            return contents
        except Exception:
            if propagate_errors:
                raise
            return ""

    def get_pr_file_content_snapshot(self, file_path: str, branch: str) -> FileContentSnapshot:
        if not self._pr_head_in_base_repo():
            raise ValueError("Cannot snapshot a fork pull request for writing")
        if branch != self.pr.source_branch:
            raise ValueError("Bitbucket file snapshots require the PR source branch")
        revision = self.pr.data["source"]["commit"]["hash"]
        if not isinstance(revision, str) or not revision:
            raise ValueError("Bitbucket file snapshot is missing its source commit")
        url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/"
               f"{revision}/{quote(file_path, safe='/')}")
        response = requests.request(
            "GET", url, headers=self.headers, timeout=get_http_request_timeout())
        if response.status_code == 404:
            return FileContentSnapshot("", False, revision)
        response.raise_for_status()
        return FileContentSnapshot(response.text, True, revision)

    def create_or_update_pr_file(
        self, file_path: str, branch: str, contents="", message="", *, expected_snapshot: FileContentSnapshot
    ) -> None:
        if not self._pr_head_in_base_repo():
            raise ValueError("Cannot write to a fork pull request")
        if branch != self.pr.source_branch:
            raise ValueError("Bitbucket file writes require the PR source branch")
        if not isinstance(expected_snapshot.revision, str) or not expected_snapshot.revision:
            raise ValueError("Bitbucket file write requires the captured source commit")
        url = (f"https://api.bitbucket.org/2.0/repositories/{self.workspace_slug}/{self.repo_slug}/src/")
        if not message:
            if contents:
                message = f"Update {file_path}"
            else:
                message = f"Create {file_path}"
        files = {file_path: contents}
        data = {
            "message": message,
            "branch": branch,
            # Assert the current HEAD of an existing branch; do not rely on this to
            # guard a deleted branch's lifecycle because this endpoint can recreate it.
            "parents": expected_snapshot.revision,
        }
        headers = {'Authorization': self.headers['Authorization']} if 'Authorization' in self.headers else {}
        response = requests.request(
            "POST", url, headers=headers, data=data, files=files, timeout=get_http_request_timeout())
        response.raise_for_status()

    def _get_pr_file_content(self, remote_link: str):
        try:
            response = requests.request("GET", remote_link, headers=self.headers, timeout=get_http_request_timeout())
            if response.status_code == 404:  # not found
                return ""
            response.raise_for_status()
            contents = response.text
            return contents
        except Exception as error:
            if isinstance(error, requests.RequestException):
                get_logger().warning(
                    f"Failed to read {remote_link!r} ({type(error).__name__}); treating it as an empty file")
            return ""

    def get_commit_messages(self) -> str:
        return ""  # not implemented yet

    # bitbucket does not support labels
    def publish_description(self, pr_title: str, description: str) -> None:
        payload_dict = {"description": description}
        if pr_title is not None:
            payload_dict["title"] = pr_title
        payload = json.dumps(payload_dict)

        response = requests.request(
            "PUT", self.bitbucket_pull_request_api_url, headers=self.headers, data=payload,
            timeout=get_http_request_timeout())
        if not 200 <= response.status_code < 300:
            message = f"Failed to update description, error code: {response.status_code}"
            get_logger().error(message)
            raise RuntimeError(message)

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
            get_logger().error(
                f"repo_url_to_clone: {redact_credentials(repo_url_to_clone)} is not a valid bitbucket URL.")
            return None

        if self.auth_type == "basic":
            # Basic auth with token
            clone_url = f"{scheme}x-token-auth:{self.basic_token}@bitbucket.org{base_url}"
        elif self.auth_type == "bearer":
            # Bearer token
            clone_url = f"{scheme}x-token-auth:{self.bearer_token}@bitbucket.org{base_url}"
        else:
            # This case should ideally not be reached if __init__ validates auth_type
            get_logger().error(
                f"Unsupported or uninitialized auth_type: "
                f"{getattr(self, 'auth_type', 'N/A')}. Returning None"
            )
            return None

        return clone_url
