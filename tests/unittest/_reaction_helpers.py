"""A provider double for the reaction tests: only the reaction primitive is real.

`GitProvider` keeps declaring more abstract methods, and a test double that misses one cannot be
instantiated at all, so both the GitHub App and the GitLab webhook reaction tests take it from
here rather than each keeping a copy.
"""

from pr_agent.git_providers.git_provider import GitProvider


class _RecordingProvider(GitProvider):
    """Minimal concrete provider: only the reaction primitive is real."""

    def __init__(self):
        self.reactions = []
        self.removed = []

    def add_reaction(self, issue_comment_id: int, reaction: str):
        self.reactions.append((issue_comment_id, reaction))
        return len(self.reactions)

    # the abstract surface the base class declares
    def is_supported(self, capability): return True
    def get_files(self): return []
    def get_diff_files(self): return []
    def publish_description(self, pr_title, pr_body): pass
    def publish_comment(self, pr_comment, is_temporary=False): pass
    def publish_inline_comment(self, body, relevant_file, relevant_line_in_file, original_suggestion=None): pass
    def publish_inline_comments(self, comments): pass
    def remove_initial_comment(self): pass
    def remove_comment(self, comment): pass
    def get_languages(self): return {}
    def get_pr_branch(self): return ""
    def get_user_id(self): return ""
    def get_pr_description_full(self): return ""
    def get_issue_comments(self): return []
    def get_repo_settings(self): return b""
    def remove_reaction(self, issue_comment_id, reaction_id):
        self.removed.append((issue_comment_id, reaction_id))
        return True
    def get_commit_messages(self) -> str: return ""
    def publish_labels(self, labels): pass
    def get_pr_labels(self, update=False): return []
    def publish_code_suggestions(self, code_suggestions) -> bool: return True
