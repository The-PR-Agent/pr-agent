"""Side-effect-free helpers shared by the GitHub app server and the Action runner.

This module must stay import-safe for any entry point: no setup_logger() call,
no settings mutation at import time. The Action runner imports from here so it
no longer pulls in pr_agent.servers.github_app, whose import switches logs to
JSON and overrides GITHUB.DEPLOYMENT_TYPE to "app".
"""

from typing import Any, Dict, Optional

from pr_agent.config_loader import get_settings
from pr_agent.servers.utils import is_ask_command_comment


def _normalise_setting_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


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


def _reformat_quote_ask_command(comment_body: str) -> Optional[str]:
    """Move a /ask command buried in a quoted Golf/mobile reply to the front so it
    is dispatched, preserving the whole question text. Returns None when the
    comment is not an image-quote reply carrying a /ask."""
    if '/ask' not in comment_body or not comment_body.strip().startswith('> ![image]'):
        return None
    before, _, after = comment_body.partition('/ask')
    return '/ask' + after + ' \n' + before.strip().lstrip('>')


def handle_line_comments(body: Dict, comment_body: [str, Any]):
    if not comment_body:
        return ""
    start_line = body["comment"]["start_line"] or body["comment"].get("original_start_line")
    end_line = body["comment"]["line"] or body["comment"].get("original_line")
    start_line = end_line if not start_line else start_line
    # Strip only the leading command. str.replace() would also remove "/ask" from
    # inside the question, mangling text such as "/ask how do I call /ask_line?".
    # gitlab_webhook.handle_ask_line() is the reference for this contract.
    question = comment_body.strip().removeprefix('/ask').strip()
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
