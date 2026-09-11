import argparse
import asyncio
import os
import sys

from pr_agent.agent.pr_agent import PRAgent, commands
from pr_agent.algo.ai_handlers.litellm_helpers import (
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    drain_litellm_callbacks,
    litellm_callbacks_registered,
)
from pr_agent.algo.artifacts import inject_artifact_context
from pr_agent.algo.utils import get_version
from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger, setup_logger

log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)

_PLAIN_DIFF_MARKDOWN_COMMANDS = frozenset({
    "review", "review_pr", "auto_review",
    "describe", "describe_pr",
    "improve", "improve_code",
    "ask", "ask_question",
    "config", "settings", "help",
})
_PLAIN_DIFF_JSON_COMMANDS = frozenset({"review", "review_pr"})
_OUTPUT_OPTIONS = ("--output", "--json-output")


def _resolve_output_option(parser, arg):
    option_text = arg.partition("=")[0]
    if option_text in _OUTPUT_OPTIONS:
        return option_text
    if not option_text.startswith("--") or not parser.allow_abbrev:
        return None

    # argparse has no public API for resolving one option spelling. Reuse its
    # own option table so misplaced options follow the parser's abbreviation
    # rules and future ambiguous prefixes are left untouched.
    matches = parser._get_option_tuples(option_text)
    if len(matches) == 1 and matches[0][1] in _OUTPUT_OPTIONS:
        return matches[0][1]
    return None


def _validate_output_options(parser, args, diff_mode):
    for arg in getattr(args, "rest", []):
        option = _resolve_output_option(parser, arg)
        if option:
            parser.error(
                f"{option} must appear before the command "
                f"(for example: --stdin {option} result review)"
            )

    output = getattr(args, "output", None)
    json_output = getattr(args, "json_output", None)
    for option, value in (("--output", output), ("--json-output", json_output)):
        if value is not None and not value:
            parser.error(f"{option} requires a non-empty path")

    command = args.command.lstrip("/").lower()
    if output is not None:
        if not diff_mode:
            parser.error("--output is only supported in plain-diff mode (--stdin or --diff-file)")
        if command not in _PLAIN_DIFF_MARKDOWN_COMMANDS:
            parser.error(f"--output is not supported for plain-diff command '{command}'")

    if json_output is not None:
        if not diff_mode:
            parser.error("--json-output is only supported in plain-diff mode (--stdin or --diff-file)")
        if command not in _PLAIN_DIFF_JSON_COMMANDS:
            parser.error("--json-output is only supported for plain-diff review commands (review or review_pr)")


def set_parser():
    parser = argparse.ArgumentParser(description='AI based pull request analyzer', usage=
    f"""\
    Usage: cli.py --pr_url=<URL on supported git hosting service> <command> [<args>].
    For example:
    - cli.py --pr_url=... review
    - cli.py --pr_url=... describe
    - cli.py --pr_url=... improve
    - cli.py --pr_url=... ask "write me a poem about this PR"
    - cli.py --issue_url=... similar_issue

    Supported commands:
    - review / review_pr - {COMMAND_DESCRIPTIONS["review"]}

    - ask / ask_question [question] - Ask a question about the PR.

    - describe / describe_pr - {COMMAND_DESCRIPTIONS["describe"]}

    - improve / improve_code - {COMMAND_DESCRIPTIONS["improve"]}
    Extended mode ('improve --extended') employs several calls, and provides a more thorough feedback

    - update_changelog - Update the changelog based on the PR's contents.

    - add_docs

    - generate_labels

    Configuration:
    To edit any configuration parameter from 'configuration.toml', just add -config_path=<value>.
    For example: 'python cli.py --pr_url=... review --pr_reviewer.extra_instructions="focus on the file: ..."'
    """)
    parser.add_argument('--version', action='version', version=f'pr-agent {get_version()}')
    parser.add_argument('--pr_url', type=str, help='The URL of the PR to review', default=None)
    parser.add_argument('--issue_url', type=str, help='The URL of the Issue to review', default=None)
    parser.add_argument('--config-branch', type=str, help='Git branch to load .pr_agent.toml from', default=None)
    parser.add_argument(
        "--extra_config_url",
        type=str,
        default=os.environ.get("PR_AGENT_EXTRA_CONFIG_URL"),
        help=(
            "URL or local path of an additional .pr_agent.toml to merge before the "
            "repo-local config (e.g. shared/org defaults). Accepts http(s):// URLs or "
            "a filesystem path. For private endpoints, set PR_AGENT_EXTRA_CONFIG_AUTH_HEADER "
            "(e.g. 'PRIVATE-TOKEN: <token>' or 'JOB-TOKEN: $CI_JOB_TOKEN'). "
            "Repo-local .pr_agent.toml overrides values set here."
        ),
    )
    parser.add_argument("--diff-file", dest="diff_file", type=str, default=None,
                        help="Path to a unified diff file to review (plain-diff local mode)")
    parser.add_argument("--stdin", action="store_true", default=False,
                        help="Read a unified diff from stdin (plain-diff local mode)")
    parser.add_argument("--output", dest="output", type=str, default=None,
                        help=("Write Plain Diff Markdown output to this file "
                              "(place before the command)"))
    parser.add_argument("--json-output", dest="json_output", type=str, default=None,
                        help=("Write a Plain Diff review and token usage to this JSON file "
                              "(place before the review command)"))
    parser.add_argument('command', type=str, help='The', choices=commands, default='review')
    parser.add_argument('rest', nargs=argparse.REMAINDER, default=[])
    return parser


def run_command(pr_url, command):
    # Preparing the command
    run_command_str = f"--pr_url={pr_url} {command.lstrip('/')}"
    args = set_parser().parse_args(run_command_str.split())

    # Run the command. Feedback will appear in GitHub PR comments
    run(args=args)


def run(inargs=None, args=None):
    parser = set_parser()
    if not args:
        args = parser.parse_args(inargs)
    diff_mode = getattr(args, "stdin", False) or getattr(args, "diff_file", None)
    if diff_mode and args.stdin and args.diff_file:
        parser.error("--stdin and --diff-file are mutually exclusive")
    _validate_output_options(parser, args, diff_mode)
    if diff_mode:
        if args.diff_file:
            try:
                with open(args.diff_file, "r", encoding="utf-8") as fh:
                    diff_content = fh.read()
            except OSError as e:
                parser.error(f"Could not read --diff-file '{args.diff_file}': {e}")
            except UnicodeDecodeError as e:
                parser.error(f"--diff-file '{args.diff_file}' is not valid UTF-8 text: {e}")
        else:
            diff_content = sys.stdin.read()
        if not diff_content.strip():
            parser.error("No diff content received (empty stdin/file)")
        get_settings().set("config.git_provider", "plain-diff")
        get_settings().set("plain_diff.content", diff_content)
        get_settings().set("plain_diff.output_path", getattr(args, "output", None))
        get_settings().set("plain_diff.json_output_path", getattr(args, "json_output", None))
        # Plain-diff mode's whole purpose is to emit the result to stdout/--output, so
        # force publishing on even if a config/env set publish_output=false.
        get_settings().set("config.publish_output", True)
    elif not args.pr_url and not args.issue_url:
        parser.print_help()
        return

    command = args.command.lower()
    get_settings().set("CONFIG.CLI_MODE", True)
    # Strip each candidate independently so a whitespace-only CLI value doesn't
    # short-circuit the PR_AGENT_CONFIG_BRANCH env fallback before precedence.
    cli_branch = (getattr(args, "config_branch", None) or "").strip()
    env_branch = (os.environ.get("PR_AGENT_CONFIG_BRANCH") or "").strip()
    # Always reconcile CONFIG.CONFIG_BRANCH with the current invocation so a value
    # set by an earlier run() call in the same process can't leak into a later one
    # (get_settings() is a process-wide singleton).
    get_settings().set("CONFIG.CONFIG_BRANCH", cli_branch or env_branch or None)
    # Always reconcile CONFIG.EXTRA_CONFIG_URL with the current invocation so a
    # previously-set value from an earlier run() call in the same process can't
    # leak into a later one (get_settings() is a process-wide singleton).
    get_settings().set("CONFIG.EXTRA_CONFIG_URL", getattr(args, "extra_config_url", None))
    # A CI artifact (see [artifacts]) reaches the prompts from the environment or the settings files,
    # the same way it does under the GitHub Action, so any pipeline that runs the CLI can supply one.
    inject_artifact_context()

    async def inner():
        if args.issue_url:
            result = await asyncio.create_task(PRAgent().handle_request(args.issue_url, [command] + args.rest))
        else:
            target = args.pr_url if args.pr_url else "local_diff"
            result = await asyncio.create_task(PRAgent().handle_request(target, [command] + args.rest))

        # litellm defers its success/failure callbacks onto the event loop, which
        # asyncio.run() below tears down the moment this coroutine returns. Give
        # them a chance to run first, or they are silently dropped.
        if litellm_callbacks_registered():
            get_logger().debug("Waiting for event queue to complete")
            await drain_litellm_callbacks(
                get_settings().litellm.get("callback_timeout_seconds", DEFAULT_CALLBACK_TIMEOUT_SECONDS)
            )

        return result

    result = asyncio.run(inner())
    if not result:
        parser.print_help()


if __name__ == '__main__':
    run()
