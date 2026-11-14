import copy
import re
from functools import partial
from urllib.parse import urlparse

from starlette_context import context

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.artifacts import get_artifact_context
from pr_agent.algo.comment_identity import format_pr_questions_header
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
    FallbackEligibleError,
    get_pr_diff,
    retry_with_fallback_models,
    run_diff_preparation,
)
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.review_json_output import review_json_output_path, write_review_json_output
from pr_agent.algo.run_details import get_run_details, init_run_details, record_command_failure
from pr_agent.algo.run_output import append_run_details, push_outputs_async
from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.utils import ModelType, decode_user_text_args
from pr_agent.commands.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets,
    fit_related_tickets_to_prompt_budget,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_for_command
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage

NO_ANALYZABLE_FILES_NOTE = (
    "Every file changed in this PR is excluded from analysis (for example lockfiles, generated "
    "files, or files matched by the ignore settings), so there is no diff to answer this question from."
)
REPO_CONTEXT_OMITTED_NOTE = (
    "Repository instruction files were omitted because they did not fit the model context. "
    "This answer does not use them."
)
NO_ANALYZABLE_FILE_NOTE = (
    "The requested file has no analyzable diff in this PR. It may be excluded by the ignore "
    "settings or file-type filters, or have no available patch."
)
FILE_QUESTION_BUDGET_NOTE = (
    "This file question could not be prepared within the configured input budget. "
    "Try shortening the question or other prompt context, asking on specific lines with a line comment, "
    "or increasing the configured input limit."
)


def _sanitize_slash_commands(text: str) -> str:
    # make sure no line starts with "/", so the published comment cannot trigger quick actions
    sanitized = text.replace("\n/", "\n /").replace("\r/", "\r /")
    if sanitized.startswith("/"):
        sanitized = " " + sanitized
    return sanitized


def _get_question_diff(git_provider, *args, **kwargs):
    file_name = context.get("file_question_path") if context.exists() else None
    if not file_name:
        return get_pr_diff(git_provider, *args, **kwargs)
    diff_files = git_provider.get_diff_files()
    patch = next((file.patch for file in diff_files if file.filename == file_name), "")
    return f"## File: '{file_name}'\n\n{patch}" if patch and patch.strip() else ""


class PRQuestions:
    def _write_json_output(self, answer: str) -> None:
        """Export a question's answer when the CLI requested a JSON artifact."""
        if not review_json_output_path.get():
            return
        details = get_run_details()
        usage = {}
        if details is not None and details.has_token_usage:
            usage = {
                "prompt_tokens": details.prompt_tokens,
                "completion_tokens": details.completion_tokens,
                "total_tokens": details.total_tokens,
            }
        try:
            write_review_json_output({"question": self.question_str, "answer": answer, "usage": usage})
        except (OSError, TypeError, ValueError):
            # Keep the normal answer available; the CLI still reports the missing artifact.
            record_command_failure()

    def __init__(self, pr_url: str, args=None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        question_str = self.parse_args(args)
        self.pr_url = pr_url
        self.git_provider = get_git_provider_for_command(pr_url)
        self.main_pr_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_pr_language

        self.question_str = question_str
        settings = get_settings()
        skills_context = (
            get_skills_context()
            if settings.skills.get("enabled", False)
            else ""
        )
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.git_provider.get_pr_description(),
            "language": self.main_pr_language,
            "diff": "",  # empty diff for initial calculation
            "questions": self.question_str,
            "conversation_history": self._load_conversation_history(),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "extra_instructions": settings.pr_questions.extra_instructions,
            "skills_context": skills_context,
            "artifact_context": get_artifact_context("pr_questions"),
            "repo_context": "",
            "related_tickets": [],
            "related_tickets_omitted": 0,
        }
        self.patches_diff = None
        self.prediction = None
        self.no_analyzable_files = False
        self._repo_context: str | None = None
        self._ticket_context_loaded = False

    def parse_args(self, args):
        return decode_user_text_args(args)

    async def run(self):
        # A fresh collector so the run-details section reports only this command's model.
        init_run_details()
        get_logger().info(f'Answering a PR question about the PR {self.pr_url} ')
        relevant_configs = {'pr_questions': dict(get_settings().pr_questions),
                            'config': dict(get_settings().config)}
        get_logger().debug("Relevant configs", artifacts=relevant_configs)
        temporary_comment_published = False
        if get_settings().config.publish_output:
            self.git_provider.publish_comment("Preparing answer...", is_temporary=True)
            temporary_comment_published = True

        try:
            # identify image
            img_path = self.identify_image_in_comment()
            if img_path:
                get_logger().debug("Image path identified", artifact=img_path)

            try:
                await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.WEAK)
            except Exception as error:
                file_name = context.get("file_question_path") if context.exists() else None
                # A fitted diff survives later retries, so transport failures remain errors.
                if not (file_name and self.patches_diff is None
                        and isinstance(error.__cause__, FallbackEligibleError)):
                    raise
                if get_settings().config.publish_output:
                    await self._publish_answer_with_sinks(
                        self._format_answer(FILE_QUESTION_BUDGET_NOTE), FILE_QUESTION_BUDGET_NOTE,
                    )
                else:
                    get_settings().set("data.answer", FILE_QUESTION_BUDGET_NOTE)
                return ""

            if getattr(self, "no_analyzable_files", False):
                # No model was called, so there are no run details to report.
                answer = self._no_analyzable_files_note()
                note = self._prepare_no_analyzable_files_answer()
                self._write_json_output(answer)
                if get_settings().config.publish_output:
                    await self._publish_answer_with_sinks(note, answer)
                else:
                    get_settings().set("data.answer", answer)
                return ""

            pr_comment = self._prepare_pr_answer()
            get_logger().debug("PR output", artifact=pr_comment)
            self._write_json_output((self.prediction or "").strip())

            if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_questions.enable_help_text:
                pr_comment += "<hr>\n\n<details> <summary><strong>💡 Command usage guide:</strong></summary><hr> \n\n"
                pr_comment += HelpMessage.get_ask_usage_guide()
                pr_comment += "\n</details>\n"

            if get_settings().config.publish_output:
                await self._publish_answer_with_sinks(
                    append_run_details(pr_comment, self.git_provider), (self.prediction or "").strip()
                )
            else:
                get_settings().set("data.answer", (self.prediction or "").strip())
        finally:
            if temporary_comment_published:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception as cleanup_error:
                    get_logger().warning(
                        f"Failed to remove the temporary question comment: {cleanup_error}"
                    )
        return ""

    async def _publish_answer_with_sinks(self, comment: str, answer: str):
        await push_outputs_async(
            "ask", payload={"question": self.question_str, "answer": answer},
            markdown=self._format_answer(_sanitize_slash_commands(answer)), git_provider=self.git_provider,
        )
        return self._publish_answer(comment)

    def _publish_answer(self, answer: str):
        file_comment_id = context.get("file_question_comment_id") if context.exists() else None
        if file_comment_id:
            return self.git_provider.reply_to_comment_from_comment_id(file_comment_id, answer)
        comment_id = get_settings().get("comment_id", "")
        if comment_id and self.git_provider.supports_threaded_pr_questions():
            return self.git_provider.reply_to_comment_from_comment_id(comment_id, answer)
        return self.git_provider.publish_comment(answer)

    def _load_conversation_history(self) -> str:
        file_question = context.get("file_question_path") if context.exists() else None
        supports_history = (self.git_provider.supports_line_question_history() if file_question
                            else self.git_provider.supports_threaded_pr_questions())
        if not supports_history or not get_settings().pr_questions.use_conversation_history:
            return ""
        comment_id = (context["file_question_comment_id"] if file_question
                      else get_settings().get("comment_id", ""))
        if not comment_id:
            return ""
        origin_comment_id = (context["file_question_origin_comment_id"] if file_question
                             else get_settings().get("origin_comment_id", comment_id))
        try:
            comments = self.git_provider.get_review_thread_comments(comment_id)
        except Exception as e:
            get_logger().warning(f"Failed to load question thread: {e}")
            return ""
        history = []
        for comment in comments:
            body = getattr(comment, "body", "")
            if (not isinstance(body, str) or not body.strip()
                    or getattr(comment, "id", None) == origin_comment_id):
                continue
            user = getattr(comment, "user", None)
            author = getattr(user, "login", "Unknown")
            history.append(f"{len(history) + 1}. {author}: {body}")
        return "\n".join(history)

    def identify_image_in_comment(self):
        img_path = ''
        markdown_image = re.search(
            r'!\[[^\[\]\n]*\]\(\s*(https://[^\s)]+)', self.question_str, re.IGNORECASE
        )
        if markdown_image:
            img_path = markdown_image.group(1)
            self.vars['img_path'] = img_path
        elif 'https://' in self.question_str:
            # direct image link
            # pick the first http(s) URL that actually looks like an image, so that
            # surrounding prose and any earlier non-image links are not absorbed
            img_path = self._find_image_url(self.question_str)
            if img_path:
                self.vars['img_path'] = img_path
        return img_path

    @staticmethod
    def _find_image_url(text: str) -> str:
        """Return the first http(s) URL in ``text`` that points at an image, else an empty string."""
        for match in re.finditer(r'https?://[^\s\'"<>)\]]+', text):
            candidate = match.group(0)
            # strip sentence punctuation that a user typed right after the link
            candidate = candidate.rstrip('.,;:?!')
            path = urlparse(candidate).path.lower()
            if re.search(r'\.(?:png|jpe?g|gif|webp)$', path):
                return candidate
        return ''

    async def _prepare_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        repo_context_omitted = False
        # Select the same history, diff and repository instructions before spending room on tickets.
        variables["related_tickets"] = []
        variables["related_tickets_omitted"] = 0
        raw_history = variables.get("conversation_history", "")
        file_question = context.get("file_question_path") if context.exists() else None
        variables["conversation_history"] = ""
        image_path = variables.get("img_path")
        if not isinstance(image_path, str) or not image_path.strip():
            image_path = None
        output_token_reserve = getattr(self.ai_handler, "get_output_token_reserve", None)
        if not file_question:
            history_budget = AttemptTokenBudget.for_prompt_attempt(
                model,
                getattr(self.git_provider, "pr", None),
                variables,
                get_settings().pr_questions_prompt.system,
                get_settings().pr_questions_prompt.user,
                ai_handler=self.ai_handler,
                image_path=image_path,
                output_token_reserve=output_token_reserve,
            )
            fitted_history = history_budget.fit_prompt_variable(
                variables,
                "conversation_history",
                raw_history,
                ai_handler=self.ai_handler,
                default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
                preserve_minimum=True,
                additional_input_reserve=OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
                image_path=image_path,
                keep="suffix",
            )
            variables["conversation_history"] = fitted_history.optional_text

        budget = AttemptTokenBudget.for_prompt_attempt(
            model,
            getattr(self.git_provider, "pr", None),
            variables,
            get_settings().pr_questions_prompt.system,
            get_settings().pr_questions_prompt.user,
            ai_handler=self.ai_handler,
            image_path=image_path,
            output_token_reserve=output_token_reserve,
        )
        budget.require_input_capacity(
            OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD if file_question else OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
            preserve_minimum=True,
        )
        output = await run_diff_preparation(
            _get_question_diff,
            self.git_provider,
            budget.token_handler,
            model,
            return_remaining_files=True,
            output_token_reserve=output_token_reserve,
        )
        patches_diff, remaining_files_list = output if isinstance(output, tuple) else (output, [])
        if not patches_diff and not remaining_files_list:
            # The requested diff is unavailable. Reply with a note instead of failing
            # every model in the chain or answering from unrelated files.
            get_logger().info(f"Requested diff is unavailable: {self.pr_url}, answering /ask with a note")
            self.no_analyzable_files = True
            return
        if not patches_diff:
            raise FallbackEligibleError(f"No PR diff fits the /ask request for {model}")

        variables["diff"] = patches_diff
        if file_question and raw_history:
            # The required file diff is already included; fit only recent thread
            # history into the remaining space without shortening the patch.
            try:
                fitted_history = budget.fit_prompt_variable(
                    variables,
                    "conversation_history",
                    raw_history,
                    ai_handler=self.ai_handler,
                    default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
                    preserve_minimum=True,
                    image_path=image_path,
                    keep="suffix",
                )
            except FallbackEligibleError:
                pass  # Even the history truncation marker may not fit.
            else:
                variables["conversation_history"] = fitted_history.optional_text

        if getattr(self, "_repo_context", None) is None:
            self._repo_context = await run_diff_preparation(build_repo_context, self.git_provider)
        if self._repo_context:
            # Use only the room left after selecting the original history and complete diff.
            try:
                budget.fit_prompt_variable(
                    variables,
                    "repo_context",
                    self._repo_context,
                    ai_handler=self.ai_handler,
                    default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
                    preserve_minimum=True,
                    image_path=image_path,
                    require_full_text=True,
                )
            except FallbackEligibleError:
                repo_context_omitted = True
                get_logger().warning("Repository instructions do not fit; keeping the existing question context")
            else:
                variables["repo_context"] = self._repo_context

        if _as_bool_setting(get_settings().pr_questions.get("use_ticket_context", False)):
            if not self._ticket_context_loaded:
                try:
                    await extract_and_cache_pr_tickets(self.git_provider, self.vars, enabled=True, use_cache=False)
                except Exception:
                    self.vars["related_tickets"] = []
                    get_logger().warning("Failed to fetch linked tickets; continuing without ticket context")
                finally:
                    self._ticket_context_loaded = True
            if self.vars.get("related_tickets"):
                try:
                    variables, _ = fit_related_tickets_to_prompt_budget(
                        getattr(self.git_provider, "pr", None),
                        {**variables, "related_tickets": self.vars["related_tickets"]},
                        get_settings().pr_questions_prompt.system,
                        get_settings().pr_questions_prompt.user,
                        model,
                        ai_handler=self.ai_handler,
                        image_path=image_path,
                        output_token_reserve=output_token_reserve,
                        default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
                        # The diff is already included; retain the helper's required nonzero headroom.
                        additional_input_reserve=1,
                    )
                except FallbackEligibleError:
                    # Even the omission notice is optional; retain the original question context.
                    get_logger().debug("Linked tickets do not fit; keeping the existing question context")

        diff_args = dict(
            ai_handler=self.ai_handler,
            default_output_tokens=OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
            preserve_minimum=True,
            image_path=image_path,
            require_full_text=True,
        )
        fitted = budget.fit_prompt_variable(variables, "diff", patches_diff, **diff_args)
        if fitted.optional_text != patches_diff:
            raise FallbackEligibleError(
                f"The complete packed question diff does not fit the token limit for {model}"
            )
        self.patches_diff = patches_diff
        self._attempt_system_prompt = fitted.system_prompt
        self._attempt_user_prompt = fitted.user_prompt
        self._attempt_variables = variables
        get_logger().debug("PR diff", artifact=self.patches_diff)
        self.prediction = await self._get_prediction(model)
        if repo_context_omitted:
            self.prediction = f"{self.prediction.rstrip()}\n\n> {REPO_CONTEXT_OMITTED_NOTE}"

    async def _get_prediction(self, model: str):
        system_prompt = self._attempt_system_prompt
        user_prompt = self._attempt_user_prompt
        variables = self._attempt_variables
        if 'img_path' in variables:
            img_path = self.vars['img_path']
            response, finish_reason = await (self.ai_handler.chat_completion
                                             (model=model, temperature=get_settings().config.temperature,
                                              system=system_prompt, user=user_prompt, img_path=img_path))
        else:
            response, finish_reason = await self.ai_handler.chat_completion(
                model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        if finish_reason == "length":
            raise FallbackEligibleError(f"The /ask response was truncated by {model}")
        return response

    def _prepare_pr_answer(self) -> str:
        model_answer = self.prediction.strip()
        model_answer_sanitized = _sanitize_slash_commands(model_answer)
        if model_answer_sanitized != model_answer:
            get_logger().debug("Sanitized model answer",
                               artifact={"model_answer": model_answer, "sanitized_answer": model_answer_sanitized})
        return self._format_answer(model_answer_sanitized)

    def _prepare_no_analyzable_files_answer(self) -> str:
        return self._format_answer(self._no_analyzable_files_note())

    def _no_analyzable_files_note(self) -> str:
        file_name = context.get("file_question_path") if context.exists() else None
        return NO_ANALYZABLE_FILE_NOTE if file_name else NO_ANALYZABLE_FILES_NOTE

    def _format_answer(self, answer: str) -> str:
        answer_header = format_pr_questions_header(
            escape_markdown=self.git_provider.is_supported("markdown_backslash_escapes")
        )
        # Keep file replies in their original thread without repeating a potentially long question.
        file_question = context.get("file_question_path") if context.exists() else None
        question = (
            f"[View question]({context['file_question_comment_url']})\n\n"
            if file_question else f"{_sanitize_slash_commands(self.question_str)}\n\n"
        )
        answer_str = f"{answer_header}\n{question}"
        answer_str += f"### **Answer:**\n{answer}\n\n"
        return answer_str
