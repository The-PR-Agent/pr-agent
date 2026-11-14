import asyncio
import copy
import datetime
import re
from dataclasses import replace
from functools import partial

from pydantic import ValidationError

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.artifacts import get_artifact_context, prompt_boundary_markers, resolve_artifact_path
from pr_agent.algo.auto_approval import approval_allowed
from pr_agent.algo.chunk_runner import run_chunk_batch
from pr_agent.algo.comment_identity import (
    PRCommandNoticeIdentity,
    PRReviewHeader,
    PRReviewIdentity,
    add_comment_identity,
    add_pr_review_identity,
    comment_authorship_available,
    format_failed_commit_marker,
    format_reviewed_commit_marker,
    get_pr_review_comment_identifiers,
    hidden_marker_forms,
    parse_commit_review_records,
    render_hidden_marker,
)
from pr_agent.algo.deterministic_risk import build_risk_checks
from pr_agent.algo.findings import Finding
from pr_agent.algo.git_patch_processing import split_diff_lines
from pr_agent.algo.i18n import t
from pr_agent.algo.inline_comment_dedup import (
    KEY_ISSUE_LOCATION_MARKER_RE,
    TEXT_FIX_INFO,
    InlineCommentStore,
    can_verify_inline_comment_publication,
    get_inline_comment_store,
    key_issue_body_with_markers,
    key_issue_fingerprint,
    key_issue_location_fingerprint,
    strip_markers,
)
from pr_agent.algo.language_handler import filter_bad_extensions
from pr_agent.algo.markdown_span import _markdown_code_span
from pr_agent.algo.merge_verdict import compute_merge_verdict
from pr_agent.algo.output_models import (
    PRReview,
    finding_confidence,
    parse_artifact_findings,
    parse_failure_modes,
)
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
    FallbackEligibleError,
    PreparedPRDiff,
    add_ai_metadata_to_diff_files,
    append_filtered_file_names,
    get_pr_diff,
    get_pr_multi_diffs,
    retry_with_fallback_models,
    run_diff_preparation,
)
from pr_agent.algo.prompt_fragments import render_diff_hunk_format
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.review_finding_state import (
    append_review_state,
    normalize_findings,
    parse_review_state,
    reconcile_review_findings,
    render_lifecycle_summary,
    render_previous_findings,
)
from pr_agent.algo.review_json_output import (
    hosted_markdown_artifact,
    hosted_markdown_export,
    review_json_output_path,
    write_review_json_output,
)
from pr_agent.algo.review_merge import all_key_issues, chunk_hit_findings_limit, merge_review_chunks
from pr_agent.algo.run_details import (
    command_failed,
    get_run_details,
    init_run_details,
    record_command_failure,
    record_model_used,
)
from pr_agent.algo.run_output import (
    append_run_details,
    github_action_output,
    push_outputs_async,
    show_relevant_configurations,
)
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.structural_reach import build_structural_reach
from pr_agent.algo.suggestion_checks import (
    dedent_code_for_file,
    is_line_range_in_diff,
    python_syntax_is_valid,
    validate_suggestion,
)
from pr_agent.algo.token_budget import AttemptTokenBudget, clip_tokens
from pr_agent.algo.utils import (
    ModelType,
    convert_to_markdown_v2,
    get_min_finding_confidence,
    get_suggestion_fence,
    is_value_no,
    load_yaml,
)
from pr_agent.commands.pr_code_suggestions import get_committable_code_suggestions
from pr_agent.commands.progress_comment import ChunkProgressReporter
from pr_agent.commands.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets,
    fit_related_tickets_to_prompt_budget,
)
from pr_agent.config_loader import get_bot_display_name, get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import (
    DISCUSSION_CONTEXT_MAX_MESSAGE_CHARS,
    GitProvider,
    IncompleteProviderPullRequestFilesError,
    IncrementalPR,
    PRCommit,
    commit_patch_fingerprint,
    format_persistent_update_header,
    get_main_pr_language,
)
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage

MAX_REVIEW_COVERAGE_FILES = 50

_REVIEW_FAILURE_REASONS = (
    (
        ("credit balance is too low", "insufficient credits", "insufficient balance", "insufficient_quota"),
        "The model provider rejected the request because the API account has insufficient credits. "
        "Add credits, then retry the command.",
    ),
    (
        ("authenticationerror", "authentication error", "invalid api key", "invalid x-api-key"),
        "{bot_name} could not authenticate with the model provider. Check the configured API credentials, then retry.",
    ),
    (
        ("ratelimiterror", "rate limit", "too many requests"),
        "The model provider rate-limited the request. Wait for the limit to reset, then retry.",
    ),
    (
        ("apitimeouterror", "timeout error", "timed out"),
        "The model provider timed out before completing the review. Retry the command or adjust the provider timeout.",
    ),
    (
        ("context_length_exceeded", "maximum context length", "input is too long", "too many tokens"),
        "The pull request exceeded the selected model's context limit. Retry with a larger-context model or an "
        "incremental review.",
    ),
    (
        ("apiconnectionerror", "connection error"),
        "{bot_name} could not reach the model provider. Check provider availability and network access, then retry.",
    ),
    (
        ("failed to generate prediction with any model",),
        "Every configured model attempt failed. Check the {bot_name} service logs for the provider error, then retry.",
    ),
)
_UNKNOWN_REVIEW_FAILURE_REASON = (
    "{bot_name} encountered an unexpected internal error. Check the {bot_name} service logs for details."
)


def _exception_chain_text(error: Exception) -> str:
    """Return exception types and messages for classification without publishing them."""
    parts = []
    current = error
    seen = set()
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        try:
            message = str(current)
        except Exception:
            message = ""
        parts.append(f"{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    return "\n".join(parts).casefold()


def _as_bool(value) -> bool:
    """Use the shared parser for review publication settings."""
    return _as_bool_setting(value)


def _review_failure_comment(error: Exception) -> str:
    """Build an optional deterministic failure explanation from an allowlist of safe messages."""
    if not _as_bool(get_settings().pr_reviewer.get("publish_error_details", False)):
        return "Failed to review PR"

    error_text = _exception_chain_text(error)
    reason = _UNKNOWN_REVIEW_FAILURE_REASON
    for patterns, candidate in _REVIEW_FAILURE_REASONS:
        if any(pattern in error_text for pattern in patterns):
            reason = candidate
            break
    reason = reason.format(bot_name=get_bot_display_name())
    return f"Failed to review PR\n\n**Reason:** {reason}"


PER_COMMIT_MAX_AUTO_ATTEMPTS = 3
NO_OWN_CHANGES_FINGERPRINT = commit_patch_fingerprint([])
_STATE_BLOCK_INVALID_MARKER = "invalid_marker"
_STATE_BLOCK_READ_ERROR = "read_error"
_STATE_BLOCK_REVIEW_DATA = "review_data"
_STATE_BLOCK_SIZE = "state_size"


def _is_structural_reach_key(key) -> bool:
    """Whether a review key would render like the deterministic structural reach row, whatever its separators."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) == "structuralreach"


def _is_risk_checks_key(key) -> bool:
    """Whether a review key would render like the deterministic risk checks row, whatever its separators."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) in ("riskchecks", "deterministicrisk")


_LINE_NUMBER_PREFIX_RE = re.compile(r"(\d+)(?: |$)")


def _strip_line_numbers(code: str | None, start_line: int, end_line: int) -> str | None:
    """Drop the diff's line-number prefix when every line of the model's code carries the expected one."""
    if not code:
        return code
    lines = split_diff_lines(code)
    if len(lines) != end_line - start_line + 1:
        return code
    stripped = []
    for expected, line in enumerate(lines, start=start_line):
        match = _LINE_NUMBER_PREFIX_RE.match(line)
        if match is None or int(match.group(1)) != expected:
            return code
        stripped.append(line[match.end():])
    return "\n".join(stripped)


_MAX_COMMITTABLE_FIX_LINES = 10


def _code_lines(code: str) -> list[str]:
    # Split on newlines only: splitlines() also splits on Unicode separators that can sit inside a string.
    return [line.rstrip(" \t") for line in code.replace("\r\n", "\n").strip("\n").split("\n")]


def _without_fix_block(body: str) -> str:
    """Drop the fix block that _build_key_issue_comment appends to a finding's body.

    Only the block it opens with ``suggestion`` or ``TEXT_FIX_INFO`` is dropped, so a code block of the
    finding's own text is kept. The fence is longer than any backtick run in the fix, which makes the
    closest opener with the closing fence's length the right one.
    """
    lines = body.split("\n")
    fence = lines[-1]
    if len(fence) < 3 or fence.strip("`"):
        return body
    for index in range(len(lines) - 2, -1, -1):
        info = lines[index][len(fence):] if lines[index].startswith(fence) else None
        if info is not None and (info.startswith("suggestion") or info == TEXT_FIX_INFO):
            return "\n".join(lines[:index]).rstrip()
    return body


def _is_merge_recommendation_key(key) -> bool:
    """Whether a review key would render like the computed merge recommendation, whatever its separators."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) == "mergerecommendation"


def _is_merge_recommendation_checks_key(key) -> bool:
    """Whether a review key would render like the deterministic merge checks row."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) == "mergerecommendationchecks"


def _with_merge_verdict(review: dict, verdict: dict) -> dict:
    """Put the computed recommendation and its checks next to the model's answer."""
    updated = {}
    inserted = False
    for key, value in review.items():
        if _is_merge_recommendation_checks_key(key):
            continue
        if _is_merge_recommendation_key(key):
            if inserted:
                continue
            updated["merge_recommendation"] = verdict["recommendation"]
            updated["merge_recommendation_checks"] = verdict["checks"]
            inserted = True
        else:
            updated[key] = value
    if not inserted:
        updated["merge_recommendation"] = verdict["recommendation"]
        updated["merge_recommendation_checks"] = verdict["checks"]
    return updated


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

    # State of the chunked flow, rebound by _prepare_chunked_prediction. Class-level immutable
    # defaults, so the single-call flow carries no bookkeeping.
    prediction_data = None  # merged review dict; None means "parse self.prediction instead"
    review_chunk_count = 1
    review_failed_chunk_count = 0
    low_confidence_filtered_count = 0
    # chunked reviews only: every finding the chunks reported (before the merge cut it to
    # num_max_findings), and whether a chunk reached that limit on its own
    review_all_issues = None
    review_chunk_hit_limit = None
    partial_files_list = ()
    alias_notice = ""  # one line under the heading when an alias such as /improve ran this review
    _review_commit: PRCommit | None = None
    _review_has_findings = False
    _review_fingerprint = ""
    _review_done = False
    _review_published = False

    def __init__(self, pr_url: str, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, alias_notice: str = ""):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            ai_handler (BaseAiHandler): The AI handler to be used for the review. Defaults to None.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
            alias_notice (str, optional): One line shown under the review heading when the review runs for an alias.
        """
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.alias_notice = alias_notice
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.remaining_files_list = []
        self.partial_files_list = []
        self.prediction = None
        self._review_state_result = None
        self._review_state_blocked = False
        self._review_state_block_reason = None
        self._review_finding_previous_state = None
        self._review_state_preserved = False
        self._pending_sink_output = None
        previous_findings = self._load_previous_findings_context()
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug("AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug("AI metadata is disabled for this command")

        is_ai_metadata = get_settings().get("config.enable_ai_metadata", False)
        commit_messages_begin, commit_messages_end = prompt_boundary_markers("COMMIT_MESSAGES")
        commit_message_begin, commit_message_end = prompt_boundary_markers("COMMIT_MESSAGE")
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "num_pr_files": self.git_provider.get_num_of_files(),
            "num_max_findings": get_settings().pr_reviewer.num_max_findings,
            "require_score": get_settings().pr_reviewer.require_score_review,
            "require_tests": get_settings().pr_reviewer.require_tests_review,
            "require_estimate_effort_to_review": get_settings().pr_reviewer.require_estimate_effort_to_review,
            "require_risk_assessment": get_settings().pr_reviewer.get("require_risk_assessment", False),
            "require_merge_recommendation": get_settings().pr_reviewer.get("require_merge_recommendation", False),
            "require_failure_modes": get_settings().pr_reviewer.get("require_failure_modes", False),
            "require_artifact_findings": bool(
                _as_bool(get_settings().pr_reviewer.get("require_artifact_findings", False))
                and get_artifact_context("pr_reviewer")
            ),
            "require_priority_files": get_settings().pr_reviewer.get("require_priority_files", False),
            "require_estimate_contribution_time_cost": (
                get_settings().pr_reviewer.require_estimate_contribution_time_cost
            ),
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'min_finding_confidence': get_min_finding_confidence(),
            "extra_instructions": self._extra_instructions(),
            "artifact_context": get_artifact_context("pr_reviewer"),
            "skills_context": get_skills_context(),
            "repo_context": build_repo_context(self.git_provider),
            "previous_findings": previous_findings,
            "commit_messages_str": "" if self.incremental.is_incremental else self.git_provider.get_commit_messages(),
            "commit_messages_begin": commit_messages_begin,
            "commit_messages_end": commit_messages_end,
            "commit_under_review": "",
            "commit_message_begin": commit_message_begin,
            "commit_message_end": commit_message_end,
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata": is_ai_metadata,
            "diff_hunk_format": render_diff_hunk_format(
                include_line_numbers=True,
                include_ai_metadata=is_ai_metadata,
            ),
            "related_tickets": get_settings().get('related_tickets', []),
            "related_tickets_omitted": 0,
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
        }

    @staticmethod
    def _extra_instructions() -> str:
        review_extra = get_settings().pr_reviewer.extra_instructions
        suggestions_extra = str(get_settings().pr_code_suggestions.get("extra_instructions") or "").strip()
        if not suggestions_extra or suggestions_extra in str(review_extra or ""):
            return review_extra
        return f"{review_extra}\n\n{suggestions_extra}" if review_extra else suggestions_extra

    def parse_incremental(self, args: list[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i" and not hosted_markdown_export.get():
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        if (
            _as_bool_setting(get_settings().pr_reviewer.get("per_commit_review", False))
            and not hosted_markdown_export.get()
        ):
            await self._run_per_commit()
        else:
            await self._run_review()

    async def _review_as_one(self, reason: str, notice: str) -> None:
        get_logger().warning(f"{reason}, skipping per commit review")
        if get_settings().config.publish_output and not get_settings().config.get("is_auto_command", False):
            try:
                self.git_provider.publish_comment(notice)
            except Exception as e:
                get_logger().exception(f"Failed to publish the per commit review notice, error: {e}")
        await self._run_review()

    def _publish_per_commit_failure(self, error: Exception, detail: str = "") -> None:
        record_command_failure()
        if (get_settings().config.publish_output
                and _as_bool(get_settings().pr_reviewer.get("publish_review_failure_comment", True))
                and not get_settings().config.get("is_auto_command", False)):
            try:
                self.git_provider.publish_comment(_review_failure_comment(error) + detail)
            except Exception as e:
                get_logger().exception(f"Failed to publish review failure result, error: {e}")

    async def _run_per_commit(self) -> None:
        init_run_details()
        is_auto = get_settings().config.get("is_auto_command", False)
        publish_notice = get_settings().config.publish_output and not is_auto
        try:
            commits = self.git_provider.get_pr_commits()
            pr = getattr(self.git_provider, "pr", None)
            total_commits = None if commits is None else getattr(pr, "commits", None)
            reviewed, _, failures = ({}, {}, {}) if commits is None else self._commit_review_records()
        except Exception as e:
            get_logger().error(f"Failed to list the PR commits or the commits already reviewed: {e}")
            self._publish_per_commit_failure(e)
            if get_settings().config.get("propagate_command_errors", False):
                raise
            return
        if commits is None:
            await self._review_as_one(
                f"Per commit review is not supported for {get_settings().config.git_provider}",
                "Per commit review is not supported for this git provider, so the whole PR was reviewed instead.")
            return
        if isinstance(total_commits, int) and len(commits) < total_commits:
            await self._review_as_one(
                f"PR has {total_commits} commits but the git provider listed only {len(commits)}",
                f"This PR has {total_commits} commits, more than the {len(commits)} the git provider API "
                f"returns, so per commit review was skipped and the whole PR was reviewed instead.")
            return
        if reviewed is None or not self._review_comment_authorship_available():
            await self._review_as_one(
                "The author of the earlier per commit review comments cannot be verified",
                "Per commit review could not verify which earlier comments it wrote, so the whole PR was reviewed "
                "instead of each commit.")
            return

        reviewed_fingerprints = set(reviewed.values())
        fingerprints = dict(reviewed)
        incremental_args = self.incremental.is_incremental
        base_vars = copy.deepcopy(self.vars)
        max_message_tokens = get_settings().config.get("max_commits_tokens", None)
        try:
            max_commits = int(get_settings().pr_reviewer.get("max_commits_per_review", 0))
        except (TypeError, ValueError):
            get_logger().warning("max_commits_per_review is not a number, reviewing without a limit")
            max_commits = 0
        new_records = {}
        new_failures = {}
        cleared_failures = False
        reviewed_count = 0
        failed = False
        first_error = None
        unloaded = []
        try:
            pending = []
            for commit in commits:
                if commit.sha in reviewed or (commit.parents is not None and len(commit.parents) != 1):
                    continue
                try:
                    fingerprint = self.git_provider.set_commit_review_scope(commit, IncrementalPR(True))
                except IncompleteProviderPullRequestFilesError:
                    raise
                except Exception as e:
                    get_logger().error(f"Failed to load the changes of commit {commit.sha}: {e}")
                    unloaded.append((commit.sha[:7], e))
                    continue
                if fingerprint is None:
                    fingerprint = NO_OWN_CHANGES_FINGERPRINT
                    new_records[commit.sha] = fingerprint
                fingerprints[commit.sha] = fingerprint
                if fingerprint in reviewed_fingerprints:
                    new_records[commit.sha] = fingerprint
                elif is_auto and failures.get(fingerprint, 0) >= PER_COMMIT_MAX_AUTO_ATTEMPTS:
                    get_logger().warning(f"Commit {commit.sha} failed to review {failures[fingerprint]} times, "
                                         f"leaving it to a manual /review")
                elif fingerprint != NO_OWN_CHANGES_FINGERPRINT:
                    pending.append((commit, fingerprint))

            if 0 < max_commits < len(pending):
                self._restore_full_scope()
                if incremental_args:
                    self.vars.update(commit_messages_str=self.git_provider.get_commit_messages(),
                                     previous_findings=self._load_previous_findings_context())
                await self._review_as_one(
                    f"{len(pending)} commits to review exceed max_commits_per_review={max_commits}",
                    f"There are {len(pending)} commits to review, more than the limit of {max_commits} for per "
                    f"commit review, so the whole PR was reviewed in one review instead.")
                if self._review_done and not command_failed():
                    new_records.update((commit.sha, fingerprint) for commit, fingerprint in pending)
                else:
                    new_failures = {fingerprint: failures.get(fingerprint, 0) + 1 for _, fingerprint in pending}
                return

            for commit, fingerprint in pending:
                self.incremental = IncrementalPR(True)
                done = False
                try:
                    self.git_provider.set_commit_review_scope(commit, self.incremental)
                    message = clip_tokens(commit.message, max_message_tokens) if max_message_tokens \
                        else commit.message
                    self.vars = dict(copy.deepcopy(base_vars), commit_under_review=message, commit_messages_str="",
                                     previous_findings="")
                    self._review_commit = commit
                    self._review_fingerprint = fingerprint
                    get_logger().info(f"Reviewing commit {commit.sha} of PR: {self.pr_url}")
                    await self._run_review()
                    reviewed_count += 1
                    failed = failed or command_failed()
                    done = self._review_done and not command_failed()
                except IncompleteProviderPullRequestFilesError:
                    raise
                except Exception as e:
                    get_logger().error(f"Failed to review commit {commit.sha}: {e}")
                    failed = True
                    first_error = first_error or e
                if not done:
                    new_failures[fingerprint] = failures.get(fingerprint, 0) + 1
                    continue
                cleared_failures = cleared_failures or fingerprint in failures
                if not self._review_published:
                    new_records[commit.sha] = fingerprint
        finally:
            self._review_commit = None
            self.vars = base_vars
            self._restore_full_scope()
            if unloaded:
                failed = True
                first_error = first_error or unloaded[0][1]
                self._publish_per_commit_failure(
                    unloaded[0][1], "\n\nThe changes of these commits could not be loaded: "
                    + ", ".join(f"`{sha}`" for sha, _ in unloaded))
            if (new_records or new_failures or cleared_failures) and get_settings().config.publish_output:
                self._record_commit_reviews(commits, fingerprints, new_records, new_failures)

        if not reviewed_count and not failed:
            get_logger().info(f"No commit with changes of its own to review in PR: {self.pr_url}")
            if publish_notice:
                try:
                    self.git_provider.publish_comment(
                        "Per commit review skipped: no new commits with changes since the last review."
                        if reviewed_fingerprints & set(fingerprints.values())
                        else "Per commit review skipped: no commits with changes to review.")
                except Exception as e:
                    get_logger().exception(f"Failed to publish the per commit review notice, error: {e}")
        if failed:
            record_command_failure()
        if first_error is not None and get_settings().config.get("propagate_command_errors", False):
            raise first_error

    def _restore_full_scope(self) -> None:
        self.incremental = IncrementalPR(False)
        try:
            self.git_provider.get_incremental_commits(self.incremental)
        except Exception as e:
            get_logger().exception(f"Failed to restore the full PR scope after per commit review, error: {e}")

    def _commit_review_records(self) -> tuple[dict[str, str] | None, dict[str, str], dict[str, int]]:
        reviewed, recorded, failures = {}, {}, {}
        if not self.git_provider.is_supported("get_issue_comments"):
            return reviewed, recorded, failures
        unverifiable = verified = False
        for comment in self.git_provider.get_issue_comments():
            found, failed, is_record = parse_commit_review_records(GitProvider._get_comment_body(comment))
            if not (found or failed):
                continue
            try:
                if not self.git_provider.is_comment_authored_by_pr_agent(comment):
                    continue
            except RuntimeError:
                unverifiable = True
                continue
            verified = True
            reviewed.update(found)
            if is_record:
                recorded.update(found)
                for fingerprint, attempts in failed.items():
                    failures[fingerprint] = max(failures.get(fingerprint, 0), attempts)
        return (None if unverifiable and not verified else reviewed), recorded, failures

    def _record_commit_reviews(self, commits: list[PRCommit], fingerprints: dict[str, str],
                               new_records: dict[str, str], new_failures: dict[str, int]) -> None:
        identity = PRCommandNoticeIdentity.REVIEWED_COMMITS.value
        try:
            reviewed, recorded, failures = self._commit_review_records()
            if reviewed is None:
                return
            recorded.update(new_records)
            for fingerprint, attempts in new_failures.items():
                failures[fingerprint] = max(failures.get(fingerprint, 0), attempts)
            age = {fingerprints[commit.sha]: index
                   for index, commit in enumerate(commits) if commit.sha in fingerprints}
            done = set(reviewed.values()) | set(recorded.values())
            lines = [line for _, line in sorted(
                [(age[fp], format_reviewed_commit_marker(fp, sha)) for sha, fp in recorded.items() if fp in age]
                + [(age[fp], format_failed_commit_marker(fp, attempts))
                   for fp, attempts in failures.items() if fp in age and fp not in done])]
            max_chars = getattr(self.git_provider, "max_comment_chars", None)
            max_chars = max_chars if isinstance(max_chars, int) else 65000
            header = f"{identity}\n\nCommits reviewed by per commit review without a comment of their own.\n"
            while lines and len(header) + sum(len(line) + 1 for line in lines) > max_chars:
                get_logger().warning("Per commit review record is full, the oldest commits may be reviewed again")
                lines.pop(0)
            body = "\n".join([header, *lines])
            self.git_provider.publish_persistent_comment_full(
                body, initial_header=identity, update_header=False, final_update_message=False,
                identity_marker=identity, require_agent_authorship=True, fallback_on_error=False)
        except Exception as e:
            get_logger().exception(f"Failed to record the commits reviewed by per commit review, error: {e}")

    async def _run_review(self) -> None:
        init_run_details()
        self.partial_files_list = []
        for name in ("_chunked_patches_diff_list", "_chunked_remaining_files_list", "_chunked_partial_files_list",
                     "_chunked_results", "_chunked_primary_model"):
            self.__dict__.pop(name, None)
        progress_response = None
        partial_review_error = None
        review_error = None
        review_failed = False
        review_completed = False
        persistent_write_failed = False
        self._progress_response = None
        self._chunk_progress = None
        self._structured_review_data = None
        self._approval_model_fields = (None, None)
        self._review_done = self._review_published = False
        # An approval stands only for a run that finished clean; a new run starts without one.
        if self._auto_approval_enabled():
            self._withdraw_auto_approval("a new review started")
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                self._review_done = True
                return None

            if self.incremental.is_incremental and self._review_commit is None:
                can_run = self._can_run_incremental_review()
                # If the gate disabled incremental (e.g., commits_range is None), fall through to full review.
                if not can_run and self.incremental.is_incremental:
                    return None
                if not self.incremental.is_incremental:
                    # Reload the findings the incremental mode left out, for the fallback full review.
                    self.vars["previous_findings"] = self._load_previous_findings_context()
                    self.vars["commit_messages_str"] = self.git_provider.get_commit_messages()

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            if (
                self.incremental.is_incremental
                and hasattr(self.git_provider, "unreviewed_files_map")
                and not self.git_provider.unreviewed_files_map
            ):
                get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new files")
                previous_review_url = ""
                if hasattr(self.git_provider, "previous_review") and self.git_provider.previous_review is not None:
                    previous_review_url = getattr(self.git_provider.previous_review, "html_url", "") or ""
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(f"Incremental Review Skipped\n"
                                    f"No files were changed since the [previous PR Review]({previous_review_url})")
                return None

            if get_settings().config.publish_output and not get_settings().config.get('is_auto_command', False):
                progress_response = self.git_provider.publish_comment(
                    t("review.progress"), is_temporary=True)
                # A chunked run rewrites this temporary comment in place. The chunk count is
                # only known once the diff is split, so the reporter is built there; all this
                # run keeps is the handle.
                self._progress_response = progress_response

            # PyGithub fetches diff files synchronously; keep large fetches off the webhook event loop.
            diff_files = await asyncio.to_thread(self.git_provider.get_diff_files)
            # Since #4861 every provider caches a completed empty diff, so an unconfirmed
            # one — an empty result whose provider left the cache unset — is only possible
            # for a provider that does not follow the contract yet.
            unconfirmed_empty_diff = not diff_files and getattr(self.git_provider, "diff_files", diff_files) is None
            if diff_files is None or unconfirmed_empty_diff or (diff_files and filter_bad_extensions(diff_files)):
                await extract_and_cache_pr_tickets(self.git_provider, self.vars)
            # Do not retain uncached providers' prefetched contents during prediction.
            del diff_files
            self._raw_prompt_vars = copy.deepcopy(self.vars)
            try:
                await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR,
                                                 git_provider=self.git_provider)
            except Exception as error:
                if not self._merge_cached_review_chunks():
                    raise
                partial_review_error = error
                get_logger().warning("Fallback models exhausted; publishing successful review chunks")
            if not self.prediction:
                return None

            pr_review = self._prepare_pr_review()
            get_logger().debug("PR output", artifact=pr_review)

            # Deliver the review to the external sinks before anything else, so a sink
            # still receives it when the publishing below fails.
            await self._deliver_sink_output()

            if not pr_review:
                raise ValueError("Failed to prepare review output")

            state_result = getattr(self, "_review_state_result", None)
            state_changed = bool(state_result and state_result.changed)
            state_blocked = getattr(self, "_review_state_blocked", False)
            should_publish = get_settings().config.publish_output and (
                self._should_publish_review_no_suggestions(pr_review)
                or state_changed
                or state_blocked
                or self.review_failed_chunk_count > 0
            )
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(reason)
                get_settings().data = {"artifact": pr_review}
                if (
                    hosted_markdown_export.get()
                    and partial_review_error is None
                    and not self.review_failed_chunk_count
                ):
                    hosted_markdown_artifact.set(pr_review)
                self._publish_merge_recommendation_status()
                review_completed = True
                self._review_done = True
                return

            # publish the review
            # Providers that support it (GitLab) can post the review's final comment as a resolvable thread.
            # This intent applies to the review only - never to status comments or the output of other commands.
            review_thread_kwargs = {"as_thread": True} if self.git_provider.should_publish_review_as_thread() else {}
            state_block_reason = getattr(self, "_review_state_block_reason", None)
            if state_blocked and (
                state_block_reason == _STATE_BLOCK_INVALID_MARKER
                or (
                    state_block_reason == _STATE_BLOCK_SIZE
                    and getattr(self, "_review_state_preserved", False)
                )
            ):
                get_logger().warning(
                    "Review finding state cannot be written safely; replacing the persistent review "
                    "with a clean state marker"
                )
                persistent_args = dict(
                    initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                    update_header=True,
                    final_update_message=False,
                    identity_marker=PRReviewIdentity.REGULAR.value,
                    legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                    require_agent_authorship=True,
                    fallback_on_error=False,
                    **review_thread_kwargs,
                )
                persistent_write_failed = True
                result = self.git_provider.publish_persistent_comment_full(
                    pr_review, **persistent_args
                )
                persistent_write_failed = not self._persistent_publish_succeeded(result)
                if persistent_write_failed:
                    review_failed = True
            elif state_blocked:
                get_logger().warning(
                    "Review finding state is blocked by review data or provider read failure; "
                    "publishing without changing persistent state"
                )
                self.git_provider.publish_comment(
                    self._as_non_authoritative_review(pr_review),
                    **review_thread_kwargs,
                )
            elif get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                persistent_args = dict(
                    initial_header=pr_review.split("\n", 1)[0],
                    update_header=True,
                    final_update_message=final_update_message,
                    identity_marker=PRReviewIdentity.REGULAR.value,
                    legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                    **review_thread_kwargs,
                )
                if not self._review_finding_state_in_play():
                    self.git_provider.publish_persistent_comment(pr_review, **persistent_args)
                elif state_result is not None:
                    persistent_args["require_agent_authorship"] = True
                    persistent_args["fallback_on_error"] = False
                    persistent_write_failed = True
                    result = self.git_provider.publish_persistent_comment_full(
                        pr_review, **persistent_args
                    )
                    persistent_write_failed = not self._persistent_publish_succeeded(result)
                    if persistent_write_failed:
                        review_failed = True
                elif self._publish_review_check_run(pr_review):
                    pass
                elif self._review_comment_authorship_available():
                    persistent_args["require_agent_authorship"] = True
                    persistent_args["fallback_on_error"] = False
                    persistent_write_failed = True
                    result = self.git_provider.publish_persistent_comment_full(
                        pr_review, **persistent_args
                    )
                    persistent_write_failed = not self._persistent_publish_succeeded(result)
                    if persistent_write_failed:
                        review_failed = True
                elif self._persistent_review_comment_exists() is False:
                    # There is no review comment to replace, so creating one cannot overwrite
                    # a comment PR-Agent did not author. An identity this deployment cannot
                    # resolve is not a reason to demote the canonical review.
                    self.git_provider.publish_persistent_comment(pr_review, **persistent_args)
                else:
                    # An unverified provider identity must never update a canonical review.
                    self.git_provider.publish_comment(
                        self._as_non_authoritative_review(pr_review),
                        **review_thread_kwargs,
                    )

            else:
                if self._review_commit is not None:
                    pr_review = add_comment_identity(pr_review, format_reviewed_commit_marker(
                        self._review_fingerprint, self._review_commit.sha), self.git_provider)
                if self.git_provider.supports_review_comment_identity() is True:
                    identity_marker = (
                        PRReviewIdentity.INCREMENTAL.value
                        if self.incremental.is_incremental
                        else PRReviewIdentity.REGULAR.value
                    )
                    pr_review = add_pr_review_identity(pr_review, identity_marker, self.git_provider)
                self.git_provider.publish_comment(pr_review, **review_thread_kwargs)
                self._review_published = True
            review_completed = True
            self._review_done = not review_failed
        except Exception as e:
            review_error = e
            review_failed = True
            get_logger().error(f"Failed to review PR: {e}")
            if (
                isinstance(e, IncompleteProviderPullRequestFilesError)
                or get_settings().config.get("propagate_command_errors", False)
            ):
                raise
        finally:
            self._chunk_progress = None
            if progress_response is not None:
                try:
                    self.git_provider.remove_comment(progress_response)
                except Exception as e:
                    get_logger().exception(f"Failed to remove review progress comment, error: {e}")
            if not review_failed:
                self._publish_merge_recommendation_status()
            if review_failed:
                # Record the failure here, where `review_failed` is consumed: the branches above
                # set it and go on to publish a failure comment instead of raising, so the run
                # would otherwise report success. `command_failed()` is what the GitHub Action
                # runner and the reaction outcomes read.
                record_command_failure()
            if (
                review_failed
                and not isinstance(review_error, IncompleteProviderPullRequestFilesError)
                and get_settings().config.publish_output
                and _as_bool(get_settings().pr_reviewer.get("publish_review_failure_comment", True))
                and (
                    persistent_write_failed
                    or not get_settings().config.get("is_auto_command", False)
                )
            ):
                try:
                    self.git_provider.publish_comment(_review_failure_comment(review_error))
                except Exception as e:
                    get_logger().exception(f"Failed to publish review failure result, error: {e}")
            # Runs on failed and cancelled reviews too, so an earlier approval is withdrawn.
            self.auto_approve_logic(partial_review_error, review_completed=review_completed and not review_failed)
            if (partial_review_error is not None and not review_failed
                    and get_settings().config.get("propagate_command_errors", False)):
                raise partial_review_error
            if not review_failed and self._structured_review_data is not None:
                write_review_json_output(self._structured_review_data)

    def _review_finding_state_enabled(self) -> bool:
        settings = get_settings()
        if not settings.config.publish_output:
            return False
        if not settings.pr_reviewer.get("persistent_comment", True):
            return False
        if not settings.pr_reviewer.get("persistent_finding_state", True):
            return False
        provider = getattr(self, "git_provider", None)
        if provider is None:
            return False
        if (
            getattr(getattr(settings, "github", None), "publish_as_check_run", False)
            and callable(getattr(provider, "_publish_check_run", None))
        ):
            return False
        if getattr(getattr(self, "incremental", None), "is_incremental", False):
            return False
        try:
            if provider.supports_review_finding_state() is not True:
                return False
            return bool(provider.is_supported("get_issue_comments"))
        except Exception as e:
            get_logger().warning(f"Review finding state is not supported by this provider, error: {e}")
            return False

    @staticmethod
    def _persistent_publish_succeeded(result) -> bool:
        return result is not None and result is not False

    @staticmethod
    def _as_non_authoritative_review(pr_review: str) -> str:
        identity_markers = {
            *hidden_marker_forms(PRReviewIdentity.REGULAR.value),
            *hidden_marker_forms(PRReviewIdentity.INCREMENTAL.value),
        }
        markerless_review = "\n".join(
            line
            for line in str(pr_review).splitlines()
            if line.strip() not in identity_markers
        ).strip()
        bot_display_name = get_bot_display_name()
        return (
            "## Standalone PR Review\n\n"
            f"_{bot_display_name} could not safely update the persistent review. "
            "This standalone result will not replace the canonical review._\n\n"
            f"{markerless_review}"
        )

    def _publish_review_check_run(self, pr_review: str) -> bool:
        if not getattr(
            getattr(get_settings(), "github", None),
            "publish_as_check_run",
            False,
        ):
            return False
        publisher = getattr(self.git_provider, "_publish_check_run", None)
        if not callable(publisher):
            return False
        try:
            return publisher(pr_review, "review") is True
        except Exception as error:
            get_logger().warning(
                f"Failed to publish review check run, error: {error}"
            )
            return False

    def _publish_merge_recommendation_status(self) -> None:
        recommendation = getattr(self, "_merge_recommendation_status", None)
        settings = get_settings()
        if (
            not recommendation
            or not settings.config.publish_output
            or not _as_bool(settings.pr_reviewer.get("publish_merge_recommendation_status", False))
        ):
            return

        publisher = getattr(self.git_provider, "publish_merge_recommendation_status", None)
        if not callable(publisher):
            get_logger().warning(
                f"Git provider does not support publishing merge recommendation status: {recommendation}"
            )
        elif not publisher(recommendation):
            get_logger().warning(f"Failed to publish merge recommendation status: {recommendation}")

    def _review_finding_state_in_play(self) -> bool:
        if not get_settings().pr_reviewer.get("persistent_finding_state", True):
            return False
        provider = getattr(self, "git_provider", None)
        if provider is None:
            return False
        capability = getattr(provider, "supports_review_finding_state", None)
        implementation = getattr(capability, "__func__", capability)
        return (
            callable(capability)
            and implementation is not GitProvider.supports_review_finding_state
        )

    def _persistent_review_comment_exists(self) -> bool | None:
        """Whether a comment already carries the full-review identity.

        Returns None when the provider's comments could not be read at all, so a caller
        that must not overwrite an existing review can stay conservative.
        """
        provider = getattr(self, "git_provider", None)
        if provider is None:
            return None
        try:
            for _comment, _body in GitProvider._iter_persistent_comments(
                provider,
                get_pr_review_comment_identifiers(full=True, incremental=False),
                identity_marker=PRReviewIdentity.REGULAR.value,
            ):
                return True
            return False
        except Exception as error:
            get_logger().warning(f"Could not read the existing review comments: {error}")
            return None

    def _review_comment_authorship_available(self) -> bool:
        return comment_authorship_available(getattr(self, "git_provider", None))

    def _load_review_finding_state(self):
        identifiers = get_pr_review_comment_identifiers(full=True, incremental=False)
        try:
            invalid_marker_found = False
            for _comment, body in GitProvider._iter_persistent_comments(
                self.git_provider,
                identifiers,
                identity_marker=PRReviewIdentity.REGULAR.value,
                require_agent_authorship=True,
            ):
                parsed = parse_review_state(body)
                if parsed.valid and parsed.present:
                    self._review_state_blocked = False
                    self._review_state_block_reason = None
                    return parsed
                if not parsed.valid and parsed.present:
                    invalid_marker_found = True
                    get_logger().warning(
                        "Review finding state marker is malformed or unsupported; "
                        "trying an older persistent review"
                    )
            if invalid_marker_found:
                self._review_state_blocked = True
                self._review_state_block_reason = _STATE_BLOCK_INVALID_MARKER
                return None
        except Exception as e:
            self._review_state_blocked = True
            self._review_state_block_reason = _STATE_BLOCK_READ_ERROR
            get_logger().warning(f"Could not read persistent review state; skipping persistent update, error: {e}")
            return None
        self._review_state_blocked = False
        self._review_state_block_reason = None
        return parse_review_state("")

    def _load_previous_findings_context(self) -> str:
        """Return the findings stored by earlier reviews as a JSON block for the prompt, or ""."""
        value = get_settings().pr_reviewer.get("max_previous_findings_chars", 8000)
        try:
            max_chars = int(value)
        except (TypeError, ValueError, OverflowError):
            get_logger().warning(f"Invalid pr_reviewer.max_previous_findings_chars: {value!r}")
            return ""
        if max_chars <= 0 or not self._review_finding_state_enabled():
            return ""
        parsed = self._load_review_finding_state()
        if parsed is None or not parsed.valid:
            return ""
        return render_previous_findings(parsed.state, max_chars, self._load_dismissed_key_issues())

    def _load_dismissed_key_issues(self) -> list[Finding]:
        """Return inline key issues whose threads indicate an eligible dismissal, newest first.

        Only threads PR-Agent verifiably opened count. Providers determine eligible dismissal statuses;
        Azure infers a decision from an explicit dismissal differing from its configured creation default.
        """
        dismissed = []
        try:
            for thread in self.git_provider._iter_review_threads():
                if (thread.status != "resolved" or thread.authored_by_agent is not True
                        or not KEY_ISSUE_LOCATION_MARKER_RE.search(thread.suggestion)):
                    continue
                finding = Finding(
                    path=thread.file,
                    body=_without_fix_block(strip_markers(thread.suggestion).strip()),
                    line_start=thread.start_line,
                    line_end=thread.end_line,
                )
                replies = thread.text_replies()
                if replies:
                    finding = replace(finding, reply=replies[-1][1][:DISCUSSION_CONTEXT_MAX_MESSAGE_CHARS])
                dismissed.append(finding)
        except Exception as e:
            get_logger().warning(f"Could not read resolved key-issue threads, error: {e}")
            return []
        return dismissed

    @staticmethod
    def _review_finding_from_issue(issue: dict) -> Finding | None:
        return Finding.from_review_issue(issue)

    @classmethod
    def _review_findings_from_data(cls, data: dict) -> list[Finding] | None:
        review = data.get("review")
        if not isinstance(review, dict):
            return None
        if "key_issues_to_review" not in review:
            return None
        issues = review["key_issues_to_review"]
        if not isinstance(issues, list):
            return None
        findings = []
        for issue in issues:
            finding = cls._review_finding_from_issue(issue)
            if finding is None:
                # A key issue without a file or a body cannot be tracked across runs, but the
                # review summary still renders it; dropping the entry keeps the lifecycle state
                # of every other finding instead of discarding the whole review.
                get_logger().debug("Skipping a key issue that carries no trackable location",
                                   artifact={"issue": issue})
                continue
            findings.append(finding)
        return findings

    def _review_head_sha(self) -> str:
        # Resolve the head commit through the provider's own hook, so the reviewer does not
        # need to know which field each API carries it in.
        get_head_sha = getattr(self.git_provider, "get_pr_head_sha", None)
        if callable(get_head_sha):
            head_sha = get_head_sha()
            if isinstance(head_sha, str) and head_sha:
                return head_sha

        last_commit = getattr(self.git_provider, "last_commit_id", None)
        if isinstance(last_commit, str):
            return last_commit
        for attribute in ("sha", "id"):
            value = getattr(last_commit, attribute, None)
            if isinstance(value, str):
                return value
        return ""

    def _review_run_id(self) -> str:
        try:
            value = self.git_provider.get_latest_commit_url()
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    def _review_comment_max_chars(self) -> int | None:
        for attribute in ("max_comment_chars", "max_comment_length"):
            value = getattr(self.git_provider, attribute, None)
            if isinstance(value, int) and value > 0:
                update_suffix = "\n\n" + format_persistent_update_header("review", self._review_run_id())
                # The shared persistent publisher adds the full-review identity
                # before inserting the update suffix. Reserve both pieces so a
                # complete state marker remains inside the provider limit.
                identity_overhead = len(render_hidden_marker(PRReviewIdentity.REGULAR.value, self.git_provider)) + 2
                return value - len(update_suffix) - identity_overhead
        return None

    def _prepare_review_finding_state(self, data: dict) -> None:
        self._review_state_result = None
        self._review_state_blocked = False
        self._review_state_block_reason = None
        self._review_finding_previous_state = None
        self._review_state_preserved = False
        if not self._review_finding_state_enabled():
            return
        if not isinstance(data.get("review"), dict):
            self._review_state_blocked = True
            self._review_state_block_reason = _STATE_BLOCK_REVIEW_DATA
            get_logger().warning("Review data is invalid; preserving persistent finding state")
            return

        parsed = self._load_review_finding_state()
        if parsed is not None and parsed.valid:
            self._review_finding_previous_state = parsed.state
        if parsed is None and self._review_state_block_reason != _STATE_BLOCK_INVALID_MARKER:
            return
        current_findings = self._review_findings_from_data(data)
        if current_findings is None:
            self._review_state_blocked = True
            self._review_state_block_reason = _STATE_BLOCK_REVIEW_DATA
            get_logger().warning("Review finding data is invalid; skipping persistent state update")
            return
        incomplete_files = list(dict.fromkeys([*self.remaining_files_list, *self.partial_files_list]))
        if self._review_state_blocked:
            if self._review_state_block_reason == _STATE_BLOCK_INVALID_MARKER:
                self._review_state_result = reconcile_review_findings(
                    None,
                    current_findings,
                    allow_resolution=False,
                    excluded_files=incomplete_files,
                    head_sha=self._review_head_sha(),
                    run_id=self._review_run_id(),
                )
            return
        try:
            max_findings = int(get_settings().pr_reviewer.num_max_findings)
        except (TypeError, ValueError):
            max_findings = 0
        reported_issues = data["review"].get("key_issues_to_review")
        dropped_findings = (
            isinstance(reported_issues, list)
            and len(current_findings) < len(reported_issues)
        )
        if self.review_all_issues is not None:
            current_findings, dropped_findings = self._chunked_findings_for_state(
                current_findings, parsed.state)
        allow_resolution = (
            bool(self.prediction)
            and not bool(getattr(self.incremental, "is_incremental", False))
            # A merged result with failed chunks is still partial, even when chunking left
            # no additional token-budget files to report.
            and not bool(self.review_failed_chunk_count)
            and not bool(incomplete_files)
            and parsed.valid
            and current_findings is not None
            # a dropped finding is not an absent one, so this run cannot resolve anything
            and not dropped_findings
            # neither is a finding suppressed by min_finding_confidence
            and not self.low_confidence_filtered_count
            and (
                len(current_findings) < max_findings
                # a chunked review that reached the limit is complete unless a chunk stopped at it
                if self.review_chunk_hit_limit is None
                else not self.review_chunk_hit_limit
            )
        )
        result = reconcile_review_findings(
            parsed.state,
            current_findings,
            allow_resolution=allow_resolution,
            excluded_files=incomplete_files,
            head_sha=self._review_head_sha(),
            run_id=self._review_run_id(),
        )
        if parsed.state is not None or result.changed:
            self._review_state_result = result

    def _chunked_findings_for_state(
        self, published: list[Finding], previous_state: dict | None
    ) -> tuple[list[Finding], bool]:
        """Pick the findings of a chunked review that the state is reconciled against.

        The merge cuts the findings to `num_max_findings`, but a cut finding is a known one, so
        it is matched against the state: an active one stays active and a resolved one reopens.
        A new cut finding stays out until a later review publishes it, because the state reaches
        the next review as already reported. Also returns whether an issue could not be tracked.
        """
        all_findings = self._review_findings_from_data({"review": {"key_issues_to_review": self.review_all_issues}})
        wanted = {finding["finding_id"] for finding in normalize_findings(published)}
        wanted.update(finding["finding_id"] for finding in (previous_state or {}).get("findings", []))
        findings = [finding for finding in normalize_findings(all_findings) if finding["finding_id"] in wanted]
        return findings, len(all_findings) < len(self.review_all_issues)

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        if get_settings().pr_reviewer.get('publish_output_no_suggestions', True):
            return True
        if self._review_commit is not None:
            return self._review_has_findings
        return t("review.no_major_issues") not in pr_review

    async def _prepare_prediction(self, model: str) -> None:
        # Each model attempt owns a fresh result. A malformed primary must not
        # leave state that can be mistaken for a successful fallback response.
        self.prediction = None
        self.prediction_data = None
        self.review_chunk_count = 1
        self.review_failed_chunk_count = 0
        self.review_all_issues = None
        self.review_chunk_hit_limit = None
        self.low_confidence_filtered_count = 0
        self.partial_files_list = []
        raw_prompt_vars = getattr(self, "_raw_prompt_vars", getattr(self, "vars", None))
        ai_handler = getattr(self, "ai_handler", None)
        output_token_reserve = getattr(
            ai_handler, "get_output_token_reserve", None
        )
        if raw_prompt_vars is not None:
            try:
                self.vars, self.token_handler = fit_related_tickets_to_prompt_budget(
                    self.git_provider.pr,
                    raw_prompt_vars,
                    get_settings().pr_review_prompt.system,
                    get_settings().pr_review_prompt.user,
                    model,
                    ai_handler=ai_handler,
                    output_token_reserve=output_token_reserve,
                )
            except FallbackEligibleError:
                if not raw_prompt_vars.get("previous_findings"):
                    raise
                get_logger().warning(f"Earlier findings do not fit the prompt budget for {model}, omitting them")
                self.vars, self.token_handler = fit_related_tickets_to_prompt_budget(
                    self.git_provider.pr,
                    dict(raw_prompt_vars, previous_findings=""),
                    get_settings().pr_review_prompt.system,
                    get_settings().pr_review_prompt.user,
                    model,
                    ai_handler=ai_handler,
                    output_token_reserve=output_token_reserve,
                )
        chunking_enabled = get_settings().pr_reviewer.get("enable_large_pr_chunking", False)
        diff_kwargs = {
            "add_line_numbers_to_hunks": True,
            "disable_extra_lines": False,
            "return_remaining_files": True,
        }
        if callable(output_token_reserve):
            diff_kwargs["output_token_reserve"] = output_token_reserve
        if chunking_enabled:
            diff_kwargs["return_prepared"] = True
        output = await run_diff_preparation(get_pr_diff, self.git_provider, self.token_handler, model, **diff_kwargs)
        if isinstance(output, PreparedPRDiff):
            self.patches_diff = output.diff
            self.remaining_files_list = output.remaining_files_list
        elif isinstance(output, tuple):
            self.patches_diff, self.remaining_files_list = output
        else:
            self.patches_diff = output
            self.remaining_files_list = []

        # Resume an incomplete chunk plan even when a fallback model can fit the full diff.
        # Otherwise, the single-call path would bypass cached successful chunks.
        has_incomplete_chunk_plan = hasattr(self, "_chunked_patches_diff_list")
        if chunking_enabled and (self.remaining_files_list or has_incomplete_chunk_plan):
            prepared_diff = output if isinstance(output, PreparedPRDiff) else None
            if await self._prepare_chunked_prediction(model, prepared_diff):
                return

        if self.patches_diff:
            get_logger().debug("PR diff", diff=self.patches_diff)
            prediction = await self._get_prediction(model)
            self._load_valid_review_yaml(prediction)
            self.prediction = prediction
        elif not self.remaining_files_list and not self.partial_files_list:
            # Every changed file was filtered out (lockfile, asset, ignore rule), so there is
            # nothing left to review. Skip the model chain instead of failing the command.
            get_logger().info(f"PR has no analyzable files: {self.pr_url}, skipping review")
            self._review_done = True
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            raise FallbackEligibleError(f"No PR diff fits the /review request for {model}")

    def _review_progress_body(self, line: str) -> str:
        """Render the temporary review placeholder plus the current chunk progress line."""
        progress = t("review.progress")
        return f"{progress} {line}" if line else progress

    def _chunk_progress_reporter(self, total: int, completed: int = 0) -> ChunkProgressReporter | None:
        """Report chunk progress by rewriting the temporary review comment in place.

        A chunked run makes several parallel model calls plus retries that can take minutes,
        so the placeholder is kept current. Returns None when the comment cannot be edited
        back (output-only providers) or when progress reporting is turned off, which leaves
        the run with today's frozen placeholder.
        """
        comment = getattr(self, "_progress_response", None)
        settings = get_settings()
        if not settings.config.get("publish_output_progress", True):
            return None
        # A missing comment falls through: create() then serves the check-run sink alone,
        # which is the only progress channel automatic commands have.
        return ChunkProgressReporter.create(
            self.git_provider,
            comment,
            t("review.progress"),
            total=total,
            body_builder=self._review_progress_body,
            label="review",
            completed=completed,
        )

    async def _prepare_chunked_prediction(self, model: str,
                                          prepared_diff: PreparedPRDiff | None = None) -> bool:
        """Review a too-large diff in chunks and merge the per-chunk verdicts.

        Returns False when chunking does not apply, leaving the single-call flow in place.
        """
        patches_diff_list = getattr(self, "_chunked_patches_diff_list", None)
        if patches_diff_list is not None:
            self._resize_pending_review_chunks(model)
            patches_diff_list = self._chunked_patches_diff_list
        if patches_diff_list is not None and self._chunked_remaining_files_list:
            self._include_newly_reviewable_files(model)
        if patches_diff_list is None:
            multi_diff_kwargs = {
                "max_calls": get_settings().pr_reviewer.get("max_number_of_calls", 3),
                "add_line_numbers": True,
                "return_coverage": True,
                "include_filtered_file_names": False,
            }
            output_token_reserve = getattr(
                getattr(self, "ai_handler", None), "get_output_token_reserve", None
            )
            if callable(output_token_reserve):
                multi_diff_kwargs["output_token_reserve"] = output_token_reserve
            if prepared_diff is not None:
                multi_diff_kwargs["prepared_diff"] = prepared_diff
            coverage = get_pr_multi_diffs(
                self.git_provider,
                self.token_handler,
                model,
                **multi_diff_kwargs)
            patches_diff_list = coverage.chunks
            self._chunked_patches_diff_list = patches_diff_list
            self._chunked_remaining_files_list = coverage.remaining_files_list
            self._chunked_partial_files_list = coverage.partial_files_list
            self._chunked_primary_model = model
            if len(patches_diff_list) == 1:
                self.patches_diff = patches_diff_list[0]
                self.remaining_files_list = coverage.remaining_files_list
                self.partial_files_list = coverage.partial_files_list
        if len(patches_diff_list) < 2:
            get_logger().info("Large-diff chunking produced a single chunk, reviewing the PR in one call")
            return False

        get_logger().info(f"Number of PR chunk calls: {len(patches_diff_list)}")
        get_logger().debug("PR diff chunks", artifact=patches_diff_list)
        chunk_results = getattr(self, "_chunked_results", {})
        pending_indices = [index for index in range(len(patches_diff_list)) if index not in chunk_results]
        filtered_files = getattr(self.git_provider, "get_filtered_diff_file_names", lambda: [])()
        if isinstance(filtered_files, (list, tuple)) and filtered_files:
            attempt_budget = self._review_attempt_budget(model)
            max_tokens = attempt_budget.token_handler.prompt_tokens + attempt_budget.available_tokens(
                OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD, preserve_minimum=True, clamp=False,
            )
            prompts = [append_filtered_file_names(
                patches_diff_list[index], self.git_provider, attempt_budget.token_handler, max_tokens,
            ) for index in pending_indices]
        else:
            prompts = [patches_diff_list[index] for index in pending_indices]
        # A fallback model starts a new batch, so clear the previous attempt's line first.
        # Otherwise it stays on screen at "N of N" and the new attempt's first chunk rewinds
        # it; the chunks an earlier attempt finished are counted through `completed` below.
        previous_progress = getattr(self, "_chunk_progress", None)
        if previous_progress is not None:
            await previous_progress.reset_to_base()
        self._chunk_progress = self._chunk_progress_reporter(
            len(patches_diff_list),
            completed=len(patches_diff_list) - len(pending_indices),
        )
        predictions = await run_chunk_batch(
            prompts,
            lambda prompt: self._get_prediction(model, prompt),
            reporter=self._chunk_progress,
        )

        chunk_errors = []
        for chunk_index, prediction in zip(pending_indices, predictions, strict=True):
            if isinstance(prediction, Exception):
                chunk_errors.append(prediction)
                get_logger().warning(f"Failed to review chunk {chunk_index + 1}; retaining successful chunks",
                                     artifact={"error": prediction})
                continue
            try:
                data = self._load_valid_review_yaml(prediction, source=f"review chunk {chunk_index + 1}")
            except Exception as error:
                chunk_errors.append(error)
                get_logger().warning(f"Failed to parse review chunk {chunk_index + 1}; retrying it with fallback",
                                     artifact={"error": error})
                continue
            self._validate_review_schema(data)
            chunk_results[chunk_index] = (prediction, data, model)
        self._chunked_results = chunk_results
        if self._chunk_progress is not None:
            await self._chunk_progress.set_failed(len(patches_diff_list) - len(chunk_results))

        if len(chunk_results) < len(patches_diff_list):
            if chunk_errors:
                raise chunk_errors[0]
            raise FallbackEligibleError("No valid review output was produced for one or more chunks")

        return self._merge_cached_review_chunks()

    def _resize_pending_review_chunks(self, model: str) -> None:
        """Split oversized pending chunks at file boundaries while preserving result order."""
        chunks = self._chunked_patches_diff_list
        results = getattr(self, "_chunked_results", {})
        attempt_budget = self._review_attempt_budget(model)
        chunk_limit = attempt_budget.available_tokens(
            OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
            preserve_minimum=True,
        )
        max_calls = get_settings().pr_reviewer.get("max_number_of_calls", 3)
        resized, retained = [], {}
        for index, chunk in enumerate(chunks):
            if index in results:
                retained[len(resized)] = results[index]
                resized.append(chunk)
                continue
            if attempt_budget.count_tokens(chunk) <= chunk_limit:
                resized.append(chunk)
                continue
            sections = re.split(r"(?=^## File: ')", chunk, flags=re.MULTILINE)
            parts, current = [], ""
            for section in sections:
                if current and attempt_budget.count_tokens(current + section) > chunk_limit:
                    parts.append(current)
                    current = ""
                current += section
            if current:
                parts.append(current)
            reserved = len(chunks) - index - 1
            if (parts and len(resized) + len(parts) + reserved <= max_calls
                    and all(attempt_budget.count_tokens(part) <= chunk_limit for part in parts)):
                resized.extend(parts)
            else:
                # Retain unsplittable work for a later model and report it as failed if none can fit it.
                resized.append(chunk)
        self._chunked_patches_diff_list = resized
        self._chunked_results = retained

    def _include_newly_reviewable_files(self, model: str) -> None:
        """Add newly fitting files to pending work without resending successful chunks."""
        chunks = self._chunked_patches_diff_list
        results = getattr(self, "_chunked_results", {})
        remaining = self._chunked_remaining_files_list
        pending = [index for index in range(len(chunks)) if index not in results]
        attempt_budget = self._review_attempt_budget(model)
        chunk_limit = attempt_budget.available_tokens(
            OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
            preserve_minimum=True,
        )
        max_calls = get_settings().pr_reviewer.get("max_number_of_calls", 3)
        included = set()
        for section in re.split(r"(?=^## File: ')", self.patches_diff or "", flags=re.MULTILINE):
            match = re.match(r"## File: '(.*)'\n", section)
            if not match or match[1] not in remaining or match[1] in included:
                continue
            for index in pending:
                combined = chunks[index] + "\n\n" + section
                if attempt_budget.count_tokens(combined) <= chunk_limit:
                    chunks[index] = combined
                    included.add(match[1])
                    break
            else:
                if len(chunks) < max_calls and attempt_budget.count_tokens(section) <= chunk_limit:
                    pending.append(len(chunks))
                    chunks.append(section)
                    included.add(match[1])
        self._chunked_remaining_files_list = [name for name in remaining if name not in included]

    def _review_attempt_budget(self, model: str) -> AttemptTokenBudget:
        """Return the model-bound budget used for review chunk planning and dispatch."""
        return AttemptTokenBudget.for_attempt(
            model,
            self.token_handler,
            output_token_reserve=getattr(self.ai_handler, "get_output_token_reserve", None),
        )

    def _merge_cached_review_chunks(self) -> bool:
        """Merge successful chunks in order, retaining incomplete coverage after exhausted retries."""
        chunk_results = getattr(self, "_chunked_results", {})
        if not chunk_results:
            return False

        # Keep raw text for logging only; use the merged verdict from self.prediction_data.
        indices = sorted(chunk_results)
        raw_predictions = [chunk_results[index][0] for index in indices]
        # Filter before merge_review_chunks cuts to num_max_findings, so low-confidence findings cannot
        # take the place of a confident one from another chunk. Cached chunks stay raw (a fallback attempt
        # reuses them), so filter copies and recount on every merge.
        self.low_confidence_filtered_count = 0
        chunk_outputs = [copy.deepcopy(chunk_results[index][1]) for index in indices]
        for chunk_output in chunk_outputs:
            self._filter_low_confidence_findings(chunk_output)
        self.prediction = "\n".join(raw_predictions)
        try:
            max_findings = int(get_settings().pr_reviewer.num_max_findings)
        except (TypeError, ValueError):
            max_findings = None
        raw_vars = getattr(self, "_raw_prompt_vars", None) or getattr(self, "vars", None) or {}
        self.prediction_data = merge_review_chunks(
            chunk_outputs, related_tickets=raw_vars.get("related_tickets"), max_findings=max_findings)
        self.review_all_issues = all_key_issues(chunk_outputs)
        self.review_chunk_hit_limit = (
            None if max_findings is None else chunk_hit_findings_limit(chunk_outputs, max_findings))
        self.review_chunk_count = len(self._chunked_patches_diff_list)
        self.review_failed_chunk_count = self.review_chunk_count - len(chunk_results)
        self.remaining_files_list = self._chunked_remaining_files_list
        self.partial_files_list = self._chunked_partial_files_list
        models = list(dict.fromkeys(chunk_results[index][2] for index in indices))
        details = get_run_details()
        if details is not None:
            details.models_used = models
            for model in models:
                record_model_used(model, is_fallback=model != self._chunked_primary_model)
        return True

    async def _get_prediction(self, model: str, patches_diff: str | None = None) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.
            patches_diff: The diff to review. Defaults to the whole prepared diff; the chunked
                flow passes one chunk per call.

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(self.vars)
        patches_diff = self.patches_diff if patches_diff is None else patches_diff
        budget = self._review_attempt_budget(model)
        fitted = budget.fit_prompt_variable(
            variables,
            "diff",
            patches_diff,
            ai_handler=self.ai_handler,
            default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
            preserve_minimum=True,
            require_full_text=True,
        )
        if fitted.optional_text != patches_diff:
            raise FallbackEligibleError(
                f"The complete packed review diff does not fit the token limit for {model}"
            )

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=fitted.system_prompt,
            user=fitted.user_prompt,
        )
        if finish_reason == "length":
            raise FallbackEligibleError(f"Review response was truncated by {model}")

        return response

    @staticmethod
    def _load_review_yaml(prediction: str) -> dict:
        return load_yaml(prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "risk_level:",
                                        "merge_recommendation:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:", "existing_code:",
                                        "improved_code:"],
                        first_key='review', last_key='security_concerns')

    def _validate_review_schema(self, data: object) -> bool:
        is_valid = True
        try:
            PRReview.model_validate(data)
        except ValidationError as error:
            is_valid = False
            for err in error.errors():
                field_path = ".".join(str(part) for part in err.get("loc", ())) or "$"
                value = None if err.get("type") == "missing" else err.get("input")
                get_logger().warning(
                    "Review output failed schema validation",
                    artifact={
                        "field": field_path,
                        "value": value,
                    },
                )

        if isinstance(data, dict) and isinstance(data.get("review"), dict):
            review = data["review"]
            required_fields = (
                ("ticket_compliance_check", "related_tickets"),
                ("estimated_effort_to_review_[1-5]", "require_estimate_effort_to_review"),
                ("risk_level", "require_risk_assessment"),
                ("merge_recommendation", "require_merge_recommendation"),
                ("failure_modes", "require_failure_modes"),
                ("artifact_findings", "require_artifact_findings"),
                ("review_priority_files", "require_priority_files"),
                ("contribution_time_cost_estimate", "require_estimate_contribution_time_cost"),
                ("score", "require_score"),
                ("relevant_tests", "require_tests"),
                ("security_concerns", "require_security_review"),
                ("todo_sections", "require_todo_scan"),
                ("can_be_split", "require_can_be_split_review"),
            )
            vars_ = getattr(self, "vars", {})
            for field_name, setting_name in required_fields:
                if not vars_.get(setting_name) or field_name in review and review[field_name] is not None:
                    continue
                is_valid = False
                get_logger().warning(
                    "Review output failed schema validation",
                    artifact={"field": f"review.{field_name}", "value": None},
                )
        return is_valid

    @classmethod
    def _load_valid_review_yaml(cls, prediction: str, *, source: str = "model response") -> dict:
        """Parse one prediction and require the minimum publishable review shape."""
        data = cls._load_review_yaml(prediction)
        if not isinstance(data, dict) or not isinstance(data.get("review"), dict) or not data["review"]:
            raise FallbackEligibleError(f"{source} did not contain a non-empty review mapping")
        return data

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        self._merge_recommendation_status = None
        data = self.prediction_data if self.prediction_data is not None else self._load_review_yaml(self.prediction)
        if isinstance(data, dict) and isinstance(data.get("review"), dict):
            if not getattr(self, "vars", {}).get("require_failure_modes", False):
                data["review"].pop("failure_modes", None)
            if not getattr(self, "vars", {}).get("require_artifact_findings", False):
                data["review"].pop("artifact_findings", None)
            for key in [k for k in data["review"]
                        if _is_structural_reach_key(k)
                        or _is_risk_checks_key(k)
                        or _is_merge_recommendation_checks_key(k)]:
                data["review"].pop(key)
        if self.prediction_data is None:
            self._validate_review_schema(data)
            self._filter_low_confidence_findings(data)
        if isinstance(data, dict) and isinstance(data.get("review"), dict) and "failure_modes" in data["review"]:
            data["review"]["failure_modes"] = parse_failure_modes(data["review"]["failure_modes"])
        if isinstance(data, dict) and isinstance(data.get("review"), dict) and "artifact_findings" in data["review"]:
            data["review"]["artifact_findings"] = parse_artifact_findings(
                data["review"]["artifact_findings"], getattr(self, "vars", {}).get("artifact_context")
            )
        github_action_output(data, 'review')

        if not isinstance(data, dict) or not isinstance(data.get('review'), dict) or not data['review']:
            if self._review_finding_state_enabled():
                self._review_state_blocked = True
                self._review_state_block_reason = _STATE_BLOCK_REVIEW_DATA
                get_logger().warning("Review data is invalid; preserving persistent finding state")
            get_logger().exception("Failed to parse review data", artifact={"data": data})
            return ""

        structural_reach = build_structural_reach(resolve_artifact_path)
        if structural_reach:
            data["review"] = {"structural_reach": structural_reach, **data["review"]}

        diff_files = self.git_provider.get_diff_files()
        require_merge_recommendation = getattr(self, "vars", {}).get("require_merge_recommendation", False)
        unfiltered_files = None
        verdict_diff_files = None
        if require_merge_recommendation:
            get_verdict_diff_files = getattr(self.git_provider, "get_verdict_diff_files", None)
            try:
                verdict_diff_files = get_verdict_diff_files() if callable(get_verdict_diff_files) else None
            except Exception as error:
                get_logger().warning(f"Could not retrieve the complete PR diff for merge recommendation: {error}")
                verdict_diff_files = None
            risk_diff_files = verdict_diff_files
        else:
            risk_diff_files = diff_files
        if hasattr(self.git_provider, "get_pr_file_paths"):
            try:
                unfiltered_files = self.git_provider.get_pr_file_paths()
            except Exception:
                unfiltered_files = None
        if not unfiltered_files and hasattr(self.git_provider, "get_files"):
            try:
                unfiltered_files = self.git_provider.get_files()
            except Exception:
                unfiltered_files = None

        filtered_names = getattr(self.git_provider, "get_filtered_diff_file_names", lambda: [])()
        if filtered_names:
            unfiltered_files = list(unfiltered_files or []) + list(filtered_names)

        # The model's own answers, before a computed verdict replaces merge_recommendation in the review.
        self._approval_model_fields = (data["review"].get("merge_recommendation"), data["review"].get("risk_level"))
        risk_checks = build_risk_checks(
            risk_diff_files,
            unfiltered_files=unfiltered_files,
            include_clean=require_merge_recommendation,
        )
        if require_merge_recommendation and verdict_diff_files is None:
            risk_checks = None
        if risk_checks and risk_checks.get("has_findings"):
            data["review"] = {"risk_checks": risk_checks, **data["review"]}

        if require_merge_recommendation:
            verdict = compute_merge_verdict(
                self.git_provider,
                verdict_diff_files,
                model_recommendation=data["review"].get("merge_recommendation"),
                model_risk_level=data["review"].get("risk_level"),
                risk_checks=risk_checks,
            )
            self._merge_recommendation_status = verdict["recommendation"]
            data["review"] = _with_merge_verdict(data["review"], verdict)

        structured_publisher = getattr(self.git_provider, "publish_structured_review", None)
        if callable(structured_publisher) or review_json_output_path.get():
            # Deep-copy the data: dict(data) is shallow, so structured_data["review"]
            # would alias data["review"], which is mutated right below (key reordering).
            # Hand implementers an isolated snapshot, since the hook is provider-neutral
            # and a provider that defers serialization would observe the mutation.
            structured_data = copy.deepcopy(data)
            details = get_run_details()
            usage = {}
            if details is not None and details.has_token_usage:
                usage = {
                    "prompt_tokens": details.prompt_tokens,
                    "completion_tokens": details.completion_tokens,
                    "total_tokens": details.total_tokens,
                }
            structured_data["usage"] = usage
            # Expose analysis coverage to consumers that never see the review comment.
            structured_data["coverage"] = {
                "total_chunks": self.review_chunk_count,
                "failed_chunks": self.review_failed_chunk_count,
                "files_not_reviewed": list(self.remaining_files_list or []),
                "files_partially_reviewed": list(self.partial_files_list or []),
            }
            self._structured_review_data = structured_data
            if callable(structured_publisher):
                structured_publisher(structured_data)

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            data['review']['key_issues_to_review'] = key_issues_to_review

        self._prepare_review_finding_state(data)
        # Inline publication returns a pruned copy; the external sinks get the complete review, as /improve's do.
        complete_data = data
        if get_settings().config.publish_output and get_settings().pr_reviewer.get('inline_key_issues', True):
            data = self._publish_key_issues_as_inline_comments(data)
        issues = data['review'].get('key_issues_to_review')
        self._review_has_findings = isinstance(issues, list) and any(
            isinstance(issue, dict) and issue for issue in issues)

        incremental_review_markdown_text = None
        commit_review_markdown_text = None
        if self._review_commit is not None:
            commit = self._review_commit
            sha_link = f"[`{commit.sha[:7]}`]({commit.url})" if commit.url else f"`{commit.sha[:7]}`"
            subject = commit.message.strip().split("\n", 1)[0]
            commit_review_markdown_text = f"{sha_link} {_markdown_code_span(subject)}" if subject else sha_link
        elif self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                              f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                            incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files(),
                                               commit_review=commit_review_markdown_text)

        # The 'since the last review' delta renders what reconciliation already proved, so
        # the first thing a reader sees after the heading is what happened to the previous
        # review's findings — under the heading, not above it, so the persistent comment's
        # identity marker and update note keep their anchors on the first line. It is part
        # of the sized body, and the first thing sacrificed when the persistent state does
        # not fit: advancing the state outranks showing the delta.
        lifecycle_summary = ""
        if get_settings().pr_reviewer.get("show_previous_findings_summary", True):
            lifecycle_summary = render_lifecycle_summary(
                self._review_finding_previous_state, self._review_state_result
            )
        under_heading = "\n\n".join(part for part in (self.alias_notice, lifecycle_summary) if part)
        if under_heading:
            summary_block = f"\n\n{under_heading}\n\n"
            heading, _, rest = (markdown_text or "").partition("\n")
            markdown_text = f"{heading}{summary_block}{rest}" if rest else f"{heading}{summary_block.strip()}"

        if self.review_chunk_count > 1:
            markdown_text += (
                "\n\n<hr>\n\n"
                "ℹ️ **Chunked review:** the diff exceeded the model token budget, so it was reviewed in "
                f"{self.review_chunk_count} chunks and the per-chunk results were merged."
            )
            if self.review_failed_chunk_count:
                markdown_text += (f" {self.review_failed_chunk_count} chunk(s) failed and are not covered "
                                  "by this review.")

        if get_settings().pr_reviewer.enable_review_coverage_footer:
            for files, explanation in (
                (self.remaining_files_list, "were not included in this review because of the token budget"),
                (self.partial_files_list,
                 "had patches clipped before analysis to fit the token budget (partial input coverage)"),
            ):
                if not files:
                    continue
                displayed_files = files[:MAX_REVIEW_COVERAGE_FILES]
                markdown_text += (
                    "\n\n<hr>\n\n"
                    f"⚠️ **Review coverage:** The following files {explanation}:\n"
                    + "\n".join(f"- {_markdown_code_span(file)}" for file in displayed_files)
                )
                remaining_count = len(files) - len(displayed_files)
                if remaining_count:
                    markdown_text += f"\n... and {remaining_count} more"

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Command usage guide:</strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Output the fallback-model note, and the agent run details (model, tokens, time cost) if enabled
        markdown_text = append_run_details(markdown_text, self.git_provider)

        if self._review_state_result is not None:
            state_result = self._review_state_result
            try:
                markdown_text = append_review_state(
                    markdown_text or "",
                    state_result.state,
                    max_chars=self._review_comment_max_chars(),
                )
            except ValueError as error:
                # The delta section is the first thing to sacrifice: advancing the state
                # outranks showing it.
                degraded = True
                if lifecycle_summary:
                    body_without_summary = (markdown_text or "").replace(
                        f"\n\n{lifecycle_summary}\n\n", "\n", 1
                    ).replace(f"\n\n{lifecycle_summary}", "", 1)
                    try:
                        markdown_text = append_review_state(
                            body_without_summary,
                            state_result.state,
                            max_chars=self._review_comment_max_chars(),
                        )
                        degraded = False
                    except ValueError:
                        markdown_text = body_without_summary
                if degraded:
                    previous_state = getattr(self, "_review_finding_previous_state", None)
                    self._review_state_result = None
                    self._review_state_blocked = True
                    self._review_state_block_reason = _STATE_BLOCK_SIZE
                    get_logger().warning(
                        f"Persistent review state did not fit the provider comment limit; "
                        f"publishing the review without advancing state: {error}"
                    )
                    if previous_state is not None:
                        try:
                            markdown_text = append_review_state(
                                markdown_text or "",
                                previous_state,
                                max_chars=self._review_comment_max_chars(),
                            )
                        except ValueError as previous_error:
                            get_logger().warning(
                                f"Previous persistent review state also did not fit the provider "
                                f"comment limit; leaving the existing state untouched: {previous_error}"
                            )
                        else:
                            self._review_state_preserved = True

        # Emit the review to optional external sinks (stdout/file/webhook/slack/telegram); no-op unless enabled.
        # publish_output gates it so a dry run makes no external calls. The "no major issues"
        # suppression deliberately does not: that only silences the PR comment.
        # The delivery itself is left to run(), which awaits it off the event loop.
        if get_settings().config.publish_output:
            sink_markdown = markdown_text
            if complete_data is not data:
                sink_markdown = convert_to_markdown_v2(
                    complete_data, self.git_provider.is_supported("gfm_markdown"),
                    incremental_review_markdown_text, git_provider=self.git_provider,
                    files=self.git_provider.get_diff_files(), commit_review=commit_review_markdown_text)
            self._pending_sink_output = ("review", complete_data.get('review', {}), sink_markdown)

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text is None or len(markdown_text) == 0:
            markdown_text = ""

        return markdown_text

    async def _deliver_sink_output(self) -> None:
        """Deliver the prepared review to the external sinks, if there is one.

        The sinks do blocking HTTP and file I/O, so the delivery runs in a worker
        thread. Awaiting it keeps the event loop responsive and still delivers the
        output before the command returns.
        """
        if hosted_markdown_export.get():
            self._pending_sink_output = None
            return
        pending = getattr(self, "_pending_sink_output", None)
        if pending is None:
            return
        self._pending_sink_output = None
        message_type, payload, markdown = pending
        await push_outputs_async(
            message_type, payload=payload, markdown=markdown, git_provider=self.git_provider
        )

    def _filter_low_confidence_findings(self, data) -> None:
        """Drop findings below `min_finding_confidence`, counting them in `low_confidence_filtered_count`."""
        review = data.get("review") if isinstance(data, dict) else None
        issues = review.get("key_issues_to_review") if isinstance(review, dict) else None
        if not isinstance(issues, list):
            return
        threshold = get_min_finding_confidence()
        if threshold <= 0:
            return
        kept = []
        for issue in issues:
            confidence = finding_confidence(issue)
            if confidence is not None and confidence < threshold:
                continue
            kept.append(issue)
        dropped = len(issues) - len(kept)
        if dropped:
            get_logger().info(f"Dropped {dropped} finding(s) below min_finding_confidence={threshold:g}")
            review["key_issues_to_review"] = kept
            self.low_confidence_filtered_count += dropped

    def _build_key_issue_comment(self, finding: Finding, diff_files: dict) -> dict | None:
        relevant_file = finding.path
        start_line = finding.line_start or 0
        end_line = finding.line_end or start_line

        if not relevant_file or not finding.body or start_line < 1 or end_line < start_line:
            get_logger().warning("Review finding has no usable location, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        file = diff_files.get(relevant_file) or diff_files.get(relevant_file.lstrip("/"))
        if file is None:
            get_logger().warning("Review finding points at a file that is not in the diff, "
                                 "keeping it in the summary", artifact={"relevant_file": relevant_file})
            return None
        if not is_line_range_in_diff(file, start_line, end_line):
            get_logger().warning("Review finding points past the end of the file, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        finding = replace(
            finding,
            path=file.filename.strip(),
            line_start=start_line,
            line_end=end_line,
        )
        body = finding.rendered_body
        improved_code = (finding.improved_code or "").strip("\n")
        if not improved_code.strip():
            return finding.to_inline_comment(body, fallback_to_pr_comment=False)

        existing_code = _strip_line_numbers(finding.existing_code, start_line, end_line)
        # Keep leading whitespace: an indentation-only change is a real fix in Python.
        if existing_code and _code_lines(existing_code) == _code_lines(improved_code):
            return finding.to_inline_comment(body, fallback_to_pr_comment=False)
        fence = get_suggestion_fence(improved_code)
        original_suggestion = None
        # The prompt asks for fixes of at most ten lines; a longer one is shown, never offered as a commit.
        if get_committable_code_suggestions() and len(_code_lines(improved_code)) <= _MAX_COMMITTABLE_FIX_LINES:
            is_applicable, _, has_valid_anchor = validate_suggestion(file, start_line, end_line, existing_code)
            if has_valid_anchor:
                if existing_code:
                    # Align both snippets with one offset, so an indentation change in the fix survives.
                    existing_code = existing_code.strip("\n")
                    existing_count = len(split_diff_lines(existing_code))
                    aligned = split_diff_lines(
                        dedent_code_for_file(file, start_line, f"{existing_code}\n{improved_code}"))
                    existing_code = "\n".join(aligned[:existing_count])
                    improved_code = "\n".join(aligned[existing_count:])
                else:
                    improved_code = dedent_code_for_file(file, start_line, improved_code)
            if is_applicable and python_syntax_is_valid(
                    finding.path, file, start_line, end_line, improved_code) is not False:
                fence = get_suggestion_fence(improved_code)
                body += f"\n\n{fence}suggestion\n{improved_code}\n{fence}"
                original_suggestion = {
                    "existing_code": existing_code,
                    "improved_code": improved_code,
                    "relevant_file": finding.path,
                    "relevant_lines_start": start_line,
                    "relevant_lines_end": end_line,
                    "suggestion_content": finding.body,
                    "label": finding.header or "",
                }
        if original_suggestion is None:
            body += f"\n\n{fence}{TEXT_FIX_INFO}\n{improved_code}\n{fence}"
        return finding.to_inline_comment(
            body,
            fallback_to_pr_comment=False,
            original_suggestion=original_suggestion,
        )

    def _can_verify_inline_key_issue_publication(self) -> bool:
        return can_verify_inline_comment_publication(self.git_provider)

    def _published_inline_key_issue_fingerprints(self, store: InlineCommentStore,
                                                 fingerprints: set[str]) -> set[str]:
        try:
            for body in self.git_provider.get_recent_inline_comment_bodies():
                store.add_body(body)
        except Exception as e:
            get_logger().warning(
                f"Inline key-issue publishing cannot verify newly published comments, error: {e}; "
                "keeping findings in the review summary")
            return set()
        return {fingerprint for fingerprint in fingerprints if store.seen(fingerprint)}

    def _publish_key_issues_as_inline_comments(self, data: dict) -> dict:
        issues = (data.get("review") or {}).get("key_issues_to_review")
        if not isinstance(issues, list) or not issues:
            return data
        if not self._can_verify_inline_key_issue_publication():
            get_logger().info("Inline key-issue publishing is not verifiable for this provider; "
                              "keeping findings in the review summary")
            return data

        diff_files = {}
        for file in self.git_provider.get_diff_files() or []:
            if not file.filename:
                continue
            path = file.filename.strip()
            diff_files[path] = file
            diff_files.setdefault(path.lstrip("/"), file)
        store = get_inline_comment_store(self.git_provider)
        store.load()
        if store.load_failed:
            get_logger().warning("Inline key-issue publishing cannot verify existing provider comments; "
                                 "keeping findings in the review summary")
            return data
        remaining_issues = []
        candidate_comments = {}
        candidate_issues = {}
        candidate_fingerprints = {}
        published = 0
        for issue in issues:
            try:
                finding = Finding.from_review_issue(issue)
                if finding is None:
                    remaining_issues.append(issue)
                    continue
                comment = self._build_key_issue_comment(finding, diff_files)
                if comment is None:
                    remaining_issues.append(issue)
                    continue
                fingerprint = key_issue_fingerprint(comment["relevant_file"], finding.rendered_body)
                if store.seen(fingerprint):
                    published += 1
                    continue
                location_fingerprint = key_issue_location_fingerprint(
                    fingerprint, comment["relevant_lines_start"], comment["relevant_lines_end"])
                if location_fingerprint in candidate_comments:
                    candidate_issues[location_fingerprint].append(issue)
                    continue
                max_chars = next(
                    (getattr(self.git_provider, attr) for attr in
                     ("max_comment_chars", "max_comment_length")
                     if isinstance(getattr(self.git_provider, attr, None), int)),
                    None,
                )
                comment["body"] = key_issue_body_with_markers(
                    comment["body"], fingerprint, location_fingerprint,
                    max_chars, self.git_provider)
                candidate_comments[location_fingerprint] = comment
                candidate_issues[location_fingerprint] = [issue]
                candidate_fingerprints[location_fingerprint] = fingerprint
            except Exception as e:
                get_logger().warning(f"Failed to prepare a review finding for inline publication, error: {e}",
                                     artifact={"issue": issue})
                remaining_issues.append(issue)

        if candidate_comments:
            try:
                batch = list(candidate_comments.values())
                already_posted = set()
                try:
                    batch_published = self.git_provider.publish_code_suggestions(batch)
                except Exception as e:
                    get_logger().warning(f"Batch publication of review findings raised, error: {e}")
                    batch_published = False
                    # The batch may have posted some comments before it raised: retry only the others.
                    already_posted = self._published_inline_key_issue_fingerprints(store, set(candidate_comments))
                if not batch_published:
                    get_logger().info("Failed to publish review findings, trying to publish each one separately")
                    for location_fingerprint, comment in candidate_comments.items():
                        if location_fingerprint in already_posted:
                            continue
                        try:
                            self.git_provider.publish_code_suggestions([comment])
                        except Exception as e:
                            get_logger().warning(
                                f"Failed to publish a review finding as an inline comment, error: {e}",
                                artifact={"relevant_file": comment["relevant_file"],
                                          "start_line": comment["relevant_lines_start"],
                                          "end_line": comment["relevant_lines_end"]})
            except Exception as e:
                locations = [{"relevant_file": comment["relevant_file"],
                              "start_line": comment["relevant_lines_start"],
                              "end_line": comment["relevant_lines_end"]}
                             for comment in candidate_comments.values()]
                get_logger().warning(
                    f"Failed to publish review findings as inline comments, error: {e}",
                    artifact={"locations": locations})
            verified_locations = self._published_inline_key_issue_fingerprints(store, set(candidate_comments))
            for location_fingerprint, comment in candidate_comments.items():
                issues_for_location = candidate_issues[location_fingerprint]
                if location_fingerprint in verified_locations:
                    store.add(candidate_fingerprints[location_fingerprint])
                    store.add(location_fingerprint)
                    published += len(issues_for_location)
                    continue
                get_logger().warning("Failed to publish a review finding as an inline comment, "
                                     "keeping it in the summary",
                                     artifact={"relevant_file": comment["relevant_file"],
                                               "start_line": comment["relevant_lines_start"],
                                               "end_line": comment["relevant_lines_end"]})
                remaining_issues.extend(issues_for_location)

        if not published:
            return data
        get_logger().info(f"Published {published} review finding(s) as inline comments")

        data = copy.deepcopy(data)
        if remaining_issues:
            data["review"]["key_issues_to_review"] = remaining_issues
        else:
            data["review"].pop("key_issues_to_review", None)
        return data

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        if not hasattr(self.git_provider, "get_incremental_commits"):
            get_logger().info(f"Incremental review is not supported for {get_settings().config.git_provider}")
            return False
        if self.incremental.commits_range is None:
            get_logger().info(
                f"Incremental review not initialized for {get_settings().config.git_provider}; "
                f"falling back to full review."
            )
            self.incremental.is_incremental = False
            return False
        # checking if there are enough commits to start the review
        num_new_commits = len(self.incremental.commits_range)
        num_commits_threshold = get_settings().pr_reviewer.minimal_commits_for_incremental_review
        not_enough_commits = num_new_commits < num_commits_threshold
        # checking if the commits are not too recent to start the review
        recent_commits_threshold = (
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            - datetime.timedelta(minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review)
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
        # PyGithub returns timezone-aware UTC commit dates; the threshold below is a
        # naive datetime. Normalize to naive UTC so the comparison cannot raise
        # TypeError, matching how the GitLab and Azure providers emit commit dates.
        if last_seen_commit_date is not None and last_seen_commit_date.tzinfo is not None:
            last_seen_commit_date = last_seen_commit_date.astimezone(datetime.UTC).replace(tzinfo=None)
        all_commits_too_recent = (
            last_seen_commit_date > recent_commits_threshold if self.incremental.last_seen_commit else False
        )
        # check all the thresholds or just one to start the review
        condition = any if get_settings().pr_reviewer.require_all_thresholds_for_incremental_review else all
        if condition((not_enough_commits, all_commits_too_recent)):
            get_logger().info(
                f"Incremental review is enabled for {self.pr_url} but didn't pass the threshold check to run:"
                f"\n* Number of new commits = {num_new_commits} (threshold is {num_commits_threshold})"
                f"\n* Last seen commit date = {last_seen_commit_date} (threshold is {recent_commits_threshold})"
            )
            return False
        return True

    def set_review_labels(self, data):
        if not get_settings().config.publish_output or self._review_commit is not None:
            return

        if not get_settings().pr_reviewer.require_estimate_effort_to_review:
            get_settings().pr_reviewer.enable_review_labels_effort = False # we did not generate this output
        if not get_settings().pr_reviewer.require_security_review:
            get_settings().pr_reviewer.enable_review_labels_security = False # we did not generate this output

        if ((get_settings().pr_reviewer.enable_review_labels_security or
                get_settings().pr_reviewer.enable_review_labels_effort) and
                self.git_provider.is_supported("get_labels")):
            try:
                review_labels = []
                has_valid_effort_estimate = False
                has_valid_security_verdict = False
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review'].get('estimated_effort_to_review_[1-5]')
                    estimated_effort_number = None
                    if estimated_effort is None:
                        get_logger().warning("Missing estimated_effort_to_review_[1-5] in review data")
                    elif isinstance(estimated_effort, str):
                        try:
                            estimated_effort_number = int(estimated_effort.split(',')[0])
                        except ValueError:
                            get_logger().warning(f"Invalid estimated_effort value: {estimated_effort}")
                    elif isinstance(estimated_effort, int):
                        estimated_effort_number = estimated_effort
                    else:
                        get_logger().warning(f"Unexpected type for estimated_effort: {type(estimated_effort)}")
                    if estimated_effort_number is not None:
                        estimated_effort_number = max(1, min(5, int(estimated_effort_number)))
                        review_labels.append(f'Review effort {estimated_effort_number}/5')
                        has_valid_effort_estimate = True
                if (
                        get_settings().pr_reviewer.enable_review_labels_security
                        and get_settings().pr_reviewer.require_security_review
                    ):
                    security_concerns = data['review'].get('security_concerns')
                    if security_concerns is None:
                        get_logger().warning("Missing security_concerns in review data")
                    else:
                        has_valid_security_verdict = True
                        if not is_value_no(security_concerns):
                            review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if current_labels is None:
                    # The read failed with no snapshot to fall back on. publish_labels
                    # replaces the whole set, so publishing would delete human labels.
                    get_logger().error(
                        "Skipping review label publish: existing labels could not be read, "
                        "and publishing would remove them")
                    return
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels:\n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if
                                               (not (label.lower().startswith('review effort')
                                                     and has_valid_effort_estimate) and
                                                not (label.lower().startswith(
                                                    'possible security concern') and has_valid_security_verdict))]
                else:
                    current_labels_filtered = []
                new_labels = review_labels + current_labels_filtered
                if (current_labels or review_labels) and sorted(new_labels) != sorted(current_labels):
                    get_logger().info(f"Setting review labels:\n{review_labels + current_labels_filtered}")
                    self.git_provider.publish_labels(new_labels)
                else:
                    get_logger().info(f"Review labels are already set:\n{review_labels + current_labels_filtered}")
            except Exception as e:
                get_logger().error(f"Failed to set review labels, error: {e}")

    def auto_approve_logic(self, partial_review_error=None, review_completed=False) -> None:
        """Approve the reviewed commit when every gated-approval check passes, otherwise
        dismiss any approval PR-Agent holds, since an earlier run's approval no longer stands."""
        settings = get_settings()
        if not self._auto_approval_enabled():
            return
        if not _as_bool_setting(settings.pr_reviewer.get("require_merge_recommendation", False)):
            get_logger().warning("enable_auto_approval needs pr_reviewer.require_merge_recommendation; not approving")
        model_recommendation, model_risk_level = getattr(self, "_approval_model_fields", (None, None))
        allowed, reason = approval_allowed(
            review_completed=review_completed,
            verdict=getattr(self, "_merge_recommendation_status", None),
            model_merge_recommendation=model_recommendation,
            model_risk_level=model_risk_level,
            is_incremental=self.incremental.is_incremental,
            unreviewed_file_count=len(getattr(self, "remaining_files_list", []) or [])
            + len(getattr(self, "partial_files_list", []) or []),
            failed_chunk_count=getattr(self, "review_failed_chunk_count", 0),
            partial_review_error=partial_review_error,
            state_blocked=getattr(self, "_review_state_blocked", False),
        )
        bot_display_name = get_bot_display_name()
        if allowed:
            body = (f"Approved by {bot_display_name}: the computed merge verdict is `no_concerns_found` and "
                    "every gated-approval check passed on this commit.")
            try:
                if self.git_provider.auto_approve(body=body):
                    get_logger().info("Auto-approved PR")
                    return
                reason = "the provider did not approve the reviewed commit"
            except Exception as e:
                get_logger().exception(f"Auto-approval failed, error: {e}")
                reason = "the approval could not be submitted"
        get_logger().info(f"Not auto-approving: {reason}")
        self._withdraw_auto_approval(reason)

    @staticmethod
    def _auto_approval_enabled() -> bool:
        config = get_settings().config
        # A dry run writes nothing, approvals and dismissals included.
        return (_as_bool_setting(config.get("enable_auto_approval", False), setting_name="config.enable_auto_approval")
                and _as_bool_setting(config.get("publish_output", True), default=True))

    def _withdraw_auto_approval(self, reason: str) -> None:
        try:
            if not self.git_provider.revoke_auto_approval(f"{get_bot_display_name()} withdrew its approval: {reason}."):
                get_logger().error("Could not confirm that PR-Agent's earlier approvals were dismissed")
        except Exception as e:
            get_logger().exception(f"Auto-approval reconcile failed, error: {e}")
