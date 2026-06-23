import json
import os
import pathlib
import shutil
import subprocess
import uuid
from collections import Counter, namedtuple
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

import requests
import urllib3.util
from git import Repo

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.local_git_provider import PullRequestMimic
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
    get_logger().info("Cloning %s to %s", url, directory)
    stdout = _call('git', 'clone', "--depth", "1", url, directory)
    get_logger().info(stdout)


def fetch(url, refspec, cwd):
    get_logger().info("Fetching %s %s", url, refspec)
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
    clone(repo_url, directory)
    fetch(repo_url, refspec, cwd=directory)
    checkout(cwd=directory)
    return directory


def adopt_to_gerrit_message(message):
    lines = message.splitlines()
    buf = []
    for line in lines:
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
    with (
        NamedTemporaryFile("w", delete=False) as tmp,
        open(src_filename, "r") as src
    ):
        lines = src.readlines()
        tmp.writelines(lines[:start - 1])
        if context:
            tmp.write(context)
        tmp.writelines(lines[end:])

    shutil.copy(tmp.name, src_filename)
    os.remove(tmp.name)


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
        }
    )
    response.raise_for_status()
    patch_server_endpoint = patch_server_endpoint.rstrip("/")
    return patch_server_endpoint + "/" + path


class GerritProvider(GitProvider):

    def __init__(self, key: str, incremental=False):
        self.project, self.refspec = key.split(':')
        assert self.project, "Project name is required"
        assert self.refspec, "Refspec is required"
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
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())

    def get_pr_title(self):
        """
        Substitutes the branch-name as the PR-mimic title.
        """
        return self.repo.branches[0].name

    def get_issue_comments(self):
        comments = list_comments(self.parsed_url, self.refspec)
        Comments = namedtuple('Comments', ['reversed'])
        Comment = namedtuple('Comment', ['body'])
        return Comments([Comment(c['message']) for c in reversed(comments)])

    def get_pr_labels(self, update=False):
        raise NotImplementedError(
            'Getting labels is not implemented for the gerrit provider')

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False):
        raise NotImplementedError(
            'Adding reactions is not implemented for the gerrit provider')

    def remove_reaction(self, issue_comment_id: int, reaction_id: int):
        raise NotImplementedError(
            'Removing reactions is not implemented for the gerrit provider')

    def get_commit_messages(self):
        return [self.repo.head.commit.message]

    def get_repo_settings(self):
        try:
            with open(self.repo_path / ".pr_agent.toml", 'rb') as f:
                contents = f.read()
            return contents
        except OSError:
            return b""

    def get_diff_files(self) -> list[FilePatchInfo]:
        diffs = self.repo.head.commit.diff(
            self.repo.head.commit.parents[0],  # previous commit
            create_patch=True,
            R=True
        )

        diff_files = []
        for diff_item in diffs:
            if diff_item.a_blob is not None:
                original_file_content_str = (
                    diff_item.a_blob.data_stream.read().decode('utf-8')
                )
            else:
                original_file_content_str = ""  # empty file
            if diff_item.b_blob is not None:
                new_file_content_str = diff_item.b_blob.data_stream.read(). \
                    decode('utf-8')
            else:
                new_file_content_str = ""  # empty file
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
                    diff_item.diff.decode('utf-8'),
                    diff_item.b_path,
                    edit_type=edit_type,
                    old_filename=None
                    if diff_item.a_path == diff_item.b_path
                    else diff_item.a_path
                )
            )
        self.diff_files = diff_files
        return diff_files

    def get_files(self):
        diff_index = self.repo.head.commit.diff(
            self.repo.head.commit.parents[0],  # previous commit
            R=True
        )
        # Get the list of changed files
        diff_files = [item.a_path for item in diff_index]
        return diff_files

    def get_languages(self):
        """
        Calculate percentage of languages in repository. Used for hunk
        prioritisation.
        """
        # Get all files in repository
        filepaths = [Path(item.path) for item in
                     self.repo.tree().traverse() if item.type == 'blob']
        # Identify language by file extension and count
        lang_count = Counter(
            ext.lstrip('.') for filepath in filepaths for ext in
            [filepath.suffix.lower()])
        # Convert counts to percentages
        total_files = len(filepaths)
        lang_percentage = {lang: count / total_files * 100 for lang, count
                           in lang_count.items()}
        return lang_percentage

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

    def publish_code_suggestions(self, code_suggestions: list):
        msg = []
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
            full_path = upload_patch(patch, path)
            reset_local_changes(self.repo_path)
            msg.append(f'* {description}\n{full_path}')

        if msg:
            add_comment(self.parsed_url, self.refspec, "\n".join(msg))
            return True

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not is_temporary:
            msg = adopt_to_gerrit_message(pr_comment)
            add_comment(self.parsed_url, self.refspec, msg)

    def publish_description(self, pr_title: str, pr_body: str):
        msg = adopt_to_gerrit_message(pr_body)
        add_comment(self.parsed_url, self.refspec, pr_title + '\n' + msg)

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

    def remove_initial_comment(self):
        # remove repo, cloned in previous steps
        # shutil.rmtree(self.repo_path)
        pass

    def remove_comment(self, comment):
        pass

    def get_pr_branch(self):
        return self.repo.head
