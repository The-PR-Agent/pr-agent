import argparse
import copy
from functools import partial

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers, extract_hunk_lines_from_patch)
from pr_agent.algo.pr_processing import get_pr_diff, retry_with_fallback_models
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.registry import ToolRegistry


@ToolRegistry.register("ask_line")
class PR_LineQuestions:
    def __init__(self, pr_url: str, args=None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.question_str = self.parse_args(args)
        self.git_provider = get_git_provider()(pr_url)
        self.main_pr_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_pr_language

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "diff": "",  # empty diff for initial calculation
            "question": self.question_str,
            "full_hunk": "",
            "selected_lines": "",
            "conversation_history": "",  
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
        ask_diff = get_settings().get('ask_diff_hunk', "")
        line_start = get_settings().get('line_start', '')
        line_end = get_settings().get('line_end', '')
        side = get_settings().get('side', 'RIGHT')
        file_name = get_settings().get('file_name', '')
        comment_id = get_settings().get('comment_id', '')
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
        if self.patch_with_lines:
            model_answer = await retry_with_fallback_models(self._get_prediction, model_type=ModelType.WEAK)
            # sanitize the answer so that no line will start with "/"
            model_answer_sanitized = model_answer.strip().replace("\n/", "\n /")
            if model_answer_sanitized.startswith("/"):
                model_answer_sanitized = " " + model_answer_sanitized

            get_logger().info('Preparing answer...')
            if comment_id:
                self.git_provider.reply_to_comment_from_comment_id(comment_id, model_answer_sanitized)
            else:
                self.git_provider.publish_comment(model_answer_sanitized)

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
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_line_questions_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_line_questions_prompt.user).render(variables)
        if get_settings().config.verbosity_level >= 2:
            # get_logger().info(f"\nSystem prompt:\n{system_prompt}")
            # get_logger().info(f"\nUser prompt:\n{user_prompt}")
            print(f"\nSystem prompt:\n{system_prompt}")
            print(f"\nUser prompt:\n{user_prompt}")

        response, finish_reason, _ = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        return response
