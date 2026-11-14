"""One registry for every PR-Agent command and its aliases.

Each entry maps one accepted command spelling to the command
that runs it, and the per-command facts the entry points need, so no entry
point keeps its own copy of the alias lists (see issue #4723). Before this
module, the same names were repeated in ``command2class``
(``pr_agent/agent/pr_agent.py``), ``_AUTO_COMMAND_CHECK_RUNS``
(``pr_agent/servers/github_app.py``), the plain-diff and JSON-output sets
(``pr_agent/cli.py``), and the GitLab ``--comment_id`` rule
(``pr_agent/servers/gitlab_webhook.py``) — and they had already drifted:
``/review_pr``, ``/describe_pr`` and ``/improve_code`` dispatch a command but
got no check run from the GitHub app. The Gerrit ``Action`` enum
(``pr_agent/servers/gerrit_server.py``) remains a separate provider-specific API.

Facts, per spelling:
- ``check_run`` is the name the command passes to ``_publish_check_run`` when
  ``github.publish_as_check_run`` is on. ``None`` preserves the previous
  behaviour for commands that never published one.
- ``takes_comment_id`` marks the commands the GitLab webhook appends
  ``--comment_id=<discussion_id>`` to.
- ``plain_diff_markdown`` / ``json_output`` carry the CLI's ``--output`` and
  ``--json-output`` eligibility (``pr_agent/cli.py``).
"""

from dataclasses import dataclass

from pr_agent.commands.pr_add_docs import PRAddDocs
from pr_agent.commands.pr_config import PRConfig
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_generate_labels import PRGenerateLabels
from pr_agent.commands.pr_help_message import PRHelpMessage
from pr_agent.commands.pr_line_questions import PR_LineQuestions
from pr_agent.commands.pr_questions import PRQuestions
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.commands.pr_similar_issue import PRSimilarIssue
from pr_agent.commands.pr_update_changelog import PRUpdateChangelog


@dataclass(frozen=True)
class CommandSpec:
    """Facts about one accepted command spelling."""

    command: type
    check_run: str | None = None
    takes_comment_id: bool = False
    plain_diff_markdown: bool = False
    json_output: str | None = None  # "review" | "describe" | "labels" | "ask"


COMMAND_SPECS: dict[str, CommandSpec] = {
    "review": CommandSpec(
        PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "review_pr": CommandSpec(
        PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "describe": CommandSpec(
        PRDescription, check_run="describe",
        plain_diff_markdown=True, json_output="describe",
    ),
    "describe_pr": CommandSpec(
        PRDescription, check_run="describe",
        plain_diff_markdown=True, json_output="describe",
    ),
    # /improve runs /review since 0.51 (PRCodeSuggestions stays importable but is no longer dispatched).
    "improve": CommandSpec(
        PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "improve_code": CommandSpec(
        PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "ask": CommandSpec(PRQuestions, takes_comment_id=True, plain_diff_markdown=True, json_output="ask"),
    "ask_question": CommandSpec(PRQuestions, takes_comment_id=True, plain_diff_markdown=True, json_output="ask"),
    "ask_line": CommandSpec(PR_LineQuestions),
    "update_changelog": CommandSpec(PRUpdateChangelog),
    "config": CommandSpec(PRConfig, plain_diff_markdown=True),
    "settings": CommandSpec(PRConfig, plain_diff_markdown=True),
    "help": CommandSpec(PRHelpMessage, plain_diff_markdown=True),
    "similar_issue": CommandSpec(PRSimilarIssue),
    "add_docs": CommandSpec(PRAddDocs),
    "generate_labels": CommandSpec(PRGenerateLabels, json_output="labels"),
    # SECURITY: "/help_docs" is temporarily disabled while the clone-target validation
    # fix is reviewed (see issue #2445). Re-enable by restoring `"help_docs": PRHelpDocs`
    # and its import once the hardening PR is merged.
}


def resolve_command(name: str) -> CommandSpec | None:
    """The spec for a command spelling, tolerating a leading slash and case."""
    return COMMAND_SPECS.get(name.lstrip("/").lower())


def command_check_run(name: str) -> str | None:
    """The check run a command publishes to, if any."""
    spec = resolve_command(name)
    return spec.check_run if spec else None


def command_takes_comment_id(name: str) -> bool:
    """Whether the GitLab webhook appends ``--comment_id`` for this command."""
    spec = resolve_command(name)
    return spec.takes_comment_id if spec else False
