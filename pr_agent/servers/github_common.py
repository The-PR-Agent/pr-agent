"""Side-effect-free helpers shared by the GitHub app server and the Action runner.

This module must stay import-safe for any entry point: no setup_logger() call,
no settings mutation at import time. The Action runner imports from here so it
no longer pulls in pr_agent.servers.github_app, whose import switches logs to
JSON and overrides GITHUB.DEPLOYMENT_TYPE to "app".
"""

from typing import Any

from pr_agent.algo.setting_utils import normalize_setting_list
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.servers.utils import is_ask_command_comment


def _normalise_setting_list(value):
    return normalize_setting_list(value)


def matches_review_state(review_state: Any, configured_states: Any) -> bool:
    """Return whether a review state matches one of the configured states."""
    if not isinstance(review_state, str) or not review_state.strip():
        return False
    configured_states = _normalise_setting_list(configured_states)
    if not configured_states:
        return False
    normalized_state = review_state.strip().lower()
    return any(
        isinstance(state, str) and state.strip().lower() == normalized_state
        for state in configured_states
    )


def pushed_base_branch_merge(git_provider: Any, before_sha: Any, after_sha: Any) -> bool:
    """Whether a synchronize event's push only merged the base branch into the PR, as "Update branch" does.

    The pushed head (`after`) must be a merge commit whose first parent is the previous head (`before`) and
    whose second parent is on the base branch, so the push brought no other commit. The event does not say
    this: its `pull_request.merge_commit_sha` is GitHub's test merge of the head into the base, never the head
    itself. Without both commits, or when the provider cannot tell, the push is handled as an ordinary one.
    """
    is_base_branch_merge = getattr(git_provider, "is_base_branch_merge", None)
    if not callable(is_base_branch_merge) or not all(isinstance(sha, str) and sha for sha in (before_sha, after_sha)):
        return False
    try:
        return bool(is_base_branch_merge(after_sha, before_sha))
    except Exception as e:
        get_logger().warning(f"Could not tell whether {after_sha} only merges the base branch; handling the push: {e}")
        return False


def _reformat_quote_ask_command(comment_body: str) -> str | None:
    """Move a /ask command buried in a quoted Golf/mobile reply to the front so it
    is dispatched, preserving the whole question text. Returns None when the
    comment is not an image-quote reply carrying a /ask."""
    if '/ask' not in comment_body or not comment_body.strip().startswith('> ![image]'):
        return None
    before, _, after = comment_body.partition('/ask')
    return '/ask' + after + ' \n' + before.strip().lstrip('>')


def handle_line_comments(body: dict, comment_body: [str, Any]):
    if not comment_body:
        return ""
    start_line = body["comment"]["start_line"] or body["comment"].get("original_start_line")
    end_line = body["comment"]["line"] or body["comment"].get("original_line")
    start_line = end_line if not start_line else start_line
    # Strip only the command token, preserving /ask mentions within the question.
    parts = comment_body.strip().split(maxsplit=1)
    question = parts[1] if len(parts) > 1 else ""
    diff_hunk = body["comment"]["diff_hunk"]
    get_settings().set("ask_diff_hunk", diff_hunk)
    path = body["comment"]["path"]
    side = body["comment"]["side"]
    comment_id = body["comment"]["id"]
    if is_ask_command_comment(comment_body):
        # Build an argv list rather than concatenating into a shell-style
        # command string. PRAgent._handle_request() tokenises string requests
        # with single quotes treated literally, which neutralises any
        # shlex.quote() output and re-introduces the CLI-argument injection
        # vector (a quoted value containing whitespace splits into multiple
        # argv tokens). Passing a list bypasses the shlex path entirely.
        cmd = [
            "/ask_line",
            f"--line_start={start_line}",
            f"--line_end={end_line}",
            f"--side={side}",
            f"--file_name={path}",
            f"--comment_id={comment_id}",
        ]
        if question:
            cmd.append(question)
        return cmd
    return comment_body
