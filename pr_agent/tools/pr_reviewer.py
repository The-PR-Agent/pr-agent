import copy
import datetime
import re
from functools import partial
from typing import List, Optional, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.inline_comment_dedup import (
    InlineCommentStore,
    can_verify_inline_comment_publication,
    get_inline_comment_store,
    key_issue_body_with_markers,
    key_issue_fingerprint,
    key_issue_location_fingerprint,
)
from pr_agent.algo.pr_processing import add_ai_metadata_to_diff_files, get_pr_diff, retry_with_fallback_models
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (
    ModelType,
    PRReviewHeader,
    PRReviewIdentity,
    add_pr_review_identity,
    convert_to_markdown_v2,
    github_action_output,
    load_yaml,
    push_outputs,
    show_relevant_configurations,
    show_run_details,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import IncrementalPR, get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.ticket_pr_compliance_check import extract_and_cache_pr_tickets

MAX_REVIEW_COVERAGE_FILES = 50
_SUGGESTION_FENCE_RE = re.compile(r"```[ \t]*suggestion\b", re.IGNORECASE)


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

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
        self.prediction = None
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
            "is_ai_metadata":  get_settings().get("config.enable_ai_metadata", False),
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
        review_failed = False
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

            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            if not self.prediction:
                return None

            pr_review = self._prepare_pr_review()
            get_logger().debug("PR output", artifact=pr_review)

            should_publish = get_settings().config.publish_output and self._should_publish_review_no_suggestions(pr_review)
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
            if get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                self.git_provider.publish_persistent_comment(
                    pr_review,
                    initial_header=pr_review.split("\n", 1)[0],
                    update_header=True,
                    final_update_message=final_update_message,
                    identity_marker=PRReviewIdentity.REGULAR.value,
                    legacy_initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
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
            if (review_failed and get_settings().config.publish_output and
                    not get_settings().config.get("is_auto_command", False)):
                try:
                    self.git_provider.publish_comment("Failed to review PR")
                except Exception as e:
                    get_logger().exception(f"Failed to publish review failure result, error: {e}")

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review

    async def _prepare_prediction(self, model: str) -> None:
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

        if self.patches_diff:
            get_logger().debug("PR diff", diff=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    async def _get_prediction(self, model: str) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )

        return response

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        first_key = 'review'
        last_key = 'security_concerns'
        data = load_yaml(self.prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "risk_level:",
                                        "merge_recommendation:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:"],
                         first_key=first_key, last_key=last_key)
        github_action_output(data, 'review')

        if 'review' not in data:
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

        if self.remaining_files_list and get_settings().pr_reviewer.enable_review_coverage_footer:
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
