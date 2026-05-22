import copy
from functools import partial

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import get_pr_diff, retry_with_fallback_models
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import GitLabProvider, get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger
from pr_agent.mcp.integration import maybe_chat_completion_with_mcp
from pr_agent.servers.help import HelpMessage


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
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.git_provider.get_pr_description(),
            "language": self.main_pr_language,
            "diff": "",  # empty diff for initial calculation
            "questions": self.question_str,
            "commit_messages_str": self.git_provider.get_commit_messages(),
        }
        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          get_settings().pr_questions_prompt.system,
                                          get_settings().pr_questions_prompt.user)
        self.patches_diff = None
        self.prediction = None

    def parse_args(self, args):
        if args and len(args) > 0:
            question_str = " ".join(args)
        else:
            question_str = ""
        return question_str

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
            get_logger().debug(f"Image path identified", artifact=img_path)

        await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.WEAK)

        pr_comment = self._prepare_pr_answer()
        get_logger().debug(f"PR output", artifact=pr_comment)

        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_questions.enable_help_text:
            pr_comment += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            pr_comment += HelpMessage.get_ask_usage_guide()
            pr_comment += "\n</details>\n"

        if get_settings().config.publish_output:
            self.git_provider.publish_comment(pr_comment)
            self.git_provider.remove_initial_comment()
        return ""

    def identify_image_in_comment(self):
        img_path = ""
        if "![image]" in self.question_str:
            # assuming structure:
            # /ask question ...  > ![image](img_path)
            img_path = self.question_str.split("![image]")[1].strip().strip("()")
            self.vars["img_path"] = img_path
        elif "https://" in self.question_str and (".png" in self.question_str or "jpg" in self.question_str):  # direct image link
            # include https:// in the image path
            img_path = "https://" + self.question_str.split("https://")[1]
            self.vars["img_path"] = img_path
        return img_path

    async def _prepare_prediction(self, model: str):
        self.patches_diff = get_pr_diff(self.git_provider, self.token_handler, model)
        if self.patches_diff:
            get_logger().debug(f"PR diff", artifact=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().error(f"Error getting PR diff")
            self.prediction = ""

    async def _get_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_questions_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_questions_prompt.user).render(variables)
        img_path = variables.get("img_path")
        response, finish_reason = await maybe_chat_completion_with_mcp(
            self.ai_handler,
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt,
            img_path=img_path,
            command_name="ask",
        )
        return response

    def gitlab_protections(self, model_answer: str) -> str:
        github_quick_actions_MR = ["/approve", "/close", "/merge", "/reopen", "/unapprove", "/title", "/assign",
                                "/copy_metadata", "/target_branch"]
        if any(action in model_answer for action in github_quick_actions_MR):
            str_err = "Model answer contains GitHub quick actions, which are not supported in GitLab"
            get_logger().error(str_err)
            return str_err
        return model_answer

    def _prepare_pr_answer(self) -> str:
        model_answer = self.prediction.strip()
        # sanitize the answer so that no line will start with "/"
        model_answer_sanitized = model_answer.replace("\n/", "\n /")
        model_answer_sanitized = model_answer_sanitized.replace("\r/", "\r /")
        if isinstance(self.git_provider, GitLabProvider):
            model_answer_sanitized = self.gitlab_protections(model_answer_sanitized)
        if model_answer_sanitized.startswith("/"):
            model_answer_sanitized = " " + model_answer_sanitized
        if model_answer_sanitized != model_answer:
            get_logger().debug(f"Sanitized model answer",
                               artifact={"model_answer": model_answer, "sanitized_answer": model_answer_sanitized})


        answer_str = f"### **Ask**❓\n{self.question_str}\n\n"
        answer_str += f"### **Answer:**\n{model_answer_sanitized}\n\n"
        return answer_str
