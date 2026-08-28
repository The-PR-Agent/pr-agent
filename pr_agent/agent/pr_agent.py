import json
import shlex
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import get_logger
from pr_agent.tools.pr_add_docs import PRAddDocs
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_config import PRConfig
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from pr_agent.tools.pr_help_message import PRHelpMessage
from pr_agent.tools.pr_line_questions import PR_LineQuestions
from pr_agent.tools.pr_questions import PRQuestions
from pr_agent.tools.pr_reviewer import PRReviewer
from pr_agent.tools.pr_similar_issue import PRSimilarIssue
from pr_agent.tools.pr_update_changelog import PRUpdateChangelog

command2class = {
    "auto_review": PRReviewer,
    "answer": PRReviewer,
    "review": PRReviewer,
    "review_pr": PRReviewer,
    "describe": PRDescription,
    "describe_pr": PRDescription,
    "improve": PRCodeSuggestions,
    "improve_code": PRCodeSuggestions,
    "ask": PRQuestions,
    "ask_question": PRQuestions,
    "ask_line": PR_LineQuestions,
    "update_changelog": PRUpdateChangelog,
    "config": PRConfig,
    "settings": PRConfig,
    "help": PRHelpMessage,
    "similar_issue": PRSimilarIssue,
    "add_docs": PRAddDocs,
    "generate_labels": PRGenerateLabels,
    # SECURITY: "/help_docs" is temporarily disabled while the clone-target validation
    # fix is reviewed (see issue #2445). Re-enable by restoring `"help_docs": PRHelpDocs`
    # and its import once the hardening PR is merged.
}

commands = list(command2class.keys())


def _split_command(command: str) -> list[tuple[str, bool]]:
    """Split an auto command and retain whether each token was quoted.

    ``shlex.split`` removes quote markers before setting overrides are handed to
    ``yaml.safe_load``. That makes a quoted ``#`` look like a YAML comment and
    changes quoted scalar values such as ``"true"`` into booleans. This small
    tokenizer keeps the normal shell-style token boundaries while recording the
    presence of quotes so setting values can be normalized as strings later.

    Apostrophes inside a word remain literal, matching the legacy request parser
    (for example, ``What's``). An apostrophe at a token boundary or immediately
    after ``=`` still starts a single-quoted value, as used by the documented
    webhook configuration examples.
    """
    tokens = []
    token = []
    quote = None
    value_was_quoted = False
    equals_seen = False
    token_started = False

    def flush_token():
        nonlocal equals_seen, token_started, token, value_was_quoted
        if token_started:
            tokens.append(("".join(token), value_was_quoted))
        token = []
        equals_seen = False
        token_started = False
        value_was_quoted = False

    index = 0
    while index < len(command):
        character = command[index]
        if quote is None:
            if character.isspace():
                flush_token()
            elif character == "\\":
                if index + 1 >= len(command):
                    raise ValueError("No escaped character")
                token.append(command[index + 1])
                token_started = True
                index += 1
            elif character == "=":
                token.append(character)
                equals_seen = True
                token_started = True
            elif character == '"':
                quote = character
                value_was_quoted = equals_seen
                token_started = True
            elif character == "'" and (not token_started or command[index - 1] == "="):
                quote = character
                value_was_quoted = equals_seen
                token_started = True
            else:
                token.append(character)
                token_started = True
        elif quote == "'":
            if character == "'":
                quote = None
            else:
                token.append(character)
        else:
            if character == '"':
                quote = None
            elif character == "\\":
                if index + 1 >= len(command):
                    raise ValueError("No escaped character")
                escaped = command[index + 1]
                if escaped in {'"', "\\", "$", "`"}:
                    token.append(escaped)
                elif escaped != "\n":
                    token.extend(("\\", escaped))
                index += 1
            else:
                token.append(character)
        index += 1

    if quote is not None:
        raise ValueError("No closing quotation")
    flush_token()
    return tokens


def prepare_command(command: str) -> list[str]:
    """Apply command-line settings while preserving quoted argument boundaries.

    Webhook adapters use this before handing configured commands to ``PRAgent``. Parsing
    with ``str.split(" ")`` breaks values such as ``--section.key=\"words with spaces\"``;
    the tokenizer keeps the value as one argument and preserves explicit quoting for YAML.
    Returning the token list avoids serializing it back to a string, which would otherwise
    be re-parsed by ``PRAgent`` and could alter quoted arguments.
    """
    tokens = _split_command(command)
    if not tokens:
        return []

    (action, _), *token_args = tokens
    args = []
    for argument, value_was_quoted in token_args:
        if value_was_quoted and argument.startswith("--") and "=" in argument:
            key, value = argument.split("=", 1)
            argument = f"{key}={json.dumps(value, ensure_ascii=False)}"
        args.append(argument)
    other_args = update_settings_from_args(args)
    return [action] + other_args


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
        if action not in command2class:
            get_logger().warning(f"Unknown command: {action}")
            return False
        with get_logger().contextualize(command=action, pr_url=pr_url):
            get_logger().info("PR-Agent request handler started", analytics=True)
            if action == "answer":
                if notify:
                    notify()
                await PRReviewer(pr_url, is_answer=True, args=args, ai_handler=self.ai_handler).run()
            elif action == "auto_review":
                await PRReviewer(pr_url, is_auto=True, args=args, ai_handler=self.ai_handler).run()
            elif action in command2class:
                if notify:
                    notify()

                await command2class[action](pr_url, ai_handler=self.ai_handler, args=args).run()
            else:
                return False
            return True

    async def handle_request(self, pr_url, request, notify=None) -> bool:
        try:
            return await self._handle_request(pr_url, request, notify)
        except Exception:
            get_logger().exception("Failed to process the command.")
            return False
