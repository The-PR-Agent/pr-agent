from collections import Counter
from pathlib import Path
from typing import List

from git import Repo

from pr_agent.algo.language_handler import build_language_file_matcher
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import format_pr_code_suggestions_header, show_run_details
from pr_agent.config_loader import _find_repository_root, get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger


class PullRequestMimic:
    """
    This class mimics the PullRequest class from the PyGithub library for the LocalGitProvider.
    """

    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


class LocalGitProvider(GitProvider):
    """
    This class implements the GitProvider interface for local git repositories.
    It mimics the PR functionality of the GitProvider interface,
    but does not require a hosted git repository.
    Instead of providing a PR url, the user provides a local branch path to generate a diff-patch.
    It supports the /review, /describe and /improve capabilities; each writes its output to a
    file (review.md, description.md, improve.md) since there is no hosted PR to comment on.
    """

    def __init__(self, target_branch_name, incremental=False):
        self.repo_path = _find_repository_root()
        if self.repo_path is None:
            raise ValueError('Could not find repository root')
        self.repo = Repo(self.repo_path)
        if self.repo.head.is_detached:
            self.head_branch_name = self.repo.head.commit.hexsha[:7]
        else:
            self.head_branch_name = self.repo.head.ref.name
        self.target_branch_name = target_branch_name
        self._prepare_repo()
        self.diff_files = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())
        self.description_path = get_settings().get('local.description_path') \
            if get_settings().get('local.description_path') is not None else self.repo_path / 'description.md'
        self.review_path = get_settings().get('local.review_path') \
            if get_settings().get('local.review_path') is not None else self.repo_path / 'review.md'
        self.improve_path = get_settings().get('local.improve_path') \
            if get_settings().get('local.improve_path') is not None else self.repo_path / 'improve.md'
        # inline code comments are not supported for local git repositories
        get_settings().pr_reviewer.inline_code_comments = False

    def _prepare_repo(self):
        """
        Prepare the repository for PR-mimic generation.
        """
        get_logger().debug('Preparing repository for PR-mimic generation...')
        if self.repo.is_dirty():
            raise ValueError('The repository is not in a clean state. Please commit or stash pending changes.')
        if self.target_branch_name not in self.repo.heads:
            raise KeyError(f'Branch: {self.target_branch_name} does not exist')

    def is_supported(self, capability: str) -> bool:
        if capability in ['get_issue_comments', 'create_inline_comment', 'publish_inline_comments', 'get_labels',
                          'gfm_markdown']:
            return False
        return True

    def supports_code_suggestions_artifact(self) -> bool:
        return True

    def get_diff_files(self) -> list[FilePatchInfo]:
        diffs = self.repo.head.commit.diff(
            self.repo.merge_base(self.repo.head, self.repo.branches[self.target_branch_name]),
            create_patch=True,
            R=True
        )
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
                get_logger().warning(f"Skipping non-UTF-8 file in local diff: {filename!r} ({e})")
                continue
            edit_type = EDIT_TYPE.MODIFIED
            if diff_item.new_file:
                edit_type = EDIT_TYPE.ADDED
            elif diff_item.deleted_file:
                edit_type = EDIT_TYPE.DELETED
            elif diff_item.renamed_file:
                edit_type = EDIT_TYPE.RENAMED
            diff_files.append(
                FilePatchInfo(original_file_content_str,
                              new_file_content_str,
                              patch,
                              filename,
                              edit_type=edit_type,
                              old_filename=None if diff_item.a_path == diff_item.b_path else diff_item.a_path
                              )
            )
        self.diff_files = diff_files
        return diff_files

    def get_files(self) -> List[str]:
        """
        Returns a list of files with changes in the diff.
        """
        diff_index = self.repo.head.commit.diff(
            self.repo.merge_base(self.repo.head, self.repo.branches[self.target_branch_name]),
            R=True
        )
        # Get the list of changed files
        diff_files = [item.a_path for item in diff_index]
        return diff_files

    def publish_description(self, pr_title: str, pr_body: str):
        with open(self.description_path, "w") as file:
            title = self.get_pr_title() if pr_title is None else pr_title
            file.write(title + '\n' + pr_body)

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        # Temporary comments (e.g. "Preparing suggestions...") have no place to live
        # locally and would otherwise clobber the persisted review.md; skip them.
        if is_temporary:
            return
        with open(self.review_path, "w", encoding="utf-8") as file:
            # Write the string to the file
            file.write(pr_comment)

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str, original_suggestion=None):
        raise NotImplementedError('Publishing inline comments is not implemented for the local git provider')

    def publish_inline_comments(self, comments: list[dict]):
        raise NotImplementedError('Publishing inline comments is not implemented for the local git provider')

    def publish_code_suggestion(self, body: str, relevant_file: str,
                                relevant_lines_start: int, relevant_lines_end: int):
        raise NotImplementedError('Publishing code suggestions is not implemented for the local git provider')

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return self.publish_code_suggestions_artifact(code_suggestions)

    def publish_code_suggestions_artifact(
            self, code_suggestions: list, artifact_footer: str = "",
            no_suggestions_message: str = "No code suggestions found for the PR.") -> bool:
        """
        Write /improve output to a file (improve.md by default).

        There is no hosted PR to attach inline suggestions to, so the suggestions the
        tool built for inline publishing are rendered as a single markdown document,
        mirroring how /review and /describe persist their output locally. Each entry
        carries a rendered 'body' plus its file and line range; format them into a
        readable section per suggestion. Returns True so the caller does not fall back
        to publishing suggestions one by one.
        """
        sections = []
        for suggestion in code_suggestions:
            relevant_file = suggestion.get('relevant_file', '').strip()
            start = suggestion.get('relevant_lines_start')
            end = suggestion.get('relevant_lines_end')
            location = relevant_file
            if start is not None:
                location += f" [{start}-{end}]" if end is not None and end != start else f" [{start}]"
            header = f"### {location}" if location else "### Suggestion"
            sections.append(f"{header}\n\n{suggestion.get('body', '').strip()}")
        header = format_pr_code_suggestions_header(markdown_level=1)
        pr_body = f"{header}\n\n" + "\n\n".join(sections) if sections \
            else f"{header}\n\n{no_suggestions_message}"
        pr_body += artifact_footer
        if not sections and get_settings().get("config.output_run_details", False):
            pr_body += show_run_details(False)
        with open(self.improve_path, "w", encoding="utf-8") as file:
            file.write(pr_body)
        return True

    def publish_labels(self, labels):
        pass  # Not applicable to the local git provider, but required by the interface

    def remove_initial_comment(self):
        pass  # Not applicable to the local git provider, but required by the interface

    def remove_comment(self, comment):
        pass  # Not applicable to the local git provider, but required by the interface

    def add_eyes_reaction(self, comment):
        pass  # Not applicable to the local git provider, but required by the interface

    def get_commit_messages(self):
        pass  # Not applicable to the local git provider, but required by the interface

    def get_repo_settings(self):
        pass  # Not applicable to the local git provider, but required by the interface

    def remove_reaction(self, comment):
        pass  # Not applicable to the local git provider, but required by the interface

    def get_languages(self):
        """
        Calculate percentage of languages in repository. Used for hunk prioritisation.

        Keys are language NAMES (e.g. "Python"), not raw extensions: the consumer
        sort_files_by_main_languages() maps each name back to its extensions, so
        returning extensions ("py") silently drops every file into the "Other"
        bucket and defeats the prioritisation this method exists for. Use the
        shared configured filename matcher so all providers apply the same
        full-filename, multipart-extension, and case-sensitive rules.
        """
        lang_map = get_settings().get("language_extension_map_org", {}) or {}
        get_language = build_language_file_matcher(lang_map)

        # Get all files in repository
        filepaths = [Path(item.path) for item in self.repo.tree().traverse() if item.type == 'blob']
        # Identify language by filename (mapped to its language name) and count
        lang_count = Counter()
        for filepath in filepaths:
            language = get_language(filepath.name)
            if language:
                lang_count[language] += 1
        # Convert counts to percentages
        total = sum(lang_count.values()) or 1
        return {lang: count / total * 100 for lang, count in lang_count.items()}

    def get_pr_branch(self):
        return self.repo.head

    def get_user_id(self):
        return -1  # Not used anywhere for the local provider, but required by the interface

    def get_pr_description_full(self):
        commits_diff = list(self.repo.iter_commits(self.target_branch_name + '..HEAD'))
        # Get the commit messages and concatenate
        commit_messages = " ".join([commit.message for commit in commits_diff])
        # TODO Handle the description better - maybe use gpt-3.5 summarisation here?
        return commit_messages[:200]  # Use max 200 characters

    def get_pr_title(self):
        """
        Substitutes the branch-name as the PR-mimic title.
        """
        return self.head_branch_name

    def get_issue_comments(self):
        raise NotImplementedError('Getting issue comments is not implemented for the local git provider')

    def get_pr_labels(self, update=False):
        raise NotImplementedError('Getting labels is not implemented for the local git provider')
