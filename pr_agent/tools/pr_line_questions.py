import argparse
import copy
from functools import partial

from jinja2 import Environment, StrictUndefined, select_autoescape
from litellm import token_counter

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extract_hunk_lines_from_patch)
from pr_agent.algo.pr_processing import (OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
                                         get_pr_diff,
                                         retry_with_fallback_models)
from pr_agent.algo.token_handler import TokenEncoder, TokenHandler
from pr_agent.algo.utils import ModelType, get_max_tokens
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage


class PR_LineQuestions:
    def __init__(self, pr_url: str, args=None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.question_str = self.parse_args(args)
        self.git_provider = get_git_provider()(pr_url)
        self.main_pr_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_pr_language

        # only GitHub can resolve threads today; elsewhere the marker would be
        # requested from the model and every answer would log a failed resolve
        self.resolve_threads = (get_settings().pr_questions.get("resolve_threads", False)
                                and self.git_provider.supports_thread_resolution())
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "diff": "",  # empty diff for initial calculation
            "question": self.question_str,
            "full_hunk": "",
            "selected_lines": "",
            "conversation_history": "",
            "resolve_threads": self.resolve_threads,
            "extra_instructions": get_settings().pr_questions.extra_instructions,
        }
        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          get_settings().pr_line_questions_prompt.system,
                                          get_settings().pr_line_questions_prompt.user)
        self.patches_diff = None
        self.prediction = None

    def parse_args(self, args):
        if args and len(args) > 0:
            question_str = " ".join(args)
        else:
            question_str = ""
        return question_str


    async def run(self):
        get_logger().info('Answering a PR lines question...')
        # if get_settings().config.publish_output:
        #     self.git_provider.publish_comment("Preparing answer...", is_temporary=True)

        # set conversation history if enabled
        # currently only supports GitHub provider
        if get_settings().pr_questions.use_conversation_history and isinstance(self.git_provider, GithubProvider):
            conversation_history = self._load_conversation_history()
            self.vars["conversation_history"] = conversation_history

        self.patch_with_lines = ""
        self.selected_lines = ""
        ask_diff = get_settings().get('ask_diff_hunk', "")
        line_start = get_settings().get('line_start', '')
        line_end = get_settings().get('line_end', '')
        side = get_settings().get('side', 'RIGHT')
        file_name = get_settings().get('file_name', '')
        comment_id = get_settings().get('comment_id', '')
        if not comment_id:
            self.resolve_threads = False
            self.vars["resolve_threads"] = False
        if ask_diff:
            self.patch_with_lines, self.selected_lines = extract_hunk_lines_from_patch(ask_diff,
                                                                                       file_name,
                                                                                       line_start=line_start,
                                                                                       line_end=line_end,
                                                                                       side=side
                                                                                       )
        else:
            diff_files = self.git_provider.get_diff_files()
            for file in diff_files:
                if file.filename == file_name:
                    self.patch_with_lines, self.selected_lines = extract_hunk_lines_from_patch(file.patch, file.filename,
                                                                                               line_start=line_start,
                                                                                               line_end=line_end,
                                                                                               side=side)
        # A matched hunk always contributes its '@@' header; a miss leaves only the file
        # title. Gating on selected_lines instead would also silence the case where GitHub
        # truncates diff_hunk from the front but keeps the original header, which leaves the
        # requested line outside the shortened body with the hunk itself still present.
        hunk_found = any(line.startswith('@@') for line in self.patch_with_lines.splitlines())
        if hunk_found:
            model_answer = await retry_with_fallback_models(self._get_prediction, model_type=ModelType.WEAK)

            should_resolve = False
            answer_stripped = model_answer.rstrip()
            if self.resolve_threads and answer_stripped.endswith("[THREAD_RESOLVED]"):
                should_resolve = True
                model_answer = answer_stripped[:-len("[THREAD_RESOLVED]")].rstrip()

            # sanitize the answer so that no line will start with "/"
            model_answer_sanitized = model_answer.strip().replace("\n/", "\n /")
            if model_answer_sanitized.startswith("/"):
                model_answer_sanitized = " " + model_answer_sanitized

            get_logger().info('Preparing answer...')
            if comment_id:
                self.git_provider.reply_to_comment_from_comment_id(comment_id, model_answer_sanitized)
                if should_resolve:
                    if self.git_provider.resolve_comment_thread(comment_id):
                        get_logger().info(f"Resolved review thread for comment {comment_id}")
                    else:
                        get_logger().warning(f"Failed to resolve review thread for comment {comment_id}")
            else:
                self.git_provider.publish_comment(model_answer_sanitized)
        else:
            get_logger().info("No hunk matched the requested range for "
                              f"'{file_name}'; skipping the /ask_line model call")
            # Without this the run is silent and the asker cannot tell us apart from a
            # broken bot, so say why nothing was answered.
            no_hunk_message = (f"Could not find the requested lines of `{file_name}` in this "
                               "pull request's diff, so there is nothing to answer about.")
            if comment_id:
                self.git_provider.reply_to_comment_from_comment_id(comment_id, no_hunk_message)
            else:
                self.git_provider.publish_comment(no_hunk_message)

        return ""

    def _load_conversation_history(self) -> str:
        """Generate conversation history from the code review thread

        Returns:
            str: The formatted conversation history
        """
        comment_id = get_settings().get('comment_id', '')
        file_path = get_settings().get('file_name', '')
        line_number = get_settings().get('line_end', '')

        # early return if any required parameter is missing
        if not all([comment_id, file_path, line_number]):
            get_logger().error("Missing required parameters for conversation history")
            return ""

        try:
            # retrieve thread comments
            thread_comments = self.git_provider.get_review_thread_comments(comment_id)

            # filter and prepare comments
            filtered_comments = []
            for comment in thread_comments:
                body = getattr(comment, 'body', '')

                # skip empty comments, current comment(will be added as a question at prompt)
                if not body or not body.strip() or comment_id == comment.id:
                    continue

                user = comment.user
                author = user.login if hasattr(user, 'login') else 'Unknown'
                filtered_comments.append((author, body))

            # transform conversation history to string using the same pattern as get_commit_messages
            if filtered_comments:
                comment_count = len(filtered_comments)
                get_logger().info(f"Loaded {comment_count} comments from the code review thread")

                # Format as numbered list, similar to get_commit_messages
                conversation_history_str = "\n".join([f"{i + 1}. {author}: {body}"
                                                   for i, (author, body) in enumerate(filtered_comments)])
                return conversation_history_str

            return ""

        except Exception as e:
            get_logger().error(f"Error processing conversation history, error: {e}")
            return ""

    async def _get_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        variables["full_hunk"] = self.patch_with_lines  # update diff
        variables["selected_lines"] = self.selected_lines
        variables["conversation_history"] = self._fit_conversation_history(variables, model)
        system_prompt, user_prompt = self._render_prompts(variables)
        if get_settings().config.verbosity_level >= 2:
            # get_logger().info(f"\nSystem prompt:\n{system_prompt}")
            # get_logger().info(f"\nUser prompt:\n{user_prompt}")
            print(f"\nSystem prompt:\n{system_prompt}")
            print(f"\nUser prompt:\n{user_prompt}")

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        return response

    def _render_prompts(self, variables):
        environment = Environment(
            autoescape=select_autoescape(default_for_string=False),
            undefined=StrictUndefined,
        )
        system_prompt = environment.from_string(get_settings().pr_line_questions_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_line_questions_prompt.user).render(variables)
        return system_prompt, user_prompt

    def _fit_conversation_history(self, variables, model):
        conversation_history = variables.get("conversation_history", "")
        encoder = None
        try:
            completion_tokens = int(get_settings().config.get("max_output_tokens", 0))
        except (TypeError, ValueError):
            completion_tokens = 0
        if completion_tokens <= 0:
            completion_tokens = OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
        max_tokens = max(get_max_tokens(model) - completion_tokens, 0)

        def render_with_history(history):
            prompt_variables = copy.deepcopy(variables)
            prompt_variables["conversation_history"] = history
            return self._render_prompts(prompt_variables)

        def count_prompts(prompts):
            nonlocal encoder
            system_prompt, user_prompt = prompts
            try:
                model_token_count = token_counter(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                if model_token_count > 0:
                    return model_token_count
            except Exception as e:
                get_logger().debug(f"Model-aware token counting failed for {model}: {e}")
            if encoder is None:
                encoder = TokenEncoder.get_token_encoder(model)
            return len(encoder.encode(system_prompt, disallowed_special=())) + len(
                encoder.encode(user_prompt, disallowed_special=()))

        if count_prompts(render_with_history(conversation_history)) <= max_tokens:
            return conversation_history

        if count_prompts(render_with_history("")) > max_tokens:
            raise ValueError(
                f"The /ask_line prompt exceeds the token limit for {model} even without conversation history"
            )

        truncation_marker = "\n...(truncated)\n"
        low, high = 0, len(conversation_history)
        best_history = ""
        while low <= high:
            keep_chars = (low + high) // 2
            candidate = (
                truncation_marker + conversation_history[-keep_chars:]
                if keep_chars
                else ""
            )
            if count_prompts(render_with_history(candidate)) <= max_tokens:
                best_history = candidate
                low = keep_chars + 1
            else:
                high = keep_chars - 1

        get_logger().warning(
            f"Conversation history was clipped for /ask_line to fit the {max_tokens}-token input limit"
        )
        return best_history
