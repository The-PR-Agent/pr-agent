"""One registry for every PR-Agent command and its aliases.

Each entry maps one accepted command spelling to its canonical name, the tool
that runs it, and the per-command facts the entry points need, so no entry
point keeps its own copy of the alias lists (see issue #4723). Before this
module, the same names were repeated in ``command2class``
(``pr_agent/agent/pr_agent.py``), ``_AUTO_COMMAND_CHECK_RUNS``
(``pr_agent/servers/github_app.py``), the plain-diff and JSON-output sets
(``pr_agent/cli.py``), and the GitLab ``--comment_id`` rule
(``pr_agent/servers/gitlab_webhook.py``) — and they had already drifted:
``/review_pr``, ``/describe_pr`` and ``/improve_code`` dispatch a tool but
got no check run from the GitHub app. The Gerrit ``Action`` enum
(``pr_agent/servers/gerrit_server.py``) is intentionally untouched: its
five canonical actions are a pinned API contract
(``tests/unittest/test_gerrit_ask_dispatch.py::test_openapi_does_not_advertise_reflect``),
so accepting aliases there is a separate decision for the maintainers.

Facts, per spelling:
- ``canonical`` names the tool family the spelling belongs to. Dispatch still
  uses the raw spelling: ``auto_review`` runs the reviewer with
  ``is_auto=True`` and ``answer`` runs it with ``is_answer=True``
  (``pr_agent/agent/pr_agent.py``).
- ``check_run`` is the name the tool passes to ``_publish_check_run`` when
  ``github.publish_as_check_run`` is on. ``None`` preserves the previous
  behaviour for commands that never published one (``answer``,
  ``auto_review`` — the dispatcher never calls ``notify`` for it — and the
  rest).
- ``takes_comment_id`` marks the commands the GitLab webhook appends
  ``--comment_id=<discussion_id>`` to.
- ``plain_diff_markdown`` / ``json_output`` carry the CLI's ``--output`` and
  ``--json-output`` eligibility (``pr_agent/cli.py``).
"""

from dataclasses import dataclass

from pr_agent.tools.pr_add_docs import PRAddDocs
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_config import PRConfig
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from pr_agent.tools.pr_help_message import PRHelpMessage
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from pr_agent.tools.pr_questions import PRQuestions
from pr_agent.tools.pr_reviewer import PRReviewer
from pr_agent.tools.pr_similar_issue import PRSimilarIssue
from pr_agent.tools.pr_update_changelog import PRUpdateChangelog


@dataclass(frozen=True)
class CommandSpec:
    """Facts about one accepted command spelling."""

    canonical: str
    tool: type
    check_run: str | None = None
    takes_comment_id: bool = False
    plain_diff_markdown: bool = False
    json_output: str | None = None  # "review" | "improve" | "describe"


COMMAND_SPECS: dict[str, CommandSpec] = {
    "auto_review": CommandSpec("review", PRReviewer, plain_diff_markdown=True),
    "answer": CommandSpec("answer", PRReviewer),
    "review": CommandSpec(
        "review", PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "review_pr": CommandSpec(
        "review", PRReviewer, check_run="review", takes_comment_id=True,
        plain_diff_markdown=True, json_output="review",
    ),
    "describe": CommandSpec(
        "describe", PRDescription, check_run="describe",
        plain_diff_markdown=True, json_output="describe",
    ),
    "describe_pr": CommandSpec(
        "describe", PRDescription, check_run="describe",
        plain_diff_markdown=True, json_output="describe",
    ),
    "improve": CommandSpec(
        "improve", PRCodeSuggestions, check_run="suggestions", takes_comment_id=True,
        plain_diff_markdown=True, json_output="improve",
    ),
    "improve_code": CommandSpec(
        "improve", PRCodeSuggestions, check_run="suggestions", takes_comment_id=True,
        plain_diff_markdown=True, json_output="improve",
    ),
    "ask": CommandSpec("ask", PRQuestions, plain_diff_markdown=True),
    "ask_question": CommandSpec("ask", PRQuestions, plain_diff_markdown=True),
    "ask_line": CommandSpec("ask_line", PR_LineQuestions),
    "update_changelog": CommandSpec("update_changelog", PRUpdateChangelog),
    "config": CommandSpec("config", PRConfig, plain_diff_markdown=True),
    "settings": CommandSpec("config", PRConfig, plain_diff_markdown=True),
    "help": CommandSpec("help", PRHelpMessage, plain_diff_markdown=True),
    "similar_issue": CommandSpec("similar_issue", PRSimilarIssue),
    "add_docs": CommandSpec("add_docs", PRAddDocs),
    "generate_labels": CommandSpec("generate_labels", PRGenerateLabels),
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
