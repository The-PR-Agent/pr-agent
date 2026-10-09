import json
import pathlib
import posixpath
import re
import shutil
import stat
import string
import subprocess
import uuid
from collections import Counter, namedtuple
from pathlib import Path
from tempfile import mkdtemp
from typing import Optional

import requests
import urllib3.util
from git import Repo

from pr_agent.agent.request_policy import policy_metadata
from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.language_handler import build_language_file_matcher
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider, cache_languages, redact_credentials
from pr_agent.git_providers.local_git_provider import PullRequestMimic
from pr_agent.git_providers.request_timeout import get_http_request_timeout
from pr_agent.log import get_logger


def _call(*command, **kwargs) -> (int, str, str):
    res = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        **kwargs,
    )
    return res.stdout.decode()


def clone(url, directory):
    get_logger().info("Cloning {} to {}", redact_credentials(url), directory)
    stdout = _call('git', 'clone', "--depth", "1", url, directory)
    get_logger().info(stdout)


def fetch(url, refspec, cwd):
    get_logger().info("Fetching {} {}", redact_credentials(url), refspec)
    stdout = _call(
        'git', 'fetch', '--depth', '2', url, refspec,
        cwd=cwd
    )
    get_logger().info(stdout)


def checkout(cwd):
    get_logger().info("Checking out")
    stdout = _call('git', 'checkout', "FETCH_HEAD", cwd=cwd)
    get_logger().info(stdout)


def show(*args, cwd=None):
    get_logger().info("Show")
    return _call('git', 'show', *args, cwd=cwd)


def diff(*args, cwd=None):
    get_logger().info("Diff")
    patch = _call('git', 'diff', *args, cwd=cwd)
    if not patch:
        get_logger().warning("No changes found")
        return
    return patch


def reset_local_changes(cwd):
    get_logger().info("Reset local changes")
    _call('git', 'checkout', "--force", cwd=cwd)


def add_comment(url: urllib3.util.Url, refspec, message):
    *_, patchset, changenum = refspec.rsplit("/")
    message = "'" + message.replace("'", "'\"'\"'") + "'"
    return _call(
        "ssh",
        "-p", str(url.port),
        f"{url.auth}@{url.host}",
        "gerrit", "review",
        "--message", message,
        # "--code-review", score,
        f"{patchset},{changenum}",
    )


def list_comments(url: urllib3.util.Url, refspec):
    *_, patchset, _ = refspec.rsplit("/")
    stdout = _call(
        "ssh",
        "-p", str(url.port),
        f"{url.auth}@{url.host}",
        "gerrit", "query",
        "--comments",
        "--current-patch-set", patchset,
        "--format", "JSON",
    )
    change_set, *_ = stdout.splitlines()
    return json.loads(change_set)["currentPatchSet"]["comments"]


def prepare_repo(url: urllib3.util.Url, project, refspec):
    repo_url = (f"{url.scheme}://{url.auth}@{url.host}:{url.port}/{project}")

    directory = pathlib.Path(mkdtemp())
    try:
        clone(repo_url, directory)
        fetch(repo_url, refspec, cwd=directory)
        checkout(cwd=directory)
    except BaseException:
        try:
            shutil.rmtree(directory)
        except OSError as cleanup_error:
            get_logger().warning(
                "Failed to clean up temp repo at {} after setup failed: {}",
                directory, cleanup_error
            )
        raise
    return directory


_ASK_HEADING_PREFIX = "### **"
_ASK_HEADING_SUFFIX = "** ❓"
_ESCAPED_MARKDOWN_PUNCTUATION = re.compile(
    r"\\([" + re.escape(string.punctuation) + r"])"
)


def _convert_gerrit_ask_heading(line: str) -> str | None:
    """Convert only the generated /ask heading to Gerrit's plain-text form."""
    if not line.startswith(_ASK_HEADING_PREFIX) or not line.endswith(_ASK_HEADING_SUFFIX):
        return None
    escaped_heading = line[len(_ASK_HEADING_PREFIX):-len(_ASK_HEADING_SUFFIX)]
    heading = _ESCAPED_MARKDOWN_PUNCTUATION.sub(
        lambda match: match.group(1),
        escaped_heading,
    )
    return f"{heading}❓"


def adopt_to_gerrit_message(message):
    lines = message.splitlines()
    buf = []
    for line_number, line in enumerate(lines):
        if line_number == 0:
            ask_heading = _convert_gerrit_ask_heading(line.strip())
            if ask_heading is not None:
                buf.append(f"\n{ask_heading}:")
                continue

        # remove markdown formatting
        line = (line.replace("*", "")
                .replace("``", "`")
                .replace("<details>", "")
                .replace("</details>", "")
                .replace("<summary>", "")
                .replace("</summary>", ""))

        line = line.strip()
        if line.startswith('#'):
            buf.append("\n" +
                       line.replace('#', '').removesuffix(":").strip() +
                       ":")
            continue
        elif line.startswith('-'):
            buf.append(line.removeprefix('-').strip())
            continue
        else:
            buf.append(line)
    return "\n".join(buf).strip()


def add_suggestion(src_filename, context: str, start, end: int):
    # Rewrite the file in place with its own line endings, so the patch built from
    # `git diff` holds only the suggestion: no CRLF-to-LF rewrite and no mode change.
    with open(src_filename, "r", encoding="utf-8", newline="") as src:
        lines = src.readlines()
    # Match the ending of the first replaced line, falling back to the first line.
    anchor = lines[start - 1] if 0 < start <= len(lines) else (lines[0] if lines else "")
    if context and anchor.endswith("\r\n"):
        context = context.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(src_filename, "w", encoding="utf-8", newline="") as dst:
        dst.writelines(lines[:start - 1])
        if context:
            dst.write(context)
        dst.writelines(lines[end:])


def upload_patch(patch, path):
    patch_server_endpoint = get_settings().get(
        'gerrit.patch_server_endpoint')
    patch_server_token = get_settings().get(
        'gerrit.patch_server_token')

    response = requests.post(
        patch_server_endpoint,
        json={
            "content": patch,
            "path": path,
        },
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {patch_server_token}",
        },
        timeout=get_http_request_timeout(),
    )
    response.raise_for_status()
    patch_server_endpoint = patch_server_endpoint.rstrip("/")
    return patch_server_endpoint + "/" + path


class GerritProvider(GitProvider):

    def get_request_policy_metadata(self, required_fields: set[str]) -> dict:
        # The checkout provides no reliable source/target branch names or review labels.
        return policy_metadata(title=self.pr.title, sender=self.repo.head.commit.author.email,
                               repo_full_name=self.project, source_branch=None, target_branch=None)

    def __init__(self, key: str, incremental=False):
        self.repo_path = None
        self.project, self.refspec = key.split(':')
        assert self.project, "Project name is required"
        assert self.refspec, "Refspec is required"

        if not re.fullmatch(r"refs/changes/[0-9]{2}/[0-9]+/[0-9]+", self.refspec):
            raise ValueError(
                "Gerrit refspec must match refs/changes/NN/<change>/<patchset>"
            )
        base_url = get_settings().get('gerrit.url')
        assert base_url, "Gerrit URL is required"
        user = get_settings().get('gerrit.user')
        assert user, "Gerrit user is required"

        parsed = urllib3.util.parse_url(base_url)
        self.parsed_url = urllib3.util.parse_url(
            f"{parsed.scheme}://{user}@{parsed.host}:{parsed.port}"
        )

        self.repo_path = prepare_repo(
            self.parsed_url, self.project, self.refspec
        )
        self.repo = Repo(self.repo_path)
        assert self.repo
        self.pr_url = base_url
        self._commit_diffs = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())

    def get_pr_title(self):
        """
        Substitutes the branch-name as the PR-mimic title.
        """
        return self.repo.branches[0].name

    def get_issue_comments(self) -> list:
        Comment = namedtuple('Comment', ['body'])
        return [Comment(c['message']) for c in list_comments(self.parsed_url, self.refspec)]

    def get_pr_labels(self, update=False):
        raise NotImplementedError(
            'Getting labels is not implemented for the gerrit provider')

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        raise NotImplementedError(
            'Adding reactions is not implemented for the gerrit provider')

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        raise NotImplementedError(
            'Removing reactions is not implemented for the gerrit provider')

    def get_commit_messages(self) -> str:
        return self.repo.head.commit.message

    @staticmethod
    def _resolve_settings_entry(settings_tree, settings_path):
        path_parts = settings_path.split("/")
        visited_links = set()
        resolved_parts = []
        tree_stack = [settings_tree]
        while path_parts:
            part = path_parts.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved_parts:
                    return None
                resolved_parts.pop()
                tree_stack.pop()
                continue

            entry = tree_stack[-1] / part
            if stat.S_ISLNK(entry.mode):
                link_path = "/".join((*resolved_parts, part))
                link_state = (link_path, tuple(path_parts))
                if link_state in visited_links or len(visited_links) >= 40:
                    return None
                visited_links.add(link_state)
                target = entry.data_stream.read().decode("utf-8")
                if not target or posixpath.isabs(target):
                    return None
                path_parts = target.split("/") + path_parts
            elif path_parts:
                if entry.type != "tree":
                    return None
                resolved_parts.append(part)
                tree_stack.append(entry)
            else:
                return entry
        return None

    def get_repo_settings(self):
        try:
            settings_tree = self.repo.branches[0].commit.tree
            settings_entry = self._resolve_settings_entry(settings_tree, ".pr_agent.toml")
            if settings_entry is None or settings_entry.type != "blob":
                return b""
            return settings_entry.data_stream.read()
        except (IndexError, KeyError, OSError, UnicodeDecodeError, ValueError):
            return b""

    def _get_commit_diffs(self) -> list:
        """Return the cached, unfiltered commit diff, including rename detection and patches.

        Keep filtering in callers so repository settings loaded after construction
        take effect on every read.
        """
        diffs = getattr(self, '_commit_diffs', None)
        if diffs is None:
            diffs = list(
                self.repo.head.commit.diff(
                    self.repo.head.commit.parents[0],  # previous commit
                    create_patch=True,
                    R=True
                )
            )
            self._commit_diffs = diffs
        return diffs

    def get_diff_files(self) -> list[FilePatchInfo]:
        # Apply ignore rules at call time: __init__ reads the diff before repository
        # settings are loaded.
        diffs = filter_ignored(self._get_commit_diffs(), 'gerrit')

        diff_files = []
        for diff_item in diffs:
            filename = diff_item.b_path or diff_item.a_path
            try:
                if diff_item.a_blob is not None:
                    original_file_content_str = diff_item.a_blob.data_stream.read().decode("utf-8")
                else:
                    original_file_content_str = ""  # empty file
                if diff_item.b_blob is not None:
                    new_file_content_str = diff_item.b_blob.data_stream.read().decode("utf-8")
                else:
                    new_file_content_str = ""  # empty file
                patch = diff_item.diff.decode("utf-8")
            except UnicodeDecodeError as e:
                get_logger().warning(f"Skipping non-UTF-8 file in Gerrit diff: {filename!r} ({e})")
                continue
            edit_type = EDIT_TYPE.MODIFIED
            if diff_item.new_file:
                edit_type = EDIT_TYPE.ADDED
            elif diff_item.deleted_file:
                edit_type = EDIT_TYPE.DELETED
            elif diff_item.renamed_file:
                edit_type = EDIT_TYPE.RENAMED
            diff_files.append(
                FilePatchInfo(
                    original_file_content_str,
                    new_file_content_str,
                    patch,
                    filename,
                    edit_type=edit_type,
                    old_filename=None
                    if diff_item.a_path == diff_item.b_path
                    else diff_item.a_path
                )
            )
        self.diff_files = diff_files
        return diff_files

    def get_files(self):
        # Read names from the filtered raw diff to avoid another walk or blob decoding.
        # Use the destination path for renames and the original path for deletions.
        return [
            path
            for path in (diff.b_path or diff.a_path for diff in filter_ignored(self._get_commit_diffs(), 'gerrit'))
            if path
        ]

    @cache_languages
    def get_languages(self):
        """
        Calculate percentage of languages in repository. Used for hunk
        prioritisation.
        """
        lang_map = get_settings().get("language_extension_map_org", {}) or {}
        get_language = build_language_file_matcher(lang_map)

        # Get all files in repository
        filepaths = [Path(item.path) for item in
                     self.repo.tree().traverse() if item.type == 'blob']
        # Identify language by filename and count
        lang_count = Counter()
        for filepath in filepaths:
            language = get_language(filepath.name)
            if language:
                lang_count[language] += 1
        # Convert counts to percentages
        total = sum(lang_count.values()) or 1
        return {lang: count / total * 100 for lang, count in lang_count.items()}

    def get_pr_description_full(self):
        return self.repo.head.commit.message

    def get_user_id(self):
        return self.repo.head.commit.author.email

    def is_supported(self, capability: str) -> bool:
        if capability in [
            # 'get_issue_comments',
            'create_inline_comment',
            'publish_inline_comments',
            'get_labels',
            'gfm_markdown'
        ]:
            return False
        return True

    def split_suggestion(self, msg) -> tuple[str, str]:
        is_code_context = False
        description = []
        context = []
        for line in msg.splitlines():
            if line.startswith('```suggestion'):
                is_code_context = True
                continue
            if line.startswith('```'):
                is_code_context = False
                continue
            if is_code_context:
                context.append(line)
            else:
                description.append(
                    line.replace('*', '')
                )

        return (
            '\n'.join(description),
            '\n'.join(context) + '\n' if context else ''
        )

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        msg = []
        publishable_count = 0
        published_count = 0
        repo_root = pathlib.Path(self.repo_path).resolve()
        for suggestion in code_suggestions:
            # Validate suggestion structure before accessing keys
            if not isinstance(suggestion, dict) or not isinstance(suggestion.get("relevant_file"), str):
                get_logger().warning("Skipping malformed suggestion: missing or invalid 'relevant_file'")
                continue
            # Sanitize file path to prevent directory traversal
            try:
                target_path = (repo_root / suggestion["relevant_file"]).resolve()
                target_path.relative_to(repo_root)
            except ValueError:
                get_logger().warning(f"Skipping suggestion with path traversal: {suggestion['relevant_file']}")
                continue

            publishable_count += 1
            description, code = self.split_suggestion(suggestion['body'])
            add_suggestion(
                target_path,
                code,
                suggestion["relevant_lines_start"],
                suggestion["relevant_lines_end"],
            )
            patch = diff(cwd=self.repo_path)
            patch_id = uuid.uuid4().hex[0:4]
            path = "/".join(["codium-ai", self.refspec, patch_id])
            uploaded = False
            try:
                full_path = upload_patch(patch, path)
                uploaded = True
            finally:
                try:
                    reset_local_changes(self.repo_path)
                except Exception as cleanup_error:
                    if uploaded:
                        raise
                    get_logger().warning(
                        "Failed to reset Gerrit edits after upload failed in {}: {}; stderr: {!r}",
                        self.repo_path, cleanup_error, getattr(cleanup_error, "stderr", None),
                    )
            msg.append(f'* {description}\n{full_path}')

        if msg:
            try:
                add_comment(self.parsed_url, self.refspec, "\n".join(msg))
                published_count += 1
            except Exception as e:
                get_logger().exception("Failed to publish Gerrit code suggestions: {}", e)

        return published_count > 0 or publishable_count == 0

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not is_temporary:
            msg = adopt_to_gerrit_message(pr_comment)
            add_comment(self.parsed_url, self.refspec, msg)

    def supports_comment_publish_confirmation(self) -> bool:
        return False

    def publish_description(self, pr_title: str, pr_body: str):
        msg = adopt_to_gerrit_message(pr_body)
        text = msg if pr_title is None else pr_title + '\n' + msg
        add_comment(self.parsed_url, self.refspec, text)

    def publish_inline_comments(self, comments: list[dict]):
        raise NotImplementedError(
            'Publishing inline comments is not implemented for the gerrit '
            'provider')

    def publish_inline_comment(self, body: str, relevant_file: str,
                               relevant_line_in_file: str, original_suggestion=None):
        raise NotImplementedError(
            'Publishing inline comments is not implemented for the gerrit '
            'provider')


    def publish_labels(self, labels):
        # Not applicable to the local git provider,
        # but required by the interface
        pass

    def cleanup(self):
        """Remove the temporary cloned repository from disk."""
        if self.repo_path and pathlib.Path(self.repo_path).exists():
            try:
                shutil.rmtree(self.repo_path)
                get_logger().info("Cleaned up temp repo at {}", self.repo_path)
            except (OSError, PermissionError) as e:
                get_logger().warning(
                    "Failed to clean up temp repo at {}: {}",
                    self.repo_path, e
                )

    def __del__(self):
        """Safety net: clean up temp repo if cleanup() was not called.

        The server's finally block can only reach providers stored in
        starlette_context. PRQuestions builds its own provider with
        get_git_provider(), so an /ask request never registers there and
        would leak its clone without this.
        """
        try:
            self.cleanup()
        except Exception as e:
            get_logger().debug("Temp repo cleanup failed during __del__: {}", e)

    def remove_initial_comment(self):
        # Do NOT call cleanup() here — this method is invoked during the
        # request lifecycle while the cloned repo is still needed by
        # subsequent commands.  Actual cleanup happens in the server's
        # finally block and in __del__ as a safety net.
        pass

    def remove_comment(self, comment):
        pass

    def get_pr_branch(self):
        return self.repo.head
