import asyncio
import copy
import re
from datetime import date
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.code_fence import strip_wrapping_code_fence
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
    FallbackEligibleError,
    get_pr_diff,
    retry_with_fallback_models,
    run_diff_preparation,
)
from pr_agent.algo.run_details import init_run_details
from pr_agent.algo.run_output import append_run_details, push_outputs_async, show_relevant_configurations
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_for_command
from pr_agent.git_providers.git_provider import FileContentSnapshot, get_main_pr_language
from pr_agent.log import get_logger

CHANGELOG_LINES = 50


class PRUpdateChangelog:
    def __init__(self, pr_url: str, cli_mode=False, args=None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        changelog_path = get_settings().pr_update_changelog.changelog_path
        if (
            not isinstance(changelog_path, str)
            or not changelog_path.strip()
            or "\\" in changelog_path
            or re.match(r"^[A-Za-z]:", changelog_path)
            or any(part in ("", ".", "..") for part in changelog_path.split("/"))
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in changelog_path)
        ):
            raise ValueError(
                "pr_update_changelog.changelog_path must be a non-empty repository-relative file path "
                "with / separators and no . or .. components or control characters"
            )
        # Keep the snapshot, write, and follow-up review on the same target throughout the command.
        self.changelog_path = changelog_path
        self.git_provider = get_git_provider_for_command(pr_url)

        # Determine whether pushing the changelog to the repo is both requested and possible.
        # If a push is requested but not possible — the provider has no push support, or
        # restricted_mode disables the "push_code" capability — degrade gracefully: still
        # generate the changelog and publish it as a comment (which only needs
        # pull-requests: write) instead of skipping the command and dropping the output entirely.
        push_changelog_changes = get_settings().pr_update_changelog.push_changelog_changes
        self.push_changelog_changes = _as_bool_setting(push_changelog_changes, default=bool(push_changelog_changes))
        self.push_skipped_reason = None
        if self.push_changelog_changes:
            if not hasattr(self.git_provider, "create_or_update_pr_file"):
                self.push_skipped_reason = "not supported for this git provider"
            elif not hasattr(self.git_provider, "get_pr_file_content_snapshot"):
                self.push_skipped_reason = "guarded file writes are not supported for this git provider"
            elif not self.git_provider.is_supported("push_code"):
                self.push_skipped_reason = (
                    "restricted by configuration (restricted_mode)"
                    if get_settings().config.restricted_mode else "not supported for this pull request"
                )
        # Push only when it was requested AND is possible; otherwise fall back to a comment.
        self.commit_changelog = self.push_changelog_changes and self.push_skipped_reason is None

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.changelog_read_error = None
        self._get_changelog_file()  # self.changelog_file_str

        try:
            self.ai_handler = ai_handler()
            if self.main_language:
                self.ai_handler.main_pr_language = self.main_language

            self.patches_diff = None
            self.prediction = None
            self.cli_mode = cli_mode
            self.vars = {
                "title": self.git_provider.pr.title,
                "branch": self.git_provider.get_pr_branch(),
                "description": self.git_provider.get_pr_description(),
                "language": self.main_language,
                "diff": "",  # empty diff for initial calculation
                "pr_link": "",
                "changelog_path": self.changelog_path,
                "changelog_file_str": self.changelog_file_str,
                "today": date.today(),
                "extra_instructions": get_settings().pr_update_changelog.extra_instructions,
                "commit_messages_str": self.git_provider.get_commit_messages(),
            }
        except Exception as setup_error:
            changelog_read_error = self.changelog_read_error
            if changelog_read_error is None:
                raise
            get_logger().exception(
                f"Failed to initialize changelog generation after a read error: {setup_error}"
            )
            self._publish_changelog_read_error_fallback()
            raise changelog_read_error  # noqa: B904

    async def run(self):
        # A fresh collector so the run-details section reports only this command's model.
        init_run_details()
        get_logger().info('Updating the changelog...')
        changelog_read_error = getattr(self, "changelog_read_error", None)

        # If a push was requested but isn't possible (unsupported provider or restricted_mode),
        # the changelog is still generated and published as a comment below (commit_changelog is
        # already False in that case), so the output is not dropped.
        if self.push_skipped_reason:
            get_logger().info(
                f"Pushing changelog changes is {self.push_skipped_reason}; "
                f"publishing the changelog as a comment instead"
            )

        temporary_comment_published = False
        if get_settings().config.publish_output:
            try:
                self.git_provider.publish_comment("Preparing changelog updates...", is_temporary=True)
                temporary_comment_published = True
            except Exception as progress_error:
                if changelog_read_error is None:
                    raise
                get_logger().exception(
                    f"Failed to publish changelog progress after a read error: {progress_error}"
                )

        try:
            try:
                await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.WEAK)
            except Exception as generation_error:
                if changelog_read_error is None:
                    raise
                get_logger().exception(
                    f"Failed to generate changelog fallback after a read error: {generation_error}"
                )
                self._publish_changelog_read_error_fallback()
                raise changelog_read_error  # noqa: B904

            if self.prediction is None:
                if changelog_read_error is not None:
                    self._publish_changelog_read_error_fallback()
                    raise changelog_read_error
                return

            new_file_content, answer = self._prepare_changelog_update()

            if changelog_read_error is not None:
                self._publish_changelog_read_error_fallback(answer)
                raise changelog_read_error

            # Output the relevant configurations if enabled
            if get_settings().get('config', {}).get('output_relevant_configurations', False):
                answer += show_relevant_configurations(relevant_section='pr_update_changelog')

            get_logger().debug("PR output", artifact=answer)

            if get_settings().config.publish_output:
                entry = strip_wrapping_code_fence(self.prediction.strip()).strip()
                await push_outputs_async(
                    "update_changelog",
                    payload={"path": self.changelog_path, "entry": entry},
                    markdown=f"**Changelog draft:**\n\n{entry}",
                    git_provider=self.git_provider,
                )
                if self.commit_changelog:
                    await self._push_changelog_update(new_file_content, answer)
                else:
                    changelog_comment = f"**Changelog updates:** 🔄\n\n{answer}"
                    if self.push_skipped_reason:
                        changelog_comment += (
                            f"\n\n> ℹ️ These changes were not pushed to the repository "
                            f"({self.push_skipped_reason})."
                        )
                    self.git_provider.publish_comment(append_run_details(changelog_comment, self.git_provider))
        finally:
            if temporary_comment_published:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception as cleanup_error:
                    get_logger().warning(
                        f"Failed to remove the temporary changelog comment: {cleanup_error}"
                    )

    def _publish_changelog_read_error_fallback(self, answer: str = ""):
        if answer:
            changelog_comment = f"**Changelog updates:** 🔄\n\n{answer}"
        else:
            changelog_comment = "**Changelog update could not be generated.**"
        changelog_comment += (
            "\n\n> ⚠️ These changes were not pushed because the existing "
            f"{self.changelog_path} could not be read safely."
        )
        if answer:
            # Without an answer no model call succeeded, and this can run from __init__,
            # before run() installs a fresh collector, so never report a model here.
            changelog_comment = append_run_details(changelog_comment, self.git_provider)
        try:
            self.git_provider.publish_comment(changelog_comment)
        except Exception as fallback_error:
            get_logger().exception(
                f"Failed to publish changelog fallback after a read error: {fallback_error}"
            )

    def _publish_changelog_write_error_fallback(self, answer: str):
        changelog_comment = f"**Changelog updates:** 🔄\n\n{answer}"
        changelog_comment += (
            "\n\n> ⚠️ The repository update could not be confirmed. "
            "The generated changelog is preserved here for recovery."
        )
        changelog_comment = append_run_details(changelog_comment, self.git_provider)
        try:
            self.git_provider.publish_comment(changelog_comment)
        except Exception as fallback_error:
            get_logger().exception(
                f"Failed to publish changelog fallback after a write error: {fallback_error}"
            )

    async def _prepare_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        if get_settings().pr_update_changelog.add_pr_link:
            variables["pr_link"] = self.git_provider.get_pr_url()
        output_token_reserve = getattr(self.ai_handler, "get_output_token_reserve", None)
        budget = AttemptTokenBudget.for_prompt_attempt(
            model,
            getattr(self.git_provider, "pr", None),
            variables,
            get_settings().pr_update_changelog_prompt.system,
            get_settings().pr_update_changelog_prompt.user,
            ai_handler=self.ai_handler,
            output_token_reserve=output_token_reserve,
        )
        budget.require_input_capacity(
            OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
            preserve_minimum=True,
        )
        output = await run_diff_preparation(
            get_pr_diff,
            self.git_provider,
            budget.token_handler,
            model,
            return_remaining_files=True,
            output_token_reserve=output_token_reserve,
        )
        patches_diff, remaining_files_list = output if isinstance(output, tuple) else (output, [])
        if not patches_diff and not remaining_files_list:
            # Every changed file was filtered out (lockfile, asset, ignore rule), so there is
            # nothing to add to the changelog. Skip the model chain instead of failing the command.
            get_logger().info("PR has no analyzable files, skipping update_changelog")
            return
        if not patches_diff:
            raise FallbackEligibleError(f"No PR diff fits the /update_changelog request for {model}")

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
                f"The complete packed changelog diff does not fit the token limit for {model}"
            )
        self.patches_diff = fitted.optional_text
        self._attempt_system_prompt = fitted.system_prompt
        self._attempt_user_prompt = fitted.user_prompt
        get_logger().debug("PR diff", artifact=self.patches_diff)
        self.prediction = await self._get_prediction(model)

    async def _get_prediction(self, model: str):
        system_prompt = self._attempt_system_prompt
        user_prompt = self._attempt_user_prompt
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, system=system_prompt, user=user_prompt, temperature=get_settings().config.temperature)
        if finish_reason == "length":
            raise FallbackEligibleError(f"Changelog response was truncated by {model}")

        # post-process the response
        response = response.strip()
        # Check before stripping, which can turn four backticks into a stray single backtick.
        fence_pattern = r"(?m)^[^\S\n]*(?:`{3,}[^`\n]*|~{3,}[^~\n]*)$"
        content = re.sub(fence_pattern, "", response)
        if content.strip():
            response = strip_wrapping_code_fence(response)
            content = re.sub(fence_pattern, "", response)
        if not content.strip():
            raise FallbackEligibleError(f"Empty changelog response from {model}")
        return response

    def _prepare_changelog_update(self) -> tuple[str, str]:
        answer = strip_wrapping_code_fence(self.prediction.strip()).strip()
        if hasattr(self, "changelog_file"):
            existing_content = self.changelog_file
        else:
            existing_content = ""

        if existing_content:
            new_file_content = answer + "\n\n" + self.changelog_file
        else:
            new_file_content = answer

        if not self.commit_changelog:
            answer += (
                f"\n\n\n>to commit the new content to the {self.changelog_path} file, enable "
                "`pr_update_changelog.push_changelog_changes=true` in `.pr_agent.toml`.\n"
            )

        return new_file_content, answer

    async def _push_changelog_update(self, new_file_content, answer):
        if not self.git_provider.is_supported("push_code"):
            # Its only caller already gates on self.commit_changelog, which is False
            # whenever this capability is missing; kept local so the guard holds even
            # if a future caller reaches this method some other way.
            return
        skip_ci_on_push = get_settings().pr_update_changelog.get("skip_ci_on_push", True)
        if _as_bool_setting(skip_ci_on_push, default=bool(skip_ci_on_push)):
            commit_message = f"[skip ci] Update {self.changelog_path}"
        else:
            commit_message = f"Update {self.changelog_path}"
        try:
            if self.changelog_snapshot is None:
                raise ValueError("Changelog write requires the command-start file snapshot")
            written_commit = self.git_provider.create_or_update_pr_file(
                file_path=self.changelog_path,
                branch=self.git_provider.get_pr_branch(),
                contents=new_file_content,
                message=commit_message,
                expected_snapshot=self.changelog_snapshot,
            )
        except Exception:
            self._publish_changelog_write_error_fallback(answer)
            raise

        if not self.git_provider.supports_changelog_update_review():
            return

        try:
            await asyncio.sleep(5)  # wait for the file to be updated
        except asyncio.CancelledError:
            # Preserve the user-visible fallback after a successful write while keeping
            # cancellation authoritative.
            try:
                if self.git_provider.supports_changelog_update_review():
                    self.git_provider.publish_comment(
                        append_run_details(f"**Changelog updates: 🔄**\n\n{answer}", self.git_provider)
                    )
            except Exception as feedback_error:
                get_logger().exception(
                    f"Failed to publish changelog fallback during cancellation: {feedback_error}"
                )
            raise
        try:
            if self.git_provider.supports_changelog_update_review():
                if written_commit is None:
                    raise ValueError("The changelog write did not return a commit for review")
                d = dict(
                    body=f"{self.changelog_path} update",
                    path=self.changelog_path,
                    line=max(2, len(answer.splitlines())),
                    start_line=1,
                )
                self.git_provider.pr.create_review(commit=written_commit, event="COMMENT", comments=[d])
        except Exception:
            # we can't create a review for some reason, let's just publish a comment
            try:
                fallback = self.git_provider.publish_comment(
                    append_run_details(f"**Changelog updates: 🔄**\n\n{answer}", self.git_provider)
                )
                if self.git_provider.supports_comment_publish_confirmation() and fallback is None:
                    raise RuntimeError("The changelog fallback comment was not confirmed")
            except Exception as feedback_error:
                if written_commit is None:
                    raise ValueError("The changelog write did not return a commit for review") from feedback_error
                get_logger().opt(exception=feedback_error).warning(
                    f"{self.changelog_path} was updated, but its automatic feedback could not be confirmed: "
                    f"{feedback_error}"
                )

    def _get_default_changelog(self):
        example_changelog = \
"""
Example:
## <current_date>

### Added
...
### Changed
...
### Fixed
...
"""
        return example_changelog

    def _get_changelog_file(self):
        strict_read = self.commit_changelog and get_settings().config.publish_output
        self.changelog_snapshot = None
        try:
            if strict_read:
                snapshot = self.git_provider.get_pr_file_content_snapshot(
                    self.changelog_path, self.git_provider.get_pr_branch()
                )
                if not isinstance(snapshot, FileContentSnapshot) or not isinstance(snapshot.contents, str):
                    raise TypeError("Changelog snapshot must contain text")
                self.changelog_snapshot = snapshot
                self.changelog_file = snapshot.contents
            else:
                self.changelog_file = self.git_provider.get_pr_file_content(
                    self.changelog_path, self.git_provider.get_pr_branch()
                )

            if not strict_read and isinstance(self.changelog_file, bytes):
                self.changelog_file = self.changelog_file.decode('utf-8')

            changelog_file_lines = self.changelog_file.splitlines()
            changelog_file_lines = changelog_file_lines[:CHANGELOG_LINES]
            self.changelog_file_str = "\n".join(changelog_file_lines)
        except Exception as e:
            get_logger().warning(f"Error getting changelog file: {e}")
            if strict_read:
                self.changelog_read_error = e
            self.changelog_file_str = ""
            self.changelog_file = ""
            return

        if not self.changelog_file_str:
            self.changelog_file_str = self._get_default_changelog()
