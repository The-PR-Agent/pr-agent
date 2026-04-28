import shlex
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import get_logger
from pr_agent.tools.registry import ToolRegistry
from pr_agent.tools.pr_reviewer import PRReviewer

# Ensure all tools are imported to trigger registration
import pr_agent.tools.pr_add_docs
import pr_agent.tools.pr_code_suggestions
import pr_agent.tools.pr_config
import pr_agent.tools.pr_description
import pr_agent.tools.pr_generate_labels
import pr_agent.tools.pr_help_docs
import pr_agent.tools.pr_help_message
import pr_agent.tools.pr_line_questions
import pr_agent.tools.pr_questions
import pr_agent.tools.pr_similar_issue
import pr_agent.tools.pr_update_changelog
import pr_agent.tools.ticket_pr_compliance_check

# command2class = {
#     "auto_review": PRReviewer,
#     "answer": PRReviewer,
#     "review": PRReviewer,
#     "review_pr": PRReviewer,
#     "describe": PRDescription,
#     "describe_pr": PRDescription,
#     "improve": PRCodeSuggestions,
#     "improve_code": PRCodeSuggestions,
#     "ask": PRQuestions,
#     "ask_question": PRQuestions,
#     "ask_line": PR_LineQuestions,
#     "update_changelog": PRUpdateChangelog,
#     "config": PRConfig,
#     "settings": PRConfig,
#     "help": PRHelpMessage,
#     "similar_issue": PRSimilarIssue,
#     "add_docs": PRAddDocs,
#     "generate_labels": PRGenerateLabels,
#     "help_docs": PRHelpDocs,
# }

# commands = list(command2class.keys())
commands = ToolRegistry.get_all_commands()



class PRAgent:
    def __init__(self, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.ai_handler = ai_handler  # will be initialized in run_action

    async def _handle_request(self, pr_url, request, notify=None) -> bool:
        # First, apply repo specific settings if exists
        apply_repo_settings(pr_url)

        # Then, apply user specific settings if exists
        if isinstance(request, str):
            request = request.replace("'", "\\'")
            lexer = shlex.shlex(request, posix=True)
            lexer.whitespace_split = True
            action, *args = list(lexer)
        else:
            action, *args = request

        # validate args
        is_valid, arg = CliArgs.validate_user_args(args)
        if not is_valid:
            get_logger().error(
                f"CLI argument for param '{arg}' is forbidden. Use instead a configuration file."
            )
            return False

        # Update settings from args
        args = update_settings_from_args(args)

        # Append the response language in the extra instructions
        response_language = get_settings().config.get('response_language', 'en-us')
        if response_language.lower() != 'en-us':
            get_logger().info(f'User has set the response language to: {response_language}')
            for key in get_settings():
                setting = get_settings().get(key)
                if str(type(setting)) == "<class 'dynaconf.utils.boxing.DynaBox'>":
                    if hasattr(setting, 'extra_instructions'):
                        current_extra_instructions = setting.extra_instructions
                        
                        # Define the language-specific instruction and the separator
                        lang_instruction_text = f"Your response MUST be written in the language corresponding to locale code: '{response_language}'. This is crucial."
                        separator_text = "\n======\n\nIn addition, "

                        # Check if the specific language instruction is already present to avoid duplication
                        if lang_instruction_text not in str(current_extra_instructions):
                            if current_extra_instructions: # If there's existing text
                                setting.extra_instructions = str(current_extra_instructions) + separator_text + lang_instruction_text
                            else: # If extra_instructions was None or empty
                                setting.extra_instructions = lang_instruction_text
                        # If lang_instruction_text is already present, do nothing.

        action = action.lstrip("/").lower()
        tool_class = ToolRegistry.get_tool(action)
        if not tool_class:
            get_logger().warning(f"Unknown command: {action}")
            return False
            
        with get_logger().contextualize(command=action, pr_url=pr_url):
            get_logger().info("PR-Agent request handler started", analytics=True)
            
            if notify:
                notify()

            if tool_class == PRReviewer:
                is_answer = (action == "answer")
                is_auto = (action == "auto_review")
                await PRReviewer(pr_url, is_answer=is_answer, is_auto=is_auto, args=args, ai_handler=self.ai_handler).run()
            else:
                await tool_class(pr_url, ai_handler=self.ai_handler, args=args).run()
            return True

    async def handle_request(self, pr_url, request, notify=None) -> bool:
        try:
            return await self._handle_request(pr_url, request, notify)
        except:
            get_logger().exception("Failed to process the command.")
            return False
