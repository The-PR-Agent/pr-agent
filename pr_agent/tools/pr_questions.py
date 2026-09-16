import copy
import json
import posixpath
import re
from functools import partial

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ai_handlers.litellm_helpers import AssistantTurn
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    get_pr_diff,
    retry_with_fallback_models,
)
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import (
    ModelType,
    clip_tokens,
    decode_user_text_args,
    format_pr_questions_header,
    get_max_tokens,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage

READ_PR_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_pr_file",
        "description": "Read the full head content of a file modified in this pull request. Use this tool when you need more context around changes than what is visible in the diff alone.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The repository path of the modified file to read.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
_MAX_TOOL_CALLS_PER_TURN = 1
_TOOL_CALL_OVERHEAD_TOKENS = 150


class PRQuestions:
    def __init__(self, pr_url: str, args=None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        question_str = self.parse_args(args)
        self.pr_url = pr_url
        self.git_provider = get_git_provider()(pr_url)
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
            "enable_tools": settings.pr_questions.get("enable_tools", False),
        }
        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          get_settings().pr_questions_prompt.system,
                                          get_settings().pr_questions_prompt.user)
        self.patches_diff = None
        self.prediction = None
        self._diff_files = None

    def parse_args(self, args):
        return decode_user_text_args(args)

    async def run(self):
        get_logger().info(f'Answering a PR question about the PR {self.pr_url} ')
        relevant_configs = {'pr_questions': dict(get_settings().pr_questions),
                            'config': dict(get_settings().config)}
        get_logger().debug("Relevant configs", artifacts=relevant_configs)
        if get_settings().config.publish_output:
            self.git_provider.publish_comment("Preparing answer...", is_temporary=True)

        # identify image
        img_path = self.identify_image_in_comment()
        if img_path:
            get_logger().debug("Image path identified", artifact=img_path)

        await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.WEAK)

        pr_comment = self._prepare_pr_answer()
        get_logger().debug("PR output", artifact=pr_comment)

        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_questions.enable_help_text:
            pr_comment += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            pr_comment += HelpMessage.get_ask_usage_guide()
            pr_comment += "\n</details>\n"

        if get_settings().config.publish_output:
            self._publish_answer(pr_comment)
            self.git_provider.remove_initial_comment()
        return ""

    def _publish_answer(self, answer: str):
        comment_id = get_settings().get("comment_id", "")
        if comment_id and self.git_provider.supports_threaded_pr_questions():
            return self.git_provider.reply_to_comment_from_comment_id(comment_id, answer)
        return self.git_provider.publish_comment(answer)

    def _load_conversation_history(self) -> str:
        if (not self.git_provider.supports_threaded_pr_questions()
                or not get_settings().pr_questions.use_conversation_history):
            return ""
        comment_id = get_settings().get("comment_id", "")
        if not comment_id:
            return ""
        origin_comment_id = get_settings().get("origin_comment_id", comment_id)
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
        if '![image]' in self.question_str:
            # assuming structure:
            # /ask question ...  > ![image](img_path)
            img_path = self.question_str.split('![image]')[1].strip().strip('()')
            self.vars['img_path'] = img_path
        elif 'https://' in self.question_str and ('.png' in self.question_str or 'jpg' in self.question_str): # direct image link
            # include https:// in the image path
            img_path = 'https://' + self.question_str.split('https://')[1]
            self.vars['img_path'] = img_path
        return img_path

    async def _prepare_prediction(self, model: str):
        token_handler = self.token_handler
        if self._should_use_tools(model):
            max_tool_tokens = get_settings().pr_questions.get("max_tool_tokens", 4000)
            token_handler = copy.copy(self.token_handler)
            token_handler.prompt_tokens = (
                self.token_handler.prompt_tokens + max_tool_tokens + _TOOL_CALL_OVERHEAD_TOKENS
            )

        self.patches_diff = get_pr_diff(self.git_provider, token_handler, model)
        if self.patches_diff:
            get_logger().debug("PR diff", artifact=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().error("Error getting PR diff")
            self.prediction = ""

    def _should_use_tools(self, model: str) -> bool:
        """Determine whether tool calling should be used for this /ask invocation."""
        if not get_settings().pr_questions.get("enable_tools", False):
            return False
        if 'img_path' in self.vars:
            return False
        if not hasattr(self.ai_handler, "chat_completion_with_tools"):
            return False
        if not hasattr(self.ai_handler, "supports_tool_calling"):
            return False
        if not self.ai_handler.supports_tool_calling(model):
            get_logger().debug(f"Model {model} does not support tool calling; using legacy /ask path")
            return False
        return True

    def _get_diff_files_map(self) -> dict:
        """Build a mapping from canonical relative and provider filenames to FilePatchInfo."""
        if self._diff_files is None:
            self._diff_files = self.git_provider.get_diff_files()
        file_map = {}
        for f in self._diff_files:
            file_map[f.filename] = f
            # Support provider-reported names with leading slash (e.g. Azure DevOps) safely
            if f.filename.startswith("/"):
                file_map[f.filename.lstrip("/")] = f
            if getattr(f, "base_filename", None):
                file_map[f.base_filename] = f
                if f.base_filename.startswith("/"):
                    file_map[f.base_filename.lstrip("/")] = f
        return file_map

    def _execute_read_pr_file(self, arguments_json: str, max_tokens_limit: int | None = None) -> str:
        """Validate and execute a read_pr_file tool call. Returns a JSON string."""
        configured_max = get_settings().pr_questions.get("max_tool_tokens", 4000)
        max_tool_tokens = configured_max
        if max_tokens_limit is not None:
            max_tool_tokens = min(configured_max, max(0, max_tokens_limit))

        # Parse arguments
        try:
            args = json.loads(arguments_json)
        except (json.JSONDecodeError, TypeError) as e:
            return json.dumps({"error": f"Malformed tool arguments: {e}"})

        if not isinstance(args, dict):
            return json.dumps({"error": "Tool arguments must be a JSON object"})

        # Reject extra properties
        allowed_keys = {"path"}
        extra = set(args.keys()) - allowed_keys
        if extra:
            return json.dumps({"error": f"Unexpected arguments: {', '.join(sorted(extra))}"})

        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return json.dumps({"error": "Missing or empty 'path' argument"})

        path = path.strip()

        # Path security: reject Windows drive-qualified paths, absolute, traversal, backslash, NUL
        if "\x00" in path:
            return json.dumps({"error": "Invalid path: contains null byte"})
        if "\\" in path:
            return json.dumps({"error": "Invalid path: use forward slashes (POSIX paths)"})
        if re.match(r"^[A-Za-z]:", path):
            return json.dumps({"error": "Invalid path: Windows drive-qualified paths are not allowed"})
        if posixpath.isabs(path):
            return json.dumps({"error": "Invalid path: absolute paths are not allowed"})
        if any(segment == ".." for segment in path.split("/")):
            return json.dumps({"error": "Invalid path: directory traversal is not allowed"})
        normalized = posixpath.normpath(path)
        if (
            normalized in (".", "..")
            or normalized.startswith("..")
            or "/../" in normalized
            or normalized.endswith("/..")
        ):
            return json.dumps({"error": "Invalid path: directory traversal is not allowed"})

        # Match against PR changed files
        file_map = self._get_diff_files_map()
        diff_file = file_map.get(normalized) or file_map.get(path)
        if diff_file is None:
            return json.dumps({"error": f"File '{path}' was not modified in this pull request"})

        # Deleted files
        if diff_file.edit_type == EDIT_TYPE.DELETED:
            return json.dumps({"error": f"File '{path}' was deleted in this pull request"})

        # Check head file availability and completeness
        if diff_file.head_file is None:
            return json.dumps({"error": f"Complete head content for '{path}' is unavailable"})
        if not getattr(diff_file, "head_file_is_complete", True):
            return json.dumps({"error": f"Complete head content for '{path}' is unavailable (partial content only)"})

        content = diff_file.head_file
        clipped = clip_tokens(content, max_tool_tokens, add_three_dots=True)
        is_truncated = len(clipped) < len(content)

        result = {"path": diff_file.filename, "content": clipped}
        if is_truncated:
            result["truncated"] = True
            result["max_tokens"] = max_tool_tokens
        return json.dumps(result)

    async def _get_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff
        variables["enable_tools"] = self._should_use_tools(model)
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_questions_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_questions_prompt.user).render(variables)

        # Image /ask: always use legacy text-only path
        if 'img_path' in variables:
            img_path = self.vars['img_path']
            response, finish_reason = await (self.ai_handler.chat_completion
                                             (model=model, temperature=get_settings().config.temperature,
                                              system=system_prompt, user=user_prompt, img_path=img_path))
            return response

        # Tool-calling path
        if variables["enable_tools"]:
            return await self._get_prediction_with_tools(model, system_prompt, user_prompt)

        # Legacy text-only path
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        return response

    async def _get_prediction_with_tools(self, model: str, system_prompt: str, user_prompt: str) -> str:
        """Execute bounded tool-calling orchestration: at most one tool round."""
        temperature = get_settings().config.temperature

        # Turn 1: initial request with tool
        turn1: AssistantTurn = await self.ai_handler.chat_completion_with_tools(
            model=model,
            system=system_prompt,
            user=user_prompt,
            temperature=temperature,
            tools=[READ_PR_FILE_TOOL],
        )

        # No tool calls: return text directly
        if not turn1.has_tool_calls:
            return turn1.content or ""

        # Enforce single-call limit: if multiple tool calls, execute none
        if len(turn1.tool_calls) > _MAX_TOOL_CALLS_PER_TURN:
            get_logger().warning(
                f"Model returned {len(turn1.tool_calls)} tool calls but limit is "
                f"{_MAX_TOOL_CALLS_PER_TURN}; using text-only fallback"
            )
            if turn1.content:
                return turn1.content
            response, _ = await self.ai_handler.chat_completion(
                model=model, temperature=temperature, system=system_prompt, user=user_prompt)
            return response

        call = turn1.tool_calls[0]

        # Protocol validation: id and name must be non-empty strings, type must be 'function'
        if not isinstance(call.id, str) or not call.id.strip():
            get_logger().warning("Tool call missing valid 'id'; rejecting turn and falling back")
            if turn1.content:
                return turn1.content
            response, _ = await self.ai_handler.chat_completion(
                model=model, temperature=temperature, system=system_prompt, user=user_prompt
            )
            return response

        if call.type != "function":
            get_logger().warning(f"Tool call has invalid type '{call.type}'; rejecting turn and falling back")
            if turn1.content:
                return turn1.content
            response, _ = await self.ai_handler.chat_completion(
                model=model, temperature=temperature, system=system_prompt, user=user_prompt
            )
            return response

        if not isinstance(call.name, str) or not call.name.strip():
            get_logger().warning("Tool call has missing or empty name; rejecting turn and falling back")
            if turn1.content:
                return turn1.content
            response, _ = await self.ai_handler.chat_completion(
                model=model, temperature=temperature, system=system_prompt, user=user_prompt
            )
            return response

        # Validate the tool name
        if call.name != "read_pr_file":
            get_logger().warning(f"Unknown tool call '{call.name}'; returning error to model")
            tool_result = json.dumps({"error": f"Unknown tool: '{call.name}'"})
        else:
            max_model_tokens = get_max_tokens(model)
            count_fn = (
                self.token_handler.count_tokens
                if hasattr(self, "token_handler") and self.token_handler
                else lambda text: len(text) // 4
            )
            used_tokens = (
                count_fn(system_prompt)
                + count_fn(user_prompt)
                + count_fn(turn1.content or "")
                + count_fn(call.arguments or "")
                + _TOOL_CALL_OVERHEAD_TOKENS
                + OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD
            )
            remaining_budget = max_model_tokens - used_tokens
            tool_result = self._execute_read_pr_file(call.arguments, max_tokens_limit=remaining_budget)
            get_logger().debug("Tool result", artifact={"tool": call.name, "path": call.arguments})

        # Build continuation messages
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {
                "role": "assistant",
                "content": turn1.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": tool_result,
            },
        ]

        # Turn 2: final response using the SAME model.
        # Retain tools=[READ_PR_FILE_TOOL] so Bedrock Converse accepts the continuation history,
        # but do not execute any further tool calls.
        turn2: AssistantTurn = await self.ai_handler.chat_completion_with_tools(
            model=model,
            temperature=temperature,
            messages=messages,
            tools=[READ_PR_FILE_TOOL],
        )

        # Turn 2 must produce text; if it requests more tools without content, fall back to text completion
        if turn2.has_tool_calls:
            get_logger().warning("Model requested additional tool calls in turn 2; terminating tool flow")
            if turn2.content and turn2.content.strip():
                return turn2.content
            response, _ = await self.ai_handler.chat_completion(
                model=model, temperature=temperature, system=system_prompt, user=user_prompt
            )
            return response

        return turn2.content or ""

    def _prepare_pr_answer(self) -> str:
        model_answer = self.prediction.strip()
        # sanitize the answer so that no line will start with "/", which would
        # trigger quick actions on providers that support them (e.g. GitLab)
        model_answer_sanitized = model_answer.replace("\n/", "\n /")
        model_answer_sanitized = model_answer_sanitized.replace("\r/", "\r /")
        if model_answer_sanitized.startswith("/"):
            model_answer_sanitized = " " + model_answer_sanitized
        if model_answer_sanitized != model_answer:
            get_logger().debug("Sanitized model answer",
                               artifact={"model_answer": model_answer, "sanitized_answer": model_answer_sanitized})
        answer_header = format_pr_questions_header(
            escape_markdown=self.git_provider.is_supported("markdown_backslash_escapes")
        )
        answer_str = f"{answer_header}\n{self.question_str}\n\n"
        answer_str += f"### **Answer:**\n{model_answer_sanitized}\n\n"
        return answer_str
