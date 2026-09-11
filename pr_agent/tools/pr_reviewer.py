import asyncio
import contextlib
import copy
import datetime
import re
from functools import partial
from typing import Any, List, Optional, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.finding_verifier import UNVERIFIED_HEADER_SUFFIX, verify_findings
from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    get_inline_comment_store,
    key_issue_body_with_markers,
    key_issue_fingerprint,
    key_issue_location_fingerprint,
)
from pr_agent.algo.pr_processing import (
    ChunkPlan,
    add_ai_metadata_to_diff_files,
    get_pr_diff,
    get_pr_multi_diffs_with_files,
    retry_with_fallback_models,
)
from pr_agent.algo.prompt_fragments import render_diff_hunk_format
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.review_coverage import CoverageLedger, FileCoverage, patch_line_counts
from pr_agent.algo.review_finding_state import (
    append_review_state,
    parse_review_state,
    reconcile_review_findings,
    render_carried_section,
)
from pr_agent.algo.review_merge import merge_review_chunks, vote_review_samples
from pr_agent.algo.run_details import get_run_details, init_run_details, set_call_findings
from pr_agent.algo.run_ledger import write_ledger
from pr_agent.algo.ship_scope import (
    DEFAULT_LOW_PRIORITY_GLOBS,
    is_low_priority,
    order_files_by_priority,
    propose_ignore_globs,
    render_ignore_proposal,
)
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (
    ModelType,
    PRReviewHeader,
    PRReviewIdentity,
    add_pr_review_identity,
    convert_to_markdown_v2,
    get_pr_review_comment_identifiers,
    github_action_output,
    load_yaml,
    push_outputs,
    show_relevant_configurations,
    show_run_details,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR, get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets,
    fit_related_tickets_to_prompt_budget,
)

MAX_REVIEW_COVERAGE_FILES = 50
_SUGGESTION_FENCE_RE = re.compile(r"```[ \t]*suggestion\b", re.IGNORECASE)

_REVIEW_FAILURE_REASONS = (
    (
        ("credit balance is too low", "insufficient credits", "insufficient balance", "insufficient_quota"),
        "The model provider rejected the request because the API account has insufficient credits. "
        "Add credits, then retry the command.",
    ),
    (
        ("authenticationerror", "authentication error", "invalid api key", "invalid x-api-key"),
        "PR-Agent could not authenticate with the model provider. Check the configured API credentials, then retry.",
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
        "PR-Agent could not reach the model provider. Check provider availability and network access, then retry.",
    ),
    (
        ("failed to generate prediction with any model",),
        "Every configured model attempt failed. Check the PR-Agent service logs for the provider error, then retry.",
    ),
)
_UNKNOWN_REVIEW_FAILURE_REASON = (
    "PR-Agent encountered an unexpected internal error. Check the PR-Agent service logs for details."
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
    """Interpret configured boolean values without treating non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return False


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
    return f"Failed to review PR\n\n**Reason:** {reason}"


_STATE_BLOCK_INVALID_MARKER = "invalid_marker"
_STATE_BLOCK_READ_ERROR = "read_error"
_STATE_BLOCK_REVIEW_DATA = "review_data"
_STATE_BLOCK_SIZE = "state_size"


def _review_findings_count(data: Any) -> int:
    """How many key issues a parsed review dict reports, or 0 when the shape is missing/wrong."""
    review = data.get("review") if isinstance(data, dict) else None
    issues = review.get("key_issues_to_review") if isinstance(review, dict) else None
    return len(issues) if isinstance(issues, list) else 0


class UnparsableReview(ValueError):
    """The model answered, but nothing the YAML repair heuristics could rescue.

    Distinct from a call that failed: the chunked flow retries an unparsable chunk and then falls
    back to a single review call, where a transport failure is re-raised as the run's error.
    """


# One retry per failed chunk. Chunk failures cost findings, and both an unanswered call and
# unparsable YAML are usually transient; more attempts would multiply latency on a large PR.
CHUNK_REVIEW_ATTEMPTS = 2


def split_chunk_plan(plan: ChunkPlan, git_provider, token_handler, model: str) -> list[ChunkPlan]:
    """Split a chunk that failed every attempt on `model` into up to two smaller chunks, one per
    half of its files, each with its diff regenerated from scratch (so the token budget is
    re-applied to just that half rather than reusing the failed chunk's possibly-clipped diff).

    Returns `[plan]` unchanged - the exact same object - when it has only one file left to split,
    or when regenerating *every* half's diff produced nothing (e.g. every file turned out to be
    delete-only). That makes `result == [plan]` the caller's test for "could not usefully split
    this"; anything else is a valid split, including a single returned plan when only one of the
    two halves regenerated to content (the other half's files are the caller's to account for,
    e.g. as deletion_only or skipped_budget, since they are not covered by the returned plan(s)).
    """
    if len(plan.files) <= 1:
        return [plan]
    mid = len(plan.files) // 2
    file_halves = (plan.files[:mid], plan.files[mid:])
    all_diff_files = git_provider.get_diff_files()

    halves: list[ChunkPlan] = []
    for files_subset in file_halves:
        wanted = set(files_subset)
        subset_diff_files = [f for f in all_diff_files if f.filename in wanted]
        # max_calls=1: if a half still does not fit in one model call, take its first plan and
        # let whatever it could not fit go uncovered, same as the top-level chunker would.
        sub_plans, _ = get_pr_multi_diffs_with_files(
            git_provider, token_handler, model, max_calls=1, add_line_numbers=True,
            diff_files=subset_diff_files)
        if sub_plans:
            halves.append(sub_plans[0])
    return halves if halves else [plan]


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

    # State of the chunked and sampled flows, rebound by _prepare_prediction. Class-level
    # immutable defaults, so a partially built instance still reads consistently.
    prediction_data = None  # parsed review dict; None means "parse self.prediction instead"
    review_chunk_count = 1
    review_failed_chunk_count = 0
    # Findings that the consensus vote discarded for lack of agreement, or trimmed at
    # num_max_findings. Non-zero means this review is partial in the same way a failed chunk
    # makes it partial.
    review_vote_dropped_count = 0
    # Premise-verification outcomes (R-8). Reset when verification runs; zero when the flag is off.
    review_refuted_count = 0
    review_unverified_count = 0
    # Line-weighted record of what the review actually looked at, built once _prepare_prediction
    # has a diff to review. None means the run never got that far (parsing/plumbing tests that
    # exercise _prepare_pr_review directly, without going through _prepare_prediction first).
    coverage: Optional[CoverageLedger] = None
    # Per-chunk file/clip breakdown from the chunked flow; empty when the diff was not chunked.
    chunk_plans: list = None
    # Ship-scope (R-9): low-priority files summarized under budget, plus an optional [ignore] proposal.
    _ship_scope_summary_paths: list = None
    _ship_scope_ignore_footer: str = ""

    def __init__(self, pr_url: str, is_answer: bool = False, is_auto: bool = False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            is_answer (bool, optional): Indicates whether the review is being done in answer mode. Defaults to False.
            is_auto (bool, optional): Indicates whether the review is being done in automatic mode. Defaults to False.
            ai_handler (BaseAiHandler): The AI handler to be used for the review. Defaults to None.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
        """
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto

        if self.is_answer and not self.git_provider.is_supported("get_issue_comments"):
            raise Exception(f"Answer mode is not supported for {get_settings().config.git_provider} for now")
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.remaining_files_list = []
        self.coverage = None
        self.chunk_plans = []
        self.prediction = None
        self._review_state_result = None
        self._review_state_blocked = False
        self._review_state_block_reason = None
        self._review_finding_previous_state = None
        self._review_state_preserved = False
        question_str, answer_str = self._get_user_answers()
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
            "require_priority_files": get_settings().pr_reviewer.get("require_priority_files", False),
            "require_estimate_contribution_time_cost": get_settings().pr_reviewer.require_estimate_contribution_time_cost,
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'question_str': question_str,
            'answer_str': answer_str,
            "extra_instructions": get_settings().pr_reviewer.extra_instructions,
            "skills_context": get_skills_context(),
            "repo_context": build_repo_context(self.git_provider),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata": is_ai_metadata,
            "diff_hunk_format": render_diff_hunk_format(
                include_line_numbers=True,
                include_ai_metadata=is_ai_metadata,
            ),
            "related_tickets": get_settings().get('related_tickets', []),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
        }

        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            get_settings().pr_review_prompt.system,
            get_settings().pr_review_prompt.user
        )

    def parse_incremental(self, args: List[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i":
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        init_run_details()
        progress_response = None
        review_error = None
        review_failed = False
        persistent_write_failed = False
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                return None

            if self.incremental.is_incremental:
                can_run = self._can_run_incremental_review()
                # If the gate disabled incremental (e.g., commits_range is None), fall through to full review.
                if not can_run and self.incremental.is_incremental:
                    return None

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # ticket extraction if exists
            await extract_and_cache_pr_tickets(self.git_provider, self.vars)
            self._raw_prompt_vars = copy.deepcopy(self.vars)

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
                progress_response = self.git_provider.publish_comment("Preparing review...", is_temporary=True)

            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR,
                                             git_provider=self.git_provider)
            if not self.prediction:
                return None

            await self._verify_prediction_findings()
            pr_review = self._prepare_pr_review()
            get_logger().debug("PR output", artifact=pr_review)

            if not pr_review:
                raise ValueError("Failed to prepare review output")

            state_result = getattr(self, "_review_state_result", None)
            state_changed = bool(state_result and state_result.changed)
            state_blocked = getattr(self, "_review_state_blocked", False)
            should_publish = get_settings().config.publish_output and (
                self._should_publish_review_no_suggestions(pr_review)
                or state_changed
                or state_blocked
            )
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(reason)
                get_settings().data = {"artifact": pr_review}
                return

            # publish the review
            # Providers that support it (GitLab) can post the review's final comment as a resolvable thread.
            # This intent applies to the review only - never to status comments or the output of other tools.
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
                if self.git_provider.supports_review_comment_identity() is True:
                    identity_marker = (
                        PRReviewIdentity.INCREMENTAL.value
                        if self.incremental.is_incremental
                        else PRReviewIdentity.REGULAR.value
                    )
                    pr_review = add_pr_review_identity(pr_review, identity_marker)
                self.git_provider.publish_comment(pr_review, **review_thread_kwargs)
        except Exception as e:
            review_error = e
            review_failed = True
            get_logger().error(f"Failed to review PR: {e}")
            if get_settings().config.get("propagate_tool_errors", False):
                raise
        finally:
            if progress_response is not None:
                try:
                    self.git_provider.remove_comment(progress_response)
                except Exception as e:
                    get_logger().exception(f"Failed to remove review progress comment, error: {e}")
            if (
                review_failed
                and get_settings().config.publish_output
                and (
                    persistent_write_failed
                    or not get_settings().config.get("is_auto_command", False)
                )
            ):
                try:
                    self.git_provider.publish_comment(_review_failure_comment(review_error))
                except Exception as e:
                    get_logger().exception(f"Failed to publish review failure result, error: {e}")
            ledger_path = get_settings().config.get("run_ledger_path")
            if ledger_path:
                try:
                    details = get_run_details()
                    if details is not None:
                        write_ledger(details, ledger_path, run_id=self._review_run_id(), tool="review")
                except Exception as e:
                    get_logger().exception(f"Failed to write run ledger, error: {e}")

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
        publisher = getattr(provider, "publish_persistent_comment", None)
        if getattr(publisher, "__func__", None) is GitProvider.publish_persistent_comment:
            # Skip generic publishers; they only create comments and cannot safely carry lifecycle state.
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
            PRReviewIdentity.REGULAR.value,
            PRReviewIdentity.INCREMENTAL.value,
        }
        markerless_review = "\n".join(
            line
            for line in str(pr_review).splitlines()
            if line.strip() not in identity_markers
        ).strip()
        return (
            "## Standalone PR Review\n\n"
            "_PR-Agent could not safely update the persistent review. "
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

    def _persistent_review_comment_exists(self) -> Optional[bool]:
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
        provider = getattr(self, "git_provider", None)
        if provider is None:
            return False
        try:
            return (
                provider.supports_review_finding_state() is True
                and provider.is_supported("get_issue_comments") is True
            )
        except Exception as error:
            get_logger().warning(
                f"Review comment authorship is not available, error: {error}"
            )
            return False

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

    @staticmethod
    def _review_finding_from_issue(issue: dict) -> Optional[dict]:
        if not isinstance(issue, dict):
            return None
        path = str(issue.get("relevant_file") or issue.get("path") or "").strip()
        raw_content = issue.get("issue_content") or issue.get("body") or ""
        content = _SUGGESTION_FENCE_RE.sub("```text", str(raw_content).strip())
        header = str(issue.get("issue_header") or "").strip()
        if header.endswith(UNVERIFIED_HEADER_SUFFIX):
            header = header[: -len(UNVERIFIED_HEADER_SUFFIX)].rstrip()
        if header.lower() == "possible bug":
            header = "Possible Issue"
        if not path or not content:
            return None

        finding = {
            "path": path,
            "body": f"**{header}**\n\n{content}" if header else content,
        }
        try:
            start = int(str(issue.get("start_line", 0)).strip())
            end = int(str(issue.get("end_line", start)).strip())
        except (TypeError, ValueError):
            start, end = 0, 0
        if start > 0:
            finding["line_start"] = start
            finding["line_end"] = max(start, end)
        return finding

    @classmethod
    def _review_findings_from_data(cls, data: dict) -> Optional[list[dict]]:
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
                update_suffix = f"\n\n#### (Review updated until commit {self._review_run_id()})\n"
                # The shared persistent publisher adds the full-review identity
                # before inserting the update suffix. Reserve both pieces so a
                # complete state marker remains inside the provider limit.
                identity_overhead = len(PRReviewIdentity.REGULAR.value) + 2
                return value - len(update_suffix) - identity_overhead
        return None

    def _prepare_review_finding_state(self, data: dict) -> None:
        self._review_state_result = None
        self._review_state_blocked = False
        self._review_state_block_reason = None
        self._review_finding_previous_state = None
        self._review_state_preserved = False
        self._review_fully_reviewed_files = []
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
        coverage = getattr(self, "coverage", None) or CoverageLedger()
        fully_reviewed = [path for path, file_coverage in coverage.files.items()
                          if file_coverage.status == "reviewed"]
        # _prepare_pr_review needs this to render the carried-findings section from the same
        # state result, without recomputing it from self.coverage a second time.
        self._review_fully_reviewed_files = fully_reviewed
        if self._review_state_blocked:
            if self._review_state_block_reason == _STATE_BLOCK_INVALID_MARKER:
                self._review_state_result = reconcile_review_findings(
                    None,
                    current_findings,
                    allow_resolution=False,
                    excluded_files=self.remaining_files_list,
                    fully_reviewed_files=fully_reviewed,
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
        allow_resolution = (
            bool(self.prediction)
            and not bool(getattr(self.incremental, "is_incremental", False))
            # A merged result with failed chunks is still partial, even when chunking left
            # no additional token-budget files to report. A finding the consensus vote discarded
            # is partial in exactly the same way: it is absent from this review without having
            # been fixed, and resolving it here would mark a live bug as done.
            and not bool(self.review_failed_chunk_count)
            and not bool(self.review_vote_dropped_count)
            and not bool(self.remaining_files_list)
            and parsed.valid
            and current_findings is not None
            # a dropped finding is not an absent one, so this run cannot resolve anything
            and not dropped_findings
            and len(current_findings) < max_findings
        )
        result = reconcile_review_findings(
            parsed.state,
            current_findings,
            allow_resolution=allow_resolution,
            excluded_files=self.remaining_files_list,
            fully_reviewed_files=fully_reviewed,
            head_sha=self._review_head_sha(),
            run_id=self._review_run_id(),
        )
        if parsed.state is not None or result.changed:
            self._review_state_result = result

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review

    async def _prepare_prediction(self, model: str) -> None:
        raw_prompt_vars = getattr(self, "_raw_prompt_vars", getattr(self, "vars", None))
        if raw_prompt_vars is not None:
            self.vars, self.token_handler = fit_related_tickets_to_prompt_budget(
                self.git_provider.pr,
                raw_prompt_vars,
                get_settings().pr_review_prompt.system,
                get_settings().pr_review_prompt.user,
                model,
            )
        output = get_pr_diff(self.git_provider,
                             self.token_handler,
                             model,
                             add_line_numbers_to_hunks=True,
                             disable_extra_lines=False,
                             return_remaining_files=True,)
        if isinstance(output, tuple):
            self.patches_diff, self.remaining_files_list = output
        else:
            self.patches_diff = output
            self.remaining_files_list = []
        # The single-call ledger. _prepare_chunked_prediction below replaces this with a more
        # granular one (clipped/chunk_failed per file) when chunking actually runs.
        self.coverage = self._build_coverage_ledger(self.remaining_files_list)

        # retry_with_fallback_models calls this once per model, so clear the previous attempt's
        # merged verdict; otherwise a chunked run that failed on model A would be read back as
        # model B's result.
        self.prediction_data = None
        self.review_chunk_count = 1  # the single-call default; the chunked flow rebinds it
        self.review_failed_chunk_count = 0
        self.review_vote_dropped_count = 0
        self._ship_scope_summary_paths = []
        self._ship_scope_ignore_footer = ""
        # One cap for the whole run, so nested fan-out (chunks x samples) cannot burst past it.
        # Rebuilt per model attempt: a semaphore is not reusable across event loops.
        self._call_semaphore = self._build_call_semaphore()

        # a non-empty remaining_files_list means the token budget truncated the diff
        if self.remaining_files_list and get_settings().pr_reviewer.get("enable_large_pr_chunking", False):
            if await self._prepare_chunked_prediction(model):
                return

        if self.patches_diff:
            get_logger().debug("PR diff", diff=self.patches_diff)
            # Parse here rather than in _prepare_pr_review: an unparsable review must raise while
            # retry_with_fallback_models is still on the stack, or the run ends without any other
            # model being tried. load_yaml returns {} for output its repair heuristics cannot
            # rescue, and models that struggle with structured output fail that way rather than by
            # erroring, which is why the transport-level retry never covered it.
            (self.prediction, self.prediction_data,
             self.review_vote_dropped_count) = await self._get_review_data(model)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    @staticmethod
    def _build_call_semaphore() -> Optional[asyncio.Semaphore]:
        """Bound the concurrent model calls one review may have in flight, or None for unbounded.

        Chunk fan-out nests sample fan-out, so the peak is max_number_of_calls x num_samples per
        model attempt - enough to trip a per-key rate limit, whose 429 is not retried. The default
        is above the shipped defaults' peak, so it changes nothing until either knob is raised.
        """
        try:
            limit = int(get_settings().pr_reviewer.get("max_concurrent_calls", 4))
        except (TypeError, ValueError):
            limit = 4
        return asyncio.Semaphore(limit) if limit > 0 else None

    @staticmethod
    def _is_parsable_review(data: Any) -> bool:
        """Is this parsed output a review the rest of the tool can render?"""
        return isinstance(data, dict) and isinstance(data.get("review"), dict) and bool(data["review"])

    def _build_coverage_ledger(self, remaining_files: list) -> CoverageLedger:
        """A file the model saw whole is reviewed by default; the caller marks the exceptions
        (clipped, skipped for budget, or lost to a failed chunk) on top of this base ledger."""
        ledger = CoverageLedger()
        for file in self.git_provider.get_diff_files():
            # FilePatchInfo defaults num_plus_lines/num_minus_lines to -1; several providers
            # (local/plain-diff, gerrit, bitbucket, codecommit) never populate them at all.
            if file.num_plus_lines < 0 or file.num_minus_lines < 0:
                # Last resort when the provider gave us nothing usable: derive both the counts
                # and the deletion-only classification from the patch text itself, rather than a
                # raw clamp that would silently zero this file out of the ratio (hiding
                # clipping/skips/failures on these providers) or misclassify a real
                # deletion-only file as fully reviewed.
                plus_lines, minus_lines = patch_line_counts(file.patch)
            else:
                plus_lines, minus_lines = file.num_plus_lines, file.num_minus_lines
            status = "deletion_only" if plus_lines == 0 and minus_lines > 0 else "reviewed"
            # STATUS_CREDIT gives deletion_only 0.0 credit, so it must also carry 0 changed
            # lines - otherwise it drags reviewed_ratio down as if those lines went unread.
            changed_lines = 0 if status == "deletion_only" else plus_lines + minus_lines
            ledger.add(FileCoverage(file.filename, changed_lines=changed_lines, status=status))
        for filename in remaining_files:
            ledger.mark(filename, "skipped_budget")
        return ledger

    async def _prepare_chunked_prediction(self, model: str) -> bool:
        """Review a too-large diff in chunks and merge the per-chunk verdicts.

        Returns False when chunking does not apply, leaving the single-call flow in place.
        """
        globs = list(get_settings().pr_reviewer.get("low_priority_globs", DEFAULT_LOW_PRIORITY_GLOBS))
        diff_files = order_files_by_priority(self.git_provider.get_diff_files(), globs)
        plans, remaining_files_list = get_pr_multi_diffs_with_files(
            self.git_provider,
            self.token_handler,
            model,
            max_calls=get_settings().pr_reviewer.get("max_number_of_calls", 3),
            add_line_numbers=True,
            diff_files=diff_files)
        if len(plans) < 2:
            get_logger().info("Large-diff chunking produced a single chunk, reviewing the PR in one call")
            return False

        self.chunk_plans = plans
        get_logger().info(f"Number of PR chunk calls: {len(plans)}")
        get_logger().debug("PR diff chunks", artifact=[plan.diff for plan in plans])

        # Built here, before the chunk loop runs, so _review_chunk_plans can mark chunk_failed on
        # it directly as plans exhaust their recovery stages, rather than the caller reassembling
        # the same information afterwards. previous_coverage is restored below if chunking ends up
        # producing no output at all: that path falls back to the single-call flow, which must see
        # the coverage ledger _prepare_prediction built before chunking was ever attempted, not
        # this one's clipped marks for a chunking attempt that never actually reviewed anything.
        previous_coverage = self.coverage
        coverage = self._build_coverage_ledger(remaining_files_list)
        for plan in plans:
            for filename in plan.clipped:
                coverage.mark(filename, "clipped")
        summarize_low = get_settings().pr_reviewer.get("low_priority_summarize_when_over_budget", True)
        if summarize_low:
            for filename in remaining_files_list:
                if is_low_priority(filename, globs):
                    coverage.mark(filename, "low_priority_summary")
        self.coverage = coverage
        self._record_ship_scope_footer(plans, remaining_files_list, globs, summarize_low)

        fallback_models = get_settings().config.get("fallback_models", [])
        if not isinstance(fallback_models, list):
            fallback_models = [m.strip() for m in fallback_models.split(",")] if fallback_models else []
        ok = await self._review_chunk_plans(model, fallback_models)
        if ok:
            self.remaining_files_list = remaining_files_list
        else:
            self.coverage = previous_coverage
            self._ship_scope_summary_paths = []
            self._ship_scope_ignore_footer = ""
        return ok

    def _record_ship_scope_footer(
        self,
        plans: list,
        remaining_files_list: list,
        globs: list,
        summarize_low: bool,
    ) -> None:
        """Stash low-priority summary lines and an optional [ignore] proposal for the review footer."""
        summary_paths = [
            path for path in remaining_files_list
            if summarize_low and is_low_priority(path, globs)
        ]
        self._ship_scope_summary_paths = summary_paths
        reviewed_paths = {path for plan in plans for path in plan.files}
        low_in_chunks = [path for path in reviewed_paths if is_low_priority(path, globs)]
        # Propose ignore when low-priority files burned tokens in a chunk, or when they were
        # summarized under budget (so a human can skip them next time).
        if not low_in_chunks and not summary_paths:
            self._ship_scope_ignore_footer = ""
            return
        low_paths = list(dict.fromkeys([*low_in_chunks, *summary_paths]))
        tokens_by_file = {
            f.filename: self.token_handler.count_tokens(f.patch or "")
            for f in self.git_provider.get_diff_files()
            if f.filename in low_paths
        }
        self._ship_scope_ignore_footer = render_ignore_proposal(
            propose_ignore_globs(low_paths, tokens_by_file)
        )

    async def _review_chunk_plans(self, model: str, fallback_models: list) -> bool:
        """Review `self.chunk_plans`, recovering a chunk that fails every attempt on `model` in
        stages, each only as expensive as it needs to be:

        1. `CHUNK_REVIEW_ATTEMPTS` attempts on `model` (a transient failure or unparsable answer
           usually survives a second try).
        2. `pr_reviewer.chunk_split_on_failure`: split whatever is still pending in half by file
           (`split_chunk_plan`) and give the halves one attempt on `model` - a chunk that failed
           because it was too large, not because the diff itself was hard, gets a smaller bite.
        3. `pr_reviewer.chunk_fallback_model_on_failure`: one attempt on `fallback_models[0]` for
           whatever is still pending, skipped when that is the model just tried (retry_with_fallback
           models will already give it its own attempt at the top level in that case).
        4. Anything still pending after all of that has its files marked chunk_failed - but only
           when at least one other chunk in this run produced output; if every chunk failed
           outright, this returns False (or raises the first transport error) so the single-call
           flow gets a turn on the whole diff, as it did before chunking existed.

        Mutates `self.coverage` directly (marking chunk_failed on it) so a caller that pre-built
        the ledger, or a test that hands one in, sees the final state without a second pass.
        """
        original_plans = list(self.chunk_plans)
        # One "leaf" per plan still being tracked, keyed by a stable id: a plan that is split
        # keeps its parent's original index (for review_failed_chunk_count) and takes its
        # parent's place in `order`, so a partial success/failure still merges in diff order and
        # a split plan's failure still counts as one failed original chunk, not two.
        order: list[int] = list(range(len(original_plans)))
        leaf_plan: dict[int, ChunkPlan] = dict(enumerate(original_plans))
        leaf_parent: dict[int, int] = {i: i for i in range(len(original_plans))}
        next_id = len(original_plans)

        results: dict[int, tuple[str, dict, int]] = {}
        # The first failure is the one worth reporting; a retry's error is usually a repeat.
        first_chunk_error: Exception | None = None

        async def _attempt(pending_ids: list, call_model: str) -> list:
            nonlocal first_chunk_error
            positions = {lid: idx for idx, lid in enumerate(order)}
            outcomes = await asyncio.gather(
                *[self._get_review_data(call_model, leaf_plan[lid].diff, chunk_index=positions[lid],
                                        files=list(leaf_plan[lid].files)) for lid in pending_ids],
                return_exceptions=True)
            still_pending = []
            for lid, outcome in zip(pending_ids, outcomes, strict=True):
                if isinstance(outcome, Exception):
                    # An unparsable chunk is not a failed call: if every chunk ends up unparsable
                    # the single-call flow still gets its turn, whereas a transport error is the
                    # run's error and is re-raised below.
                    if first_chunk_error is None and not isinstance(outcome, UnparsableReview):
                        first_chunk_error = outcome
                    get_logger().warning(
                        f"Failed to review chunk {positions[lid] + 1}; retaining successful chunks",
                        artifact={"error": outcome})
                    still_pending.append(lid)
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
                results[lid] = outcome
            return still_pending

        # Stage 1: CHUNK_REVIEW_ATTEMPTS attempts on the model this review is running as.
        pending = list(order)
        for attempt in range(CHUNK_REVIEW_ATTEMPTS):
            if not pending:
                break
            if attempt:
                get_logger().info(f"Retrying {len(pending)} failed review chunk(s)")
            pending = await _attempt(pending, model)

        # Stage 2: split what is still pending in half by file, and give the halves one attempt
        # on the same model. A plan that could not be split (one file, or split_chunk_plan handed
        # it back unchanged) already got its fair shake in stage 1 on the exact same diff, so it
        # is left pending rather than spending another call re-asking the same question.
        if pending and get_settings().pr_reviewer.get("chunk_split_on_failure", True):
            split_ids = []
            for lid in list(pending):
                plan = leaf_plan[lid]
                if len(plan.files) <= 1:
                    continue
                halves = split_chunk_plan(plan, getattr(self, "git_provider", None),
                                          getattr(self, "token_handler", None), model)
                if halves == [plan]:
                    continue  # unsplittable in practice (or nothing regenerated); leave pending
                parent = leaf_parent[lid]
                new_ids = []
                covered = set()
                for half in halves:
                    leaf_plan[next_id] = half
                    leaf_parent[next_id] = parent
                    new_ids.append(next_id)
                    next_id += 1
                    covered.update(half.files)
                    for filename in half.clipped:
                        self.coverage.mark(filename, "clipped")
                # split_chunk_plan re-chunks each half with max_calls=1: a file that does not fit
                # even alone (or turned out delete-only in isolation) is dropped from the half
                # rather than clipped, so it needs its own mark here (stage 4 overwrites it with
                # chunk_failed if the half goes on to fail anyway). A deletion-only file is already
                # correctly marked by _build_coverage_ledger's base pass, so it is left alone here
                # instead of being downgraded to skipped_budget.
                for filename in set(plan.files) - covered:
                    if self.coverage.files[filename].status != "deletion_only":
                        self.coverage.mark(filename, "skipped_budget")
                pos = order.index(lid)
                order[pos:pos + 1] = new_ids
                pending.remove(lid)
                pending.extend(new_ids)
                split_ids.extend(new_ids)
                del leaf_plan[lid]
            if split_ids:
                get_logger().info(f"Split {len(split_ids)} chunk half(s) from a failed review chunk; retrying them")
                retried = await _attempt(split_ids, model)
                pending = [lid for lid in pending if lid not in split_ids] + retried

        # Stage 3: one attempt on the first fallback model, unless it is the model already tried
        # (retry_with_fallback_models gives that its own full attempt at the top level).
        if (pending and fallback_models
                and get_settings().pr_reviewer.get("chunk_fallback_model_on_failure", True)):
            fallback_model = fallback_models[0]
            if fallback_model != model:
                get_logger().info(f"Trying fallback model {fallback_model} for {len(pending)} failed chunk(s)")
                pending = await _attempt(pending, fallback_model)

        # keep the chunks in diff order, not completion order; halves keep their parent's position
        raw_predictions = [results[lid][0] for lid in order if lid in results]
        chunk_outputs = [results[lid][1] for lid in order if lid in results]

        if not chunk_outputs:
            if first_chunk_error is not None:
                raise first_chunk_error
            get_logger().warning("No chunk produced a parsable review, falling back to a single review call")
            return False

        # Stage 4: whatever is still pending exhausted every recovery stage - its files are not
        # covered by this review.
        failed_parents = set()
        for lid in pending:
            failed_parents.add(leaf_parent[lid])
            for filename in leaf_plan[lid].files:
                self.coverage.mark(filename, "chunk_failed")

        # the raw text is kept for logging only; the merged verdict is in self.prediction_data
        self.prediction = "\n".join(raw_predictions)
        self.prediction_data = merge_review_chunks(chunk_outputs)
        self.review_chunk_count = len(original_plans)
        # Counted by ORIGINAL plan, not by leaf: a parent whose halves both succeeded counts 0,
        # one whose split left one half still failing (or that could not be split at all) counts 1
        # - never 2, even though it may now be tracked as two leaves.
        self.review_failed_chunk_count = len(failed_parents)
        self.review_vote_dropped_count = sum(result[2] for result in results.values())
        return True

    async def _get_review_data(self, model: str, patches_diff: Optional[str] = None,
                               chunk_index: Optional[int] = None,
                               files: Optional[list] = None) -> tuple[str, dict, int]:
        """Review the diff, returning `(raw response text, parsed review dict, findings dropped)`.

        With `pr_reviewer.num_samples` at its default of 1 this is one call, parsed once. With
        more, the samples run concurrently, each is parsed, and `vote_review_samples` keeps the
        findings that recur in at least `pr_reviewer.min_votes` of them (0 = a majority) before
        reducing the rest of the fields to the samples' central tendency. Either way the caller
        gets the dict directly, so the merged verdict never has to be re-serialised and re-parsed.

        The dropped count is returned rather than accumulated on the instance: the chunked flow
        awaits several of these concurrently, so an instance attribute would only hold whichever
        chunk finished last.

        Raises when nothing parsable came back, so the fallback chain gets its turn. A single
        sample that fails or does not parse is dropped, not fatal: the vote threshold clamps to
        the samples that survived.
        """
        settings = get_settings().pr_reviewer
        try:
            num_samples = int(settings.get("num_samples", 1))
        except (TypeError, ValueError):
            num_samples = 1

        if num_samples <= 1:
            # keep the one-argument call for the whole-diff case: patches_diff defaults to it
            prediction = await (self._get_prediction(model) if patches_diff is None
                                else self._get_prediction(model, patches_diff, chunk_index=chunk_index,
                                                          files=files))
            data = self._load_review_yaml(prediction)
            set_call_findings("review", chunk_index, None, _review_findings_count(data))
            if not self._is_parsable_review(data):
                get_logger().warning(f"Unparsable review from {model}", artifact={"data": data})
                raise UnparsableReview(f"Failed to parse the review produced by {model}")
            return prediction, data, 0

        if not get_settings().config.temperature:
            get_logger().warning("pr_reviewer.num_samples > 1 with config.temperature = 0: "
                                 "the samples will be identical and the vote is a no-op")

        responses = await asyncio.gather(
            *[self._get_prediction(model, patches_diff, chunk_index=chunk_index, sample_index=i, files=files)
              for i in range(num_samples)],
            return_exceptions=True)
        parsed, raw, first_error = [], [], None
        for sample_index, response in enumerate(responses):
            if isinstance(response, BaseException):
                if not isinstance(response, Exception):
                    raise response
                first_error = first_error or response
                get_logger().warning(f"Review sample failed: {response}")
                continue
            data = self._load_review_yaml(response)
            set_call_findings("review", chunk_index, sample_index, _review_findings_count(data))
            if self._is_parsable_review(data):
                parsed.append(data)
                raw.append(response)
            else:
                get_logger().warning("Review sample could not be parsed", artifact={"response": response})
        if not parsed:
            if first_error is not None:
                raise first_error
            raise UnparsableReview(
                f"None of the {num_samples} review samples from {model} could be parsed")
        if len(parsed) < num_samples:
            get_logger().info(f"{len(parsed)} of {num_samples} review samples usable")

        try:
            min_votes = int(settings.get("min_votes", 0))
        except (TypeError, ValueError):
            min_votes = 0
        try:
            max_findings = int(settings.get("num_max_findings", 0))
        except (TypeError, ValueError):
            max_findings = 0
        consensus = vote_review_samples(parsed, min_votes, max_findings=max_findings)
        return "\n".join(raw), consensus.review, consensus.dropped

    async def _get_prediction(self, model: str, patches_diff: Optional[str] = None, *,
                              chunk_index: Optional[int] = None, sample_index: Optional[int] = None,
                              files: Optional[list] = None) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.
            patches_diff: The diff to review. Defaults to the whole prepared diff; the chunked
                flow passes one chunk per call.
            chunk_index: The diff chunk this call reviews, for run-ledger attribution. None
                when the diff was not chunked.
            sample_index: The consensus sample this call produces, for run-ledger attribution.
                None when `pr_reviewer.num_samples` is 1.
            files: The files this call's diff covers, for run-ledger attribution. None when the
                diff was not chunked (the whole-diff case does not narrow it down further).

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff if patches_diff is None else patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)

        semaphore = getattr(self, "_call_semaphore", None)
        async with (semaphore if semaphore is not None else contextlib.nullcontext()):
            response, finish_reason = await self.ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt,
                stage="review",
                chunk_index=chunk_index,
                sample_index=sample_index,
                files=files,
            )

        return response

    @staticmethod
    def _load_review_yaml(prediction: str) -> dict:
        return load_yaml(prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "risk_level:",
                                        "merge_recommendation:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:"],
                         first_key='review', last_key='security_concerns')

    async def _verify_prediction_findings(self) -> None:
        """Drop refuted findings and tag unverified ones when premise verification is enabled."""
        self.review_refuted_count = 0
        self.review_unverified_count = 0
        if not get_settings().pr_reviewer.get("enable_finding_verification", False):
            return

        # Snapshot before any mutation so a whole-pass failure leaves findings untouched.
        original_prediction_data = self.prediction_data
        try:
            data = (
                self.prediction_data
                if self.prediction_data is not None
                else self._load_review_yaml(self.prediction)
            )
            if not isinstance(data, dict) or not isinstance(data.get("review"), dict):
                return
            # Work on a deep copy so failures never partially mutate prediction_data.
            data = copy.deepcopy(data)
            issues = list(data["review"].get("key_issues_to_review") or [])
            if not issues:
                self.prediction_data = data
                return

            pr_files = [f.filename for f in self.git_provider.get_diff_files()]
            model = (
                get_settings().pr_reviewer.get("verification_model")
                or get_settings().config.get("model_weak")
                or get_settings().config.model
            )
            head_sha = self._review_head_sha()
            system_text = get_settings().pr_finding_verifier_prompt.system
            user_text = get_settings().pr_finding_verifier_prompt.user
            try:
                max_chars = int(get_settings().pr_reviewer.get("verify_max_context_chars", 200000))
            except (TypeError, ValueError):
                max_chars = 200000

            async def fetch(path: str) -> str:
                if not path:
                    return ""
                try:
                    get_pr = getattr(self.git_provider, "get_pr_file_content", None)
                    if callable(get_pr):
                        return get_pr(path, head_sha) or ""
                    get_repo = getattr(self.git_provider, "get_repo_file_content", None)
                    if callable(get_repo):
                        return get_repo(path) or ""
                except Exception:
                    return ""
                return ""

            async def call_model(system: str, user: str, files: list[str]) -> str:
                response, _ = await self.ai_handler.chat_completion(
                    model=model,
                    system=system,
                    user=user,
                    temperature=0.0,
                    stage="verify",
                    files=files,
                )
                return response

            verified = await verify_findings(
                issues,
                fetch,
                pr_files,
                call_model,
                max_findings=int(get_settings().pr_reviewer.get("verify_max_findings", 10)),
                system_prompt=system_text,
                user_template=user_text,
                max_chars=max_chars,
            )
            kept = []
            refuted = 0
            unverified = 0
            for issue, verdict in verified:
                if verdict.status == "refuted":
                    refuted += 1
                    get_logger().info(
                        "Dropping refuted finding",
                        artifact={
                            "issue": issue,
                            "evidence": verdict.evidence,
                            "reason": verdict.reason,
                        },
                    )
                    continue
                issue["verification"] = verdict.status
                if verdict.status == "unverified":
                    unverified += 1
                    header = str(issue.get("issue_header", "") or "")
                    if not header.endswith(UNVERIFIED_HEADER_SUFFIX):
                        issue["issue_header"] = f"{header}{UNVERIFIED_HEADER_SUFFIX}".strip()
                kept.append(issue)
            data["review"]["key_issues_to_review"] = kept
            self.prediction_data = data
            self.review_refuted_count = refuted
            self.review_unverified_count = unverified
        except Exception as exc:
            get_logger().warning(
                f"Finding verification pass failed ({type(exc).__name__}); keeping all findings"
            )
            self.prediction_data = original_prediction_data
            self.review_refuted_count = 0
            self.review_unverified_count = 0

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        data = self.prediction_data if self.prediction_data is not None else self._load_review_yaml(self.prediction)
        github_action_output(data, 'review')

        if not self._is_parsable_review(data):
            if self._review_finding_state_enabled():
                self._review_state_blocked = True
                self._review_state_block_reason = _STATE_BLOCK_REVIEW_DATA
                get_logger().warning("Review data is invalid; preserving persistent finding state")
            get_logger().exception("Failed to parse review data", artifact={"data": data})
            return ""

        structured_publisher = getattr(self.git_provider, "publish_structured_review", None)
        if callable(structured_publisher):
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
            structured_publisher(structured_data)

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            data['review']['key_issues_to_review'] = key_issues_to_review

        self._prepare_review_finding_state(data)
        if get_settings().config.publish_output and get_settings().pr_reviewer.get('inline_key_issues', False):
            data = self._publish_key_issues_as_inline_comments(data)

        incremental_review_markdown_text = None
        # Add incremental review section
        if self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                              f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                            incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files())

        if self.coverage is not None and self.coverage.reviewed_ratio < 0.95:
            # A partial review must say so before the findings, not after: a reader who stops at
            # the findings list would otherwise take a partial pass for a complete one.
            warning = (f"> ⚠️ **Partial review.** {self.coverage.render_footer()}. "
                      "Findings below cover only the reviewed lines; a follow-up run is needed.")
            heading, separator, rest = markdown_text.partition("\n\n")
            markdown_text = f"{heading}{separator}{warning}\n\n{rest}"

        if self.review_chunk_count > 1:
            markdown_text += (
                "\n\n<hr>\n\n"
                "ℹ️ **Chunked review:** the diff exceeded the model token budget, so it was reviewed in "
                f"{self.review_chunk_count} chunks and the per-chunk results were merged."
            )
            if self.review_failed_chunk_count:
                markdown_text += (f" {self.review_failed_chunk_count} chunk(s) failed and are not covered "
                                  "by this review.")

        if self.review_vote_dropped_count:
            # Without this the reader cannot tell a clean PR from a filtered one: a vote that
            # discards every candidate publishes the same "no major issues" as a real pass.
            markdown_text += (
                "\n\n<hr>\n\n"
                f"ℹ️ **Consensus review:** {self.review_vote_dropped_count} candidate finding(s) were "
                "not reported, because too few samples agreed on them "
                "(`pr_reviewer.min_votes`) or the review was already at "
                "`pr_reviewer.num_max_findings`."
            )

        enable_coverage_footer = get_settings().pr_reviewer.enable_review_coverage_footer
        if self.remaining_files_list and enable_coverage_footer:
            displayed_files = self.remaining_files_list[:MAX_REVIEW_COVERAGE_FILES]
            markdown_text += (
                "\n\n<hr>\n\n"
                "⚠️ **Review coverage:** The following files were not included in this review "
                "because of the token budget:\n"
                + "\n".join(f"- `{file}`" for file in displayed_files)
            )
            remaining_count = len(self.remaining_files_list) - len(displayed_files)
            if remaining_count:
                markdown_text += f"\n... and {remaining_count} more"
            if self.coverage is not None:
                markdown_text += f"\n\n{self.coverage.render_footer()}"
        elif enable_coverage_footer and self.coverage is not None and self.coverage.not_fully_reviewed():
            # A PR whose only gaps are clipped/failed chunks (nothing skipped for budget) would
            # otherwise show no coverage signal at all, since the block above never fires.
            markdown_text += f"\n\n<hr>\n\n{self.coverage.render_footer()}"

        summary_paths = getattr(self, "_ship_scope_summary_paths", None) or []
        if summary_paths:
            markdown_text += "\n" + "\n".join(
                f"- `{path}` (mockup, not reviewed)" for path in summary_paths
            )
        ignore_footer = getattr(self, "_ship_scope_ignore_footer", "") or ""
        if ignore_footer:
            markdown_text += f"\n\n{ignore_footer}"

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Output the agent run details (model, tokens, time cost) if enabled
        if get_settings().get('config', {}).get('output_run_details', False):
            markdown_text += show_run_details(self.git_provider.is_supported("gfm_markdown"))

        if self._review_state_result is not None:
            state_result = self._review_state_result
            fully_reviewed = getattr(self, "_review_fully_reviewed_files", [])
            current_ids = set(state_result.current_ids)
            carried_section = render_carried_section(state_result.state, current_ids, fully_reviewed)
            try:
                markdown_text = append_review_state(
                    markdown_text or "",
                    state_result.state,
                    max_chars=self._review_comment_max_chars(),
                    carried_section=carried_section,
                )
            except ValueError as error:
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
                        previous_carried_section = render_carried_section(
                            previous_state, current_ids, fully_reviewed
                        )
                        markdown_text = append_review_state(
                            markdown_text or "",
                            previous_state,
                            max_chars=self._review_comment_max_chars(),
                            carried_section=previous_carried_section,
                        )
                    except ValueError as previous_error:
                        get_logger().warning(
                            f"Previous persistent review state also did not fit the provider "
                            f"comment limit; leaving the existing state untouched: {previous_error}"
                        )
                    else:
                        self._review_state_preserved = True

        # Emit the review to optional external sinks (stdout/file/webhook/slack); no-op unless enabled.
        # publish_output gates it so a dry run makes no external calls. The "no major issues"
        # suppression deliberately does not: that only silences the PR comment.
        if get_settings().config.publish_output:
            push_outputs("review", payload=data.get('review', {}), markdown=markdown_text)

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        return markdown_text

    def _build_key_issue_comment(self, issue, diff_files: dict) -> Optional[dict]:
        if not isinstance(issue, dict):
            return None
        relevant_file = (issue.get("relevant_file") or "").strip()
        issue_content = _SUGGESTION_FENCE_RE.sub("```text", (issue.get("issue_content") or "").strip())
        issue_header = (issue.get("issue_header") or "").strip()
        if issue_header.lower() == "possible bug":
            issue_header = "Possible Issue"
        try:
            start_line = int(str(issue.get("start_line", 0)).strip())
            end_line = int(str(issue.get("end_line", 0)).strip())
        except ValueError:
            start_line, end_line = 0, 0

        if not relevant_file or not issue_content or start_line < 1 or end_line < start_line:
            get_logger().warning("Review finding has no usable location, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        file = diff_files.get(relevant_file) or diff_files.get(relevant_file.lstrip("/"))
        if file is None:
            get_logger().warning("Review finding points at a file that is not in the diff, "
                                 "keeping it in the summary", artifact={"relevant_file": relevant_file})
            return None
        if not file.head_file or end_line > len(file.head_file.splitlines()):
            get_logger().warning("Review finding points past the end of the file, keeping it in the summary",
                                 artifact={"relevant_file": relevant_file, "start_line": start_line,
                                           "end_line": end_line})
            return None

        relevant_file = file.filename.strip()
        body = f"**{issue_header}**\n\n{issue_content}" if issue_header else issue_content
        return {"body": body,
                "relevant_file": relevant_file,
                "relevant_lines_start": start_line,
                "relevant_lines_end": end_line,
                "fallback_to_pr_comment": False}

    def _can_verify_inline_key_issue_publication(self) -> bool:
        return can_verify_inline_comment_publication(self.git_provider)

    def _published_inline_key_issue_fingerprints(self, store: InlineCommentStore,
                                                 fingerprints: set[str]) -> set[str]:
        try:
            for body in self.git_provider.get_recent_inline_comment_bodies():
                store.add_body(body)
        except Exception as e:
            get_logger().warning(
                f"Inline key-issue publishing cannot verify new Azure DevOps threads, error: {e}; "
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
            get_logger().warning("Inline key-issue publishing cannot verify existing Azure DevOps threads; "
                                 "keeping findings in the review summary")
            return data
        remaining_issues = []
        candidate_comments = {}
        candidate_issues = {}
        candidate_fingerprints = {}
        published = 0
        for issue in issues:
            try:
                comment = self._build_key_issue_comment(issue, diff_files)
                if comment is None:
                    remaining_issues.append(issue)
                    continue
                fingerprint = key_issue_fingerprint(comment["relevant_file"], comment["body"])
                if store.seen(fingerprint):
                    published += 1
                    continue
                location_fingerprint = key_issue_location_fingerprint(
                    fingerprint, comment["relevant_lines_start"], comment["relevant_lines_end"])
                if location_fingerprint in candidate_comments:
                    candidate_issues[location_fingerprint].append(issue)
                    continue
                comment["body"] = key_issue_body_with_markers(
                    comment["body"], fingerprint, location_fingerprint,
                    getattr(self.git_provider, "max_comment_chars", None))
                candidate_comments[location_fingerprint] = comment
                candidate_issues[location_fingerprint] = [issue]
                candidate_fingerprints[location_fingerprint] = fingerprint
            except Exception as e:
                get_logger().warning(f"Failed to prepare a review finding for inline publication, error: {e}",
                                     artifact={"issue": issue})
                remaining_issues.append(issue)

        if candidate_comments:
            try:
                self.git_provider.publish_code_suggestions(list(candidate_comments.values()))
            except Exception as e:
                locations = [{"relevant_file": comment["relevant_file"],
                              "start_line": comment["relevant_lines_start"],
                              "end_line": comment["relevant_lines_end"]}
                             for comment in candidate_comments.values()]
                get_logger().warning(
                    f"Failed to publish review findings as Azure DevOps threads, error: {e}",
                    artifact={"locations": locations})
            verified_locations = self._published_inline_key_issue_fingerprints(store, set(candidate_comments))
            for location_fingerprint, comment in candidate_comments.items():
                issues_for_location = candidate_issues[location_fingerprint]
                if location_fingerprint in verified_locations:
                    store.add(candidate_fingerprints[location_fingerprint])
                    store.add(location_fingerprint)
                    published += len(issues_for_location)
                    continue
                get_logger().warning("Failed to publish a review finding as an Azure DevOps inline comment, "
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

    def _get_user_answers(self) -> Tuple[str, str]:
        """
        Retrieves the question and answer strings from the discussion messages related to a pull request.

        Returns:
            A tuple containing the question and answer strings.
        """
        question_str = ""
        answer_str = ""

        if self.is_answer:
            discussion_messages = self.git_provider.get_issue_comments()

            # providers return the comments oldest-first. PyGithub's PaginatedList reverses lazily,
            # so prefer it and only materialise the plain lists other providers return.
            newest_first = getattr(discussion_messages, "reversed", None)
            if newest_first is None:
                newest_first = reversed(list(discussion_messages))

            for message in newest_first:
                if "Questions to better understand the PR:" in message.body:
                    question_str = message.body
                elif '/answer' in message.body:
                    answer_str = message.body

                if answer_str and question_str:
                    break

        return question_str, answer_str

    def _get_previous_review_comment(self):
        """
        Get the previous review comment if it exists.
        """
        try:
            if hasattr(self.git_provider, "get_previous_review"):
                return self.git_provider.get_previous_review(
                    full=not self.incremental.is_incremental,
                    incremental=self.incremental.is_incremental,
                )
        except Exception as e:
            get_logger().exception(f"Failed to get previous review comment, error: {e}")

    def _remove_previous_review_comment(self, comment):
        """
        Remove the previous review comment if it exists.
        """
        try:
            if comment:
                self.git_provider.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove previous review comment, error: {e}")

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        # checking if running is auto mode but there are no new commits
        if self.is_auto and not self.incremental.first_new_commit_sha:
            get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new commits")
            return False

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
        recent_commits_threshold = datetime.datetime.now() - datetime.timedelta(
            minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
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
        if not get_settings().config.publish_output:
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
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review']['estimated_effort_to_review_[1-5]']
                    estimated_effort_number = None
                    if isinstance(estimated_effort, str):
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
                if get_settings().pr_reviewer.enable_review_labels_security and get_settings().pr_reviewer.require_security_review:
                    security_concerns = data['review']['security_concerns']  # yes, because ...
                    security_concerns_bool = 'yes' in security_concerns.lower() or 'true' in security_concerns.lower()
                    if security_concerns_bool:
                        review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels:\n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if
                                               not label.lower().startswith('review effort') and not label.lower().startswith(
                                                   'possible security concern')]
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

    def auto_approve_logic(self):
        """
        Auto-approve a pull request if it meets the conditions for auto-approval.
        """
        if get_settings().config.enable_auto_approval:
            is_auto_approved = self.git_provider.auto_approve()
            if is_auto_approved:
                get_logger().info("Auto-approved PR")
                self.git_provider.publish_comment("Auto-approved PR")
        else:
            get_logger().info("Auto-approval option is disabled")
            self.git_provider.publish_comment("Auto-approval option for PR-Agent is disabled. "
                                              "You can enable it via a [configuration file](https://github.com/Codium-ai/pr-agent/blob/main/docs/REVIEW.md#auto-approval-1)")
