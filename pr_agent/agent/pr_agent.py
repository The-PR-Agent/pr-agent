import asyncio
import copy
import json
import shlex
from contextlib import asynccontextmanager, contextmanager
from functools import partial

import dynaconf
from opentelemetry.trace import StatusCode
from starlette_context import context, request_cycle_context

from pr_agent.agent.command_registry import COMMAND_SPECS
from pr_agent.agent.request_policy import RequestOutcome, enforce_request_policy
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.command_words import _split_command
from pr_agent.algo.comment_identity import (
    add_comment_identity,
    comment_matches_identity,
)
from pr_agent.algo.i18n import t
from pr_agent.algo.run_details import command_failed, get_run_details, init_run_details
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.algo.utils import _fix_key_value, update_settings_from_args
from pr_agent.commands.pr_code_suggestions import PRCodeSuggestions
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_questions import PRQuestions
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import (
    IncompleteBitbucketPullRequestFilesError as _IncompleteBitbucketPullRequestFilesError,
)
from pr_agent.git_providers.git_provider import IncompleteProviderPullRequestFilesError
from pr_agent.git_providers.git_provider import IncompletePullRequestFilesError as _IncompletePullRequestFilesError
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import get_logger
from pr_agent.telemetry.meter import get_ai_calls_counter, get_commands_counter, get_tokens_counter
from pr_agent.telemetry.shutdown import flush_telemetry
from pr_agent.telemetry.tracer import get_tracer

# Keep the established import path available to integrations and tests while the
# shared handler works against the provider-neutral base exception.
IncompleteBitbucketPullRequestFilesError = _IncompleteBitbucketPullRequestFilesError
IncompletePullRequestFilesError = _IncompletePullRequestFilesError

# The single source of truth for commands and aliases is
# pr_agent/agent/command_registry.py; dispatch reads the command mapping from it.
command2class = {name: spec.command for name, spec in COMMAND_SPECS.items()}

commands = list(command2class.keys())
_IMPROVE_ALIASES = frozenset({"improve", "improve_code"})

def publish_incomplete_files_comment(
    pr_url: str, error: IncompleteProviderPullRequestFilesError
) -> None:
    """Publish one trusted, sanitized provider notice without replacing the primary failure."""
    try:
        _publish_incomplete_files_comment(pr_url, error)
    except Exception:
        # Preserve the original completeness failure by containing every
        # ordinary provider or rendering failure from this secondary notice.
        get_logger().exception("Failed to prepare the incomplete-files notice")


def _publish_incomplete_files_comment(
    pr_url: str, error: IncompleteProviderPullRequestFilesError
) -> None:
    if not get_settings().get("CONFIG.PUBLISH_OUTPUT", True):
        return

    try:
        provider = get_git_provider_with_context(pr_url)
    except Exception:
        get_logger().exception("Failed to get a provider for the incomplete-files notice")
        return

    try:
        comments = provider.get_issue_comments_newest_first()
        for comment in comments:
            try:
                body = provider._get_comment_body(comment)
            except Exception:
                # Ignore comments whose bodies cannot be read. Continue looking
                # for a verifiable PR-Agent marker and publish if none can be
                # confirmed.
                get_logger().warning(
                    "Failed to read an existing incomplete-files notice; continuing"
                )
                continue
            if not comment_matches_identity(body, error.notice_marker):
                continue
            try:
                if provider.is_comment_authored_by_pr_agent(comment):
                    return
            except Exception:
                get_logger().exception("Failed to verify the author of an incomplete-files notice")
    except Exception:
        get_logger().exception("Failed to inspect existing incomplete-files notices")

    body = add_comment_identity(
        error.notice,
        error.notice_marker,
        provider,
    )
    try:
        provider.publish_comment(body)
    except Exception:
        get_logger().exception("Failed to publish the incomplete-files notice")

def parse_command(command: str) -> list[str]:
    """Normalize configured command strings to argv without applying settings."""
    tokens = _split_command(command)
    if not tokens:
        return []

    (action, _), *token_args = tokens
    args = []
    for argument, value_was_quoted in token_args:
        if value_was_quoted and argument.startswith("--") and "=" in argument:
            key, value = argument.split("=", 1)
            # A quoted list such as --ignore.glob="['*.py']" is the user's explicit list value,
            # so JSON-encoding it would collapse it into a string and silently disable list
            # settings. Other quoted values (except opaque FILE_NAME paths) are
            # encoded so update_settings_from_args cannot coerce "true" to a
            # boolean or a colon-prefixed value to a mapping.
            if key.lstrip("-").upper() != "FILE_NAME":
                _, parsed = _fix_key_value(key, value)
                if not isinstance(parsed, list):
                    argument = f"{key}={json.dumps(value, ensure_ascii=False)}"
        args.append(argument)
    return [action] + args


def _reencode_quoted_setting_args(command: str, args: list[str]) -> list[str]:
    """Keep quoted setting values as strings in raw command strings.

    The raw request parser strips quotes before ``update_settings_from_args``
    hands values to ``yaml.safe_load``, which coerces ``"true"`` to a boolean or
    a colon-prefixed string to a mapping. When the original command quoted a
    value that YAML would coerce to a non-string, re-encode it as JSON so the
    applied setting keeps the string the user typed, matching ``parse_command``.
    Plain scalars such as ``--pr_reviewer.num_max_findings=3`` keep their normal
    type conversion. Quoted lists keep their list type.
    Match quote flags by argument position so repeated values retain their own types.
    Treat surrounding single quotes as delimiters only for raw FILE_NAME arguments,
    where quoted provider filenames do not pass through this parser.
    """
    try:
        tokens = _split_command(command, single_quotes=False)[1:]
    except ValueError:
        tokens = []
    encoded = []
    for index, argument in enumerate(args):
        value_was_quoted = index < len(tokens) and tokens[index][0] == argument and tokens[index][1]
        if argument.startswith("--") and "=" in argument:
            key, value = argument.split("=", 1)
            if key.lstrip("-").upper() == "FILE_NAME":
                if not value_was_quoted and len(value) >= 2 and value[0] == value[-1] == "'":
                    argument = f"{key}={value[1:-1]}"
            elif value_was_quoted:
                _, parsed = _fix_key_value(key, value)
                if not isinstance(parsed, (str, list)):
                    argument = f"{key}={json.dumps(value, ensure_ascii=False)}"
        encoded.append(argument)
    return encoded


def _validation_args(args: list[str]) -> list[str]:
    """Project setting arguments to their keys for command-line validation.

    A mapping value sets many keys at once, so it is kept whole and every nested
    ``section.key`` path is validated instead of only the section before ``=``.
    """
    return [
        argument if CliArgs.is_mapping_arg(argument) else argument.split("=", 1)[0]
        for argument in args
    ]


def prepare_command(command: str) -> list[str]:
    """Apply validated automatic overrides and retain them for dispatch.

    Apply settings now so they can control repository loading. Return the same
    argv so ``PRAgent`` reapplies overrides after repository settings are loaded.
    Quoted setting values retain their string type; quoted lists keep their list type.
    """
    command_args = parse_command(command)
    if not command_args:
        return []
    action, *args = command_args
    kept, rejected = [], []
    for argument in args:
        # Validate the key only. The value is free text - a review instruction may legitimately
        # mention openai.key or config.url - and only the key can actually set a setting.
        is_allowed, offending_param = CliArgs.validate_user_args(_validation_args([argument]))
        if is_allowed:
            kept.append(argument)
        else:
            rejected.append(offending_param)
    if rejected:
        get_logger().error(
            "Dropping auto-command argument(s) targeting forbidden param(s): "
            + ", ".join(f"'{param}'" for param in rejected))
    settings = get_settings()
    original_language = settings.config.get("response_language", "en-us")
    try:
        update_settings_from_args(kept)
    finally:
        # Apply the requested response language only during command execution.
        if settings.config.get("response_language", "en-us") != original_language:
            settings.set("CONFIG.RESPONSE_LANGUAGE", original_language)
    return [action] + kept


def _record_token_metrics(action: str, git_provider: str) -> None:
    """Export the run's token usage through the OTel counters, if any was collected.

    Repo and PR stay out of the labels on purpose: they are high-cardinality, the same
    reason the command counter omits them. Zero values are skipped, so a provider that
    reports no usage adds nothing.
    """
    details = get_run_details()
    if details is None:
        return
    labels = {
        "pr_agent.command": action,
        "vcs.provider.name": git_provider,
        "pr_agent.fallback_used": details.fallback_used,
    }
    tokens_counter = get_tokens_counter()
    for token_type, count in (
        ("input", details.prompt_tokens),
        ("output", details.completion_tokens),
        ("cache_read", details.cache_read_tokens),
        ("cache_creation", details.cache_creation_tokens),
    ):
        if count:
            tokens_counter.add(count, {**labels, "gen_ai.token.type": token_type})
    if details.num_ai_calls:
        get_ai_calls_counter().add(details.num_ai_calls, labels)


def _normalize_publish_output_setting() -> None:
    """Honor false-like strings without changing explicitly empty or invalid setting defaults."""
    settings = get_settings()
    value = settings.config.publish_output
    settings.set("CONFIG.PUBLISH_OUTPUT", _as_bool_setting(value, default=bool(value)))


@contextmanager
def response_language_instructions():
    """Apply generated locale guidance only while a command is running."""
    settings = get_settings()
    originals = []
    try:
        response_language = settings.config.get("response_language", "en-us")
        if not isinstance(response_language, str) or not response_language.strip():
            get_logger().warning("Ignoring response_language: expected a non-empty locale string")
            response_language = "en-us"
        if response_language.lower() != "en-us":
            get_logger().info(f"User has set the response language to: {response_language}")
            language_instruction = (
                "Your response MUST be written in the language corresponding "
                f"to locale code: '{response_language}'. This is crucial. "
                "Keep schema control values (such as 'No', 'Yes', 'None', "
                "'false') in their original English form and do not translate them."
            )
            for key in settings:
                section = settings.get(key)
                if not isinstance(section, dynaconf.DataDict) or not hasattr(section, "extra_instructions"):
                    continue
                original = section.extra_instructions
                if language_instruction not in str(original):
                    originals.append((section, original))
                    section.extra_instructions = (
                        f"{original}\n======\n\nIn addition, {language_instruction}"
                        if original else language_instruction
                    )
        yield
    finally:
        for section, original in originals:
            section.extra_instructions = original


_command_initialization_locks = {}


@asynccontextmanager
async def _serialize_command_initialization(pr_url):
    """Keep same-PR initialization sequential without occupying waiting worker threads."""
    key = (asyncio.get_running_loop(), pr_url)
    lock, users = _command_initialization_locks.get(key, (asyncio.Lock(), 0))
    _command_initialization_locks[key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = _command_initialization_locks[key]
        if users == 1:
            del _command_initialization_locks[key]
        else:
            _command_initialization_locks[key] = (lock, users - 1)


class PRAgent:
    def __init__(self, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.ai_handler = ai_handler  # handler factory passed to each command when it is instantiated

    async def _create_command(self, command_class, pr_url, **kwargs):
        """Offload eligible command setup while preserving its cancellation boundary."""
        if get_settings() is global_settings or command_class not in (
            PRReviewer, PRDescription, PRCodeSuggestions, PRQuestions
        ):
            # Preserve shared settings and other commands' existing initialization
            # (some constructors also update external indexes).
            return command_class(pr_url, ai_handler=self.ai_handler, **kwargs)

        async with _serialize_command_initialization(pr_url):
            if self.ai_handler is not LiteLLMAIHandler:
                # Injected factories may require the calling event loop.
                return command_class(pr_url, ai_handler=self.ai_handler, **kwargs)

            task = asyncio.create_task(asyncio.to_thread(command_class, pr_url, ai_handler=self.ai_handler, **kwargs))
            cancelled = None
            while True:
                try:
                    command = await asyncio.shield(task)
                    break
                except asyncio.CancelledError as error:
                    if task.cancelled():
                        raise
                    # A thread cannot be stopped by cancelling its awaiter. Finish
                    # setup before restoring settings or starting another command.
                    cancelled = error
                except Exception:
                    if cancelled is not None:
                        raise cancelled from None
                    raise
            if cancelled is not None:
                raise cancelled
            return command

    async def _handle_request(
        self, pr_url, request, notify=None, propagate_command_errors: bool | None = None
    ) -> bool | RequestOutcome:
        # Exceptions raised inside are caught below, but a BaseException (e.g. the
        # CancelledError a webhook timeout raises) still escapes the span, and the SDK
        # would auto-record its message and stacktrace — request content, so opt-in.
        record_details = _as_bool_setting(
            get_settings().get("OTEL.INCLUDE_ERROR_DETAILS", False), setting_name="OTEL.INCLUDE_ERROR_DETAILS"
        )
        with get_tracer().start_as_current_span(
            "pr_agent.command",
            record_exception=record_details,
            set_status_on_exception=record_details,
        ) as span:
            if _as_bool_setting(get_settings().get("OTEL.INCLUDE_PR_URL", False), setting_name="OTEL.INCLUDE_PR_URL"):
                span.set_attribute("pr_agent.pr_url", pr_url)
            try:
                if propagate_command_errors is None:
                    return await self._run_command(pr_url, request, notify, span)
                try:
                    context["settings"]
                except Exception:
                    # Create request-local settings before awaiting commands outside middleware.
                    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
                        return await self._run_command(
                            pr_url, request, notify, span, propagate_command_errors=propagate_command_errors
                        )
                return await self._run_command(
                    pr_url, request, notify, span, propagate_command_errors=propagate_command_errors
                )
            except Exception as e:
                get_logger().exception("Failed to process the command.")
                if isinstance(e, IncompleteProviderPullRequestFilesError):
                    await asyncio.to_thread(publish_incomplete_files_comment, pr_url, e)
                # Status carries no description: it is free text, and the exception
                # message can embed PR URLs, repo names, or other request content.
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", type(e).__name__)
                if record_details:
                    span.set_attribute("error.message", str(e))
                    span.record_exception(e)
                return False

    async def _run_command(
        self, pr_url, request, notify, span, propagate_command_errors: bool | None = None
    ) -> bool | RequestOutcome:
        # Evaluate repository policy before command overrides, notifications or commands.
        apply_repo_settings(pr_url)
        if enforce_request_policy(pr_url) is False:
            span.set_attribute("pr_agent.request.ignored", True)
            return RequestOutcome.SKIPPED

        if isinstance(request, str):
            lexer = shlex.shlex(request, posix=True)
            lexer.whitespace_split = True
            # Keep apostrophes literal without adding backslashes inside double quotes.
            lexer.quotes = '"'
            # Treat "#" as ordinary text. shlex drops it and everything after it as a shell
            # comment, which silently truncated questions such as "/ask what does #123 do?".
            # This input is a single already-parsed command, never a shell script.
            lexer.commenters = ''
            action, *raw_args = list(lexer)
            args = _reencode_quoted_setting_args(request, raw_args)
        else:
            action, *raw_args = request
            args = raw_args

        # validate args
        is_valid, arg = CliArgs.validate_user_args(_validation_args(raw_args))
        if not is_valid:
            get_logger().error(
                f"CLI argument for param '{arg}' is forbidden. Use instead a configuration file."
            )
            span.set_status(StatusCode.ERROR)
            span.set_attribute("error.type", "invalid_argument")
            span.set_attribute("error.argument", arg)
            return False

        # Keep command-level language overrides from affecting later commands.
        settings = get_settings()
        previous_response_language = settings.config.get("response_language", "en-us")
        try:
            args = update_settings_from_args(args)
        except Exception:
            if settings.config.get("response_language", "en-us") != previous_response_language:
                settings.set("CONFIG.RESPONSE_LANGUAGE", previous_response_language)
            raise
        _normalize_publish_output_setting()
        language_overridden = settings.config.get("response_language", "en-us") != previous_response_language

        try:
            action = action.lstrip("/").lower()

            span.set_attribute("pr_agent.args_count", len(args))
            _git_provider = get_settings().config.git_provider
            span.set_attribute("vcs.provider.name", _git_provider)

            if action not in command2class:
                get_logger().warning(f"Unknown command: {action}")
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", "unknown_command")
                if _as_bool_setting(
                    get_settings().get("OTEL.INCLUDE_ERROR_DETAILS", False), setting_name="OTEL.INCLUDE_ERROR_DETAILS"
                ):
                    span.set_attribute("error.message", f"Unknown command: {action}")
                if language_overridden:
                    settings.set("CONFIG.RESPONSE_LANGUAGE", previous_response_language)
                return False

            # Only after validation: an unknown action is arbitrary user input and
            # must not become a span name, span attribute, or metric label.
            span.update_name(f"pr_agent {action}")
            span.set_attribute("pr_agent.command", action)
            get_commands_counter().add(1, {"pr_agent.command": action, "vcs.provider.name": _git_provider})

            # Install a fresh collector at the per-command boundary so the finally block
            # exports this command's usage and never repeats or inherits a prior command's
            # counts. Commands that run their own collector (e.g. /review) replace it on entry.
            init_run_details()
            if propagate_command_errors is not None:
                # Apply this after repository and command settings so callers that require an honest
                # result cannot be overridden by either source. Restore it below for request isolation.
                previous_propagation = settings.get("CONFIG.PROPAGATE_COMMAND_ERRORS", False)
                settings.set("CONFIG.PROPAGATE_COMMAND_ERRORS", propagate_command_errors)
        except Exception:
            if language_overridden:
                settings.set("CONFIG.RESPONSE_LANGUAGE", previous_response_language)
            raise

        try:
            with response_language_instructions(), get_logger().contextualize(command=action, pr_url=pr_url):
                get_logger().info("PR-Agent request handler started", analytics=True)
                if notify:
                    notify()

                command_kwargs = {}
                if action in _IMPROVE_ALIASES:
                    get_logger().warning(f"/{action} now runs /review")
                    command_kwargs["alias_notice"] = t("improve.alias_notice")
                command = await self._create_command(command2class[action], pr_url, args=args, **command_kwargs)
                result = await command.run()
                if action == "add_docs" and result is False:
                    span.set_status(StatusCode.ERROR)
                    span.set_attribute("error.type", "documentation_publication_failed")
                    return False

                if command_failed():
                    span.set_status(StatusCode.ERROR)
                    span.set_attribute("error.type", "command_execution_failed")
                else:
                    span.set_status(StatusCode.OK)
                return True
        finally:
            if language_overridden:
                settings.set("CONFIG.RESPONSE_LANGUAGE", previous_response_language)
            _record_token_metrics(action, _git_provider)
            if propagate_command_errors is not None:
                settings.set("CONFIG.PROPAGATE_COMMAND_ERRORS", previous_propagation)

    async def handle_request(
        self, pr_url, request, notify=None, propagate_command_errors: bool | None = None
    ) -> bool | RequestOutcome:
        """Return True, False, or RequestOutcome.SKIPPED without raising command errors.

        Callers must check for SKIPPED before reactions or other command follow-up.
        """
        try:
            if propagate_command_errors is None:
                return await self._handle_request(pr_url, request, notify)
            return await self._handle_request(
                pr_url, request, notify, propagate_command_errors=propagate_command_errors
            )
        except Exception:
            # _handle_request already catches command failures and annotates the span;
            # this is the outer contract every caller relies on — webhook handlers and
            # the router get False for failures. Policy skips are a distinct return value.
            get_logger().exception("Failed to process the command.")
            return False
        finally:
            # Serverless environments freeze after the response and are reaped
            # without running atexit, so export at the request boundary; the
            # worker thread keeps a slow collector from stalling the event loop.
            await asyncio.to_thread(flush_telemetry)
