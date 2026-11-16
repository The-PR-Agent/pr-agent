"""Side-effect-free helpers shared by the GitHub app server and the Action runner.

This module must stay import-safe for any entry point: no setup_logger() call,
no settings mutation at import time. The Action runner imports from here so it
no longer pulls in pr_agent.servers.github_app, whose import switches logs to
JSON and overrides GITHUB.DEPLOYMENT_TYPE to "app".
"""

from typing import Any

from starlette_context import context

from pr_agent.algo.setting_utils import _as_bool_setting, normalize_setting_list
from pr_agent.algo.utils import encode_user_text_arg
from pr_agent.config_loader import get_bot_display_name, get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.utils import apply_host_settings
from pr_agent.log import get_logger
from pr_agent.servers.utils import is_ask_command_comment


def _normalise_setting_list(value):
    return normalize_setting_list(value)


def revoke_auto_approval_on_push(payload: dict) -> None:
    """Dismiss PR-Agent's approval when a pull_request event changes the code it approved.

    That is a push, or a retarget onto another base branch. Call this before any filter that
    may skip the event: an approval must not outlive what it was given for just because the
    push came from a bot or the PR is ignored.
    """
    action = payload.get("action")
    if action == "synchronize":
        why = "new commits were pushed"
    elif action == "edited" and "base" in (payload.get("changes") or {}):
        why = "the base branch changed"
    elif action == "reopened":
        why = "the pull request was reopened"
    else:
        return
    pr_url = (payload.get("pull_request") or {}).get("url")
    # The gates are host-only, so read them after the trusted host layer and before any repository override.
    try:
        apply_host_settings()
    except Exception as e:
        get_logger().warning(f"Could not apply host settings before approval revocation: {type(e).__name__}")
    config = get_settings().config
    if not pr_url or not _as_bool_setting(
            config.get("enable_auto_approval", False), setting_name="config.enable_auto_approval"):
        return
    # A dry run writes nothing, dismissals included.
    if not _as_bool_setting(config.get("publish_output", True), default=True):
        return
    try:
        if not get_git_provider_with_context(pr_url).revoke_auto_approval(
            f"{get_bot_display_name()} withdrew its approval: {why}."
        ):
            get_logger().error(f"Could not confirm that PR-Agent's approvals were dismissed after: {why}")
    except Exception as e:
        get_logger().exception(f"Failed to dismiss PR-Agent approvals after a push, error: {e}")


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


def handle_file_question_comment(body: dict, comment_body: str) -> list[str] | None:
    """Turn /ask or /ask_question on a whole file into a question about that file's diff.

    The file and the comment's thread go into the request context, where PRQuestions reads them to
    answer in that thread. Returns None for any other comment.
    """
    comment = body["comment"]
    if (comment.get("subject_type") == "file"
            and comment_body.split(maxsplit=1)[0].lower() in ("/ask", "/ask_question")):
        parts = comment_body.strip().split(maxsplit=1)
        question = parts[1] if len(parts) > 1 else ""
        if not isinstance(comment.get("path"), str) or not comment["path"].strip():
            raise ValueError("Invalid file-level review comment metadata")
        context["file_question_path"] = comment["path"]
        context["file_question_comment_id"] = comment.get("in_reply_to_id") or comment["id"]
        context["file_question_origin_comment_id"] = comment["id"]
        context["file_question_comment_url"] = comment["html_url"]
        return ["/ask", encode_user_text_arg(question)]
    return None
