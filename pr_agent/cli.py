import argparse
import asyncio
import copy
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from urllib.parse import urlsplit

import yaml
from starlette_context import context, request_cycle_context

from pr_agent.agent.command_registry import COMMAND_SPECS
from pr_agent.agent.pr_agent import PRAgent, commands, parse_command
from pr_agent.agent.request_policy import RequestOutcome
from pr_agent.algo.ai_handlers.litellm_helpers import (
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    drain_litellm_callbacks,
    litellm_callbacks_registered,
)
from pr_agent.algo.artifacts import inject_artifact_context
from pr_agent.algo.review_json_output import (
    hosted_markdown_artifact,
    hosted_markdown_config_error,
    hosted_markdown_export,
    review_json_output_path,
)
from pr_agent.algo.run_details import command_failed, init_run_details
from pr_agent.algo.run_output import get_version
from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_settings
from pr_agent.doctor import DOCTOR_COMMANDS, run_doctor_checks
from pr_agent.log import get_logger, setup_logger

log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)

# --output / --json-output eligibility, derived from the command registry so a
# new alias is eligible wherever its canonical command is.
_PLAIN_DIFF_MARKDOWN_COMMANDS = frozenset(
    name for name, spec in COMMAND_SPECS.items() if spec.plain_diff_markdown
)
_JSON_REVIEW_COMMANDS = frozenset(
    name for name, spec in COMMAND_SPECS.items() if spec.json_output == "review"
)
_JSON_DESCRIBE_COMMANDS = frozenset(
    name for name, spec in COMMAND_SPECS.items() if spec.json_output == "describe"
)
_JSON_LABEL_COMMANDS = frozenset(
    name for name, spec in COMMAND_SPECS.items() if spec.json_output == "labels"
)
_JSON_ASK_COMMANDS = frozenset(
    name for name, spec in COMMAND_SPECS.items() if spec.json_output == "ask"
)
_OUTPUT_OPTIONS = ("--output", "--json-output")
_CLI_CONTEXT_CACHES = (
    "git_provider", "repo_settings", "git_files", "diff_files", "authenticated_provider_settings",
    "extra_config_payloads",
)


@contextmanager
def _cli_settings_scope():
    """Give one CLI invocation its own settings and target-derived caches."""
    try:
        scope_data = context.copy()
    except Exception:
        scope_data = {}
    scope_data["settings"] = copy.deepcopy(get_settings())
    for cache_key in _CLI_CONTEXT_CACHES:
        scope_data.pop(cache_key, None)

    cm = request_cycle_context(scope_data)
    cm.__enter__()
    try:
        yield
    finally:
        # Reset the context even when BaseException would skip the bare generator's cleanup.
        cm.__exit__(None, None, None)


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
        if diff_mode:
            if command not in _PLAIN_DIFF_MARKDOWN_COMMANDS:
                parser.error(f"--output is not supported for plain-diff command '{command}'")
        else:
            try:
                pr_url = urlsplit(args.pr_url) if args.pr_url else None
            except ValueError:
                pr_url = None
            if args.issue_url or not pr_url or pr_url.scheme not in ("http", "https") or not pr_url.netloc:
                parser.error("--output requires plain-diff mode or a hosted --pr_url")
            if command not in _JSON_REVIEW_COMMANDS:
                parser.error(f"--output with --pr_url is not supported for command '{command}'")

    if json_output is not None:
        pr_commands = _JSON_DESCRIBE_COMMANDS | _JSON_LABEL_COMMANDS | _JSON_ASK_COMMANDS
        if command not in _JSON_REVIEW_COMMANDS | pr_commands:
            parser.error(
                "--json-output is only supported for review, improve, describe, generate_labels, or ask commands"
            )
        if not diff_mode and (not args.pr_url or args.issue_url):
            parser.error("--json-output requires --pr_url, --stdin, or --diff-file")
    if output is not None and json_output is not None:
        try:
            same_destination = os.path.normcase(os.path.realpath(output)) == os.path.normcase(
                os.path.realpath(json_output)
            )
        except (OSError, ValueError):
            same_destination = False  # Invalid paths will be handled by the output write.
        if not same_destination:
            try:
                same_destination = os.path.samefile(output, json_output)
            except (OSError, ValueError):
                pass  # Missing or invalid paths cannot be compared as existing files.
        if same_destination:
            parser.error("--output and --json-output must use different files")


def _encode_plain_diff_context_args(args):
    """Keep Plain Diff context as text when command arguments are parsed as YAML."""
    encoded = []
    for arg in args:
        if arg.startswith("--") and "=" in arg:
            key, value = arg.split("=", 1)
            if key[2:].lower() in ("plain_diff.title", "plain_diff.description"):
                quoted = yaml.safe_dump(value, allow_unicode=True, default_style='"').strip()
                arg = f"{key}={quoted}"
        encoded.append(arg)
    return encoded


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

    - doctor - Check the model/key, ping the model, and check basic settings for enabled output sinks without a PR.

    - ask / ask_question [question] - Ask a question about the PR.

    - describe / describe_pr - {COMMAND_DESCRIPTIONS["describe"]}

    - improve / improve_code - {COMMAND_DESCRIPTIONS["improve"]}

    - update_changelog - Update the changelog based on the PR's contents.

    - add_docs

    - generate_labels

    Configuration:
    To edit a configuration parameter from 'configuration.toml', just add --<section>.<key>=<value>.
    Host-only settings (provider credentials and other deployment-critical keys) are rejected; set them
    in a host configuration file or through environment variables.
    Some settings are rejected as command arguments but accepted in repository configuration; set those
    in the repository's .pr_agent.toml instead. The local git provider and plain-diff mode read no
    repository configuration, so set those in host configuration there.
    For doctor, overrides after the command are ignored and those before it are rejected;
    use a host configuration file or environment variables instead.
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
                        help=("Write Plain Diff Markdown or an unpublished hosted PR review "
                              "to this file (place before the command)"))
    parser.add_argument("--json-output", dest="json_output", type=str, default=None,
                        help=("Write supported command results and token usage to this JSON file "
                              "(place before the command)"))
    parser.add_argument('command', type=str, help='The', choices=[*commands, *DOCTOR_COMMANDS], default='review')
    parser.add_argument('rest', nargs=argparse.REMAINDER, default=[])
    return parser


def run_command(pr_url, command):
    # Preparing the command
    command_args = parse_command(command.lstrip('/'))
    args = set_parser().parse_args([f"--pr_url={pr_url}", *command_args])

    # Run the command. Feedback will appear in GitHub PR comments
    return run(args=args)


def run(inargs=None, args=None):
    parser = set_parser()
    if not args:
        args = parser.parse_args(inargs)
    diff_mode = getattr(args, "stdin", False) or getattr(args, "diff_file", None)
    if diff_mode and args.stdin and args.diff_file:
        parser.error("--stdin and --diff-file are mutually exclusive")
    _validate_output_options(parser, args, diff_mode)
    if args.command.lower() in DOCTOR_COMMANDS:
        # Preflight checks need neither a PR nor a diff; run them in the CLI settings
        # scope so environment overrides and config files apply as in a real run.
        with _cli_settings_scope():
            get_settings().set("CONFIG.CLI_MODE", True)
            return asyncio.run(run_doctor_checks())
    diff_content = None
    if diff_mode:
        if args.diff_file:
            try:
                with open(args.diff_file, encoding="utf-8") as fh:
                    diff_content = fh.read()
            except OSError as e:
                parser.error(f"Could not read --diff-file '{args.diff_file}': {e}")
            except UnicodeDecodeError as e:
                parser.error(f"--diff-file '{args.diff_file}' is not valid UTF-8 text: {e}")
        else:
            diff_content = sys.stdin.read()
        if not diff_content.strip():
            parser.error("No diff content received (empty stdin/file)")
    elif not args.pr_url and not args.issue_url:
        parser.print_help()
        return

    hosted_markdown_output = not diff_mode and getattr(args, "output", None) is not None
    with _cli_settings_scope():
        if diff_mode:
            get_settings().set("config.git_provider", "plain-diff")
            get_settings().set("plain_diff.content", diff_content)
            get_settings().set("plain_diff.output_path", getattr(args, "output", None))
            get_settings().set("plain_diff.json_output_path", getattr(args, "json_output", None))
            # Plain-diff mode's whole purpose is to emit the result to stdout/--output, so
            # force publishing on even if a config/env set publish_output=false.
            get_settings().set("config.publish_output", True)

        command = args.command.lower()
        settings = get_settings()
        if hosted_markdown_output:
            settings.set("config.publish_output", False)
        get_settings().set("CONFIG.CLI_MODE", True)
        # Strip each candidate independently so a whitespace-only CLI value doesn't
        # short-circuit the PR_AGENT_CONFIG_BRANCH env fallback before precedence.
        cli_branch = (getattr(args, "config_branch", None) or "").strip()
        env_branch = (os.environ.get("PR_AGENT_CONFIG_BRANCH") or "").strip()
        get_settings().set("CONFIG.CONFIG_BRANCH", cli_branch or env_branch or None)
        get_settings().set("CONFIG.EXTRA_CONFIG_URL", getattr(args, "extra_config_url", None))
        async def inner():
            # Plain-diff reviews (and /improve, which runs /review) write through their provider;
            # describe, labels, and ask use the shared JSON writer in both modes.
            command_json_output = getattr(args, "json_output", None) if (
                not diff_mode or command in _JSON_DESCRIBE_COMMANDS | _JSON_LABEL_COMMANDS | _JSON_ASK_COMMANDS
            ) else None
            output_token = review_json_output_path.set(command_json_output)
            hosted_token = hosted_markdown_export.set(hosted_markdown_output)
            artifact_token = hosted_markdown_artifact.set(None)
            config_error_token = hosted_markdown_config_error.set(False)
            try:
                return await run_inner()
            finally:
                hosted_markdown_config_error.reset(config_error_token)
                hosted_markdown_artifact.reset(artifact_token)
                hosted_markdown_export.reset(hosted_token)
                review_json_output_path.reset(output_token)

        async def run_inner():
            # A CI artifact (see [artifacts]) reaches prompts from the environment or settings files,
            # the same way it does under the GitHub Action. Each asyncio.run gets a fresh task context.
            inject_artifact_context()
            init_run_details()
            # Read the verdict in the same task where the request installs its collector.
            command_args = _encode_plain_diff_context_args(args.rest) if diff_mode else list(args.rest)
            if hosted_markdown_output:
                # Repo settings and user arguments are applied inside PRAgent. The final
                # override keeps the review unpublished regardless of their values.
                command_args.append("--config.publish_output=false")
            if args.issue_url:
                result = await PRAgent().handle_request(args.issue_url, [command] + command_args)
            else:
                target = args.pr_url or "local_diff"
                result = await PRAgent().handle_request(target, [command] + command_args)
            if result is RequestOutcome.SKIPPED:
                # No command ran, so there are no command callbacks or failures to drain.
                return RequestOutcome.SKIPPED, False, None

            # litellm defers its success/failure callbacks onto the event loop, which
            # asyncio.run() below tears down the moment this coroutine returns. Give
            # them a chance to run first, or they are silently dropped.
            if litellm_callbacks_registered():
                get_logger().debug("Waiting for event queue to complete")
                await drain_litellm_callbacks(
                    get_settings().litellm.get("callback_timeout_seconds", DEFAULT_CALLBACK_TIMEOUT_SECONDS)
                )

            return (
                result,
                command_failed() or (hosted_markdown_output and hosted_markdown_config_error.get()),
                hosted_markdown_artifact.get(),
            )

        result, recorded_failure, artifact = asyncio.run(inner())
        if not result and result is not RequestOutcome.SKIPPED:
            parser.print_help()
        label_json_required = bool(
            getattr(args, "json_output", None) is not None and command in _JSON_LABEL_COMMANDS
        )
        ask_json_required = bool(
            getattr(args, "json_output", None) is not None and command in _JSON_ASK_COMMANDS
        )
        json_artifact_required = label_json_required or ask_json_required or bool(
            diff_mode and getattr(args, "json_output", None) is not None
            and command in _JSON_REVIEW_COMMANDS | _JSON_DESCRIBE_COMMANDS
        )
        if (settings.config.get("propagate_command_errors", False) or json_artifact_required) and (
            result is False or recorded_failure
            or ((label_json_required or ask_json_required) and result is RequestOutcome.SKIPPED)
        ):
            if result is not False and recorded_failure:
                get_logger().warning("Command reported success but recorded a failure; exiting with an error")
            return 1
        if hosted_markdown_output:
            if recorded_failure:
                get_logger().error("Hosted review reported an error; Markdown output was not written")
                return 1
            if result is not True or not isinstance(artifact, str) or not artifact.strip():
                get_logger().error("Hosted review did not produce a successful Markdown artifact")
                return 1
            if args.json_output is not None:
                # A JSON file created during review may alias the Markdown path only now.
                try:
                    same_destination = os.path.samefile(args.output, args.json_output)
                except (OSError, ValueError):
                    same_destination = False
                if same_destination:
                    get_logger().error("--output and --json-output resolve to the same file")
                    return 1
            temporary_output = None
            try:
                # Replace the symlink target, not the link, and preserve an existing file's mode.
                output_path = os.path.realpath(args.output)
                try:
                    existing_mode = stat.S_IMODE(os.stat(output_path).st_mode)
                except FileNotFoundError:
                    existing_mode = None
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=os.path.dirname(output_path),
                    prefix=".pr-agent-review-", suffix=".tmp", delete=False,
                ) as fh:
                    temporary_output = fh.name
                    fh.write(artifact)
                if existing_mode is not None:
                    os.chmod(temporary_output, existing_mode)
                os.replace(temporary_output, output_path)
            except (OSError, UnicodeError, ValueError) as e:
                get_logger().error(f"Failed to write review to {args.output}: {e}")
                return 1
            finally:
                if temporary_output is not None:
                    try:
                        os.unlink(temporary_output)
                    except FileNotFoundError:
                        # os.replace() already moved the temporary file into place.
                        pass
                    except OSError as e:
                        get_logger().warning(f"Failed to remove temporary review file {temporary_output}: {e}")


if __name__ == '__main__':
    raise SystemExit(run())
