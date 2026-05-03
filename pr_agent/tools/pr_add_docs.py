import copy
import textwrap
from functools import partial
from typing import Dict

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import get_pr_diff, retry_with_fallback_models
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import load_yaml
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.log import get_logger


class PRAddDocs:
    def __init__(self, pr_url: str, cli_mode=False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):

        self.git_provider = get_git_provider()(pr_url)
        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )

        self.ai_handler = ai_handler()
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
            "extra_instructions": get_settings().pr_add_docs.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            'docs_for_language': get_docs_for_language(self.main_language,
                                                       get_settings().pr_add_docs.docs_style),
        }
        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          get_settings().pr_add_docs_prompt.system,
                                          get_settings().pr_add_docs_prompt.user)

    async def run(self):
        try:
            get_logger().info('Generating code Docs for PR...')
            if get_settings().config.publish_output:
                self.git_provider.publish_comment("Generating Documentation...", is_temporary=True)

            get_logger().info('Preparing PR documentation...')
            await retry_with_fallback_models(self._prepare_prediction)
            data = self._prepare_pr_code_docs()
            if (not data) or (not 'Code Documentation' in data):
                get_logger().info('No code documentation found for PR.')
                return

            if get_settings().config.publish_output:
                get_logger().info('Pushing PR documentation...')
                self.git_provider.remove_initial_comment()
                get_logger().info('Pushing inline code documentation...')
                self.push_inline_docs(data)
        except Exception as e:
            get_logger().error(f"Failed to generate code documentation for PR, error: {e}")

    async def _prepare_prediction(self, model: str):
        get_logger().info('Getting PR diff...')

        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False)

        get_logger().info('Getting AI prediction...')
        self.prediction = await self._get_prediction(model)

    async def _get_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff  # update diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_add_docs_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_add_docs_prompt.user).render(variables)
        if get_settings().config.verbosity_level >= 2:
            get_logger().info(f"\nSystem prompt:\n{system_prompt}")
            get_logger().info(f"\nUser prompt:\n{user_prompt}")
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)

        return response

    def _prepare_pr_code_docs(self) -> Dict:
        docs = self.prediction.strip()
        data = load_yaml(docs)
        if isinstance(data, list):
            data = {'Code Documentation': data}
        return data

    def push_inline_docs(self, data):
        docs = []

        if not data['Code Documentation']:
            return self.git_provider.publish_comment('No code documentation found to improve this PR.')

        for d in data['Code Documentation']:
            try:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"add_docs: {d}")
                relevant_file = d['relevant file'].strip()
                relevant_line = int(d['relevant line'])  # absolute position
                documentation = d['documentation']
                doc_placement = d['doc placement'].strip()
                if documentation:
                    new_code_snippet = self.dedent_code(relevant_file, relevant_line, documentation, doc_placement,
                                                        add_original_line=True)

                    body = f"**Suggestion:** Proposed documentation\n```suggestion\n" + new_code_snippet + "\n```"
                    original_suggestion = {
                        "relevant_file": relevant_file,
                        "relevant_lines_start": relevant_line,
                        "relevant_lines_end": relevant_line,
                        "label": "documentation",
                        "suggestion_content": "Proposed documentation",
                        "improved_code": new_code_snippet,
                    }
                    docs.append({'body': body, 'relevant_file': relevant_file,
                                             'relevant_lines_start': relevant_line,
                                             'relevant_lines_end': relevant_line,
                                             'original_suggestion': original_suggestion})
            except Exception:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"Could not parse code docs: {d}")

        is_successful = self.git_provider.publish_code_suggestions(docs)
        if not is_successful:
            get_logger().info("Failed to publish code docs, trying to publish each docs separately")
            for doc_suggestion in docs:
                self.git_provider.publish_code_suggestions([doc_suggestion])

    def dedent_code(self, relevant_file, relevant_lines_start, new_code_snippet, doc_placement='after',
                    add_original_line=False):
        try:  # dedent code snippet
            self.diff_files = self.git_provider.diff_files if self.git_provider.diff_files \
                else self.git_provider.get_diff_files()
            original_initial_line = None
            for file in self.diff_files:
                if file.filename.strip() == relevant_file:
                    original_initial_line = file.head_file.splitlines()[relevant_lines_start - 1]
                    break
            if original_initial_line:
                if doc_placement == 'after':
                    line = file.head_file.splitlines()[relevant_lines_start]
                else:
                    line = original_initial_line
                suggested_initial_line = new_code_snippet.splitlines()[0]
                original_initial_spaces = len(line) - len(line.lstrip())
                suggested_initial_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
                delta_spaces = original_initial_spaces - suggested_initial_spaces
                if delta_spaces > 0:
                    new_code_snippet = textwrap.indent(new_code_snippet, delta_spaces * " ").rstrip('\n')
                if add_original_line:
                    if doc_placement == 'after':
                        new_code_snippet = original_initial_line + "\n" + new_code_snippet
                    else:
                        new_code_snippet = new_code_snippet.rstrip() + "\n" + original_initial_line
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Could not dedent code snippet for file {relevant_file}, error: {e}")

        return new_code_snippet


def get_docs_for_language(language, style):
    language = language.lower()
    if language == 'java':
        return "Javadocs"
    elif language in ['python', 'lisp', 'clojure']:
        return f"Docstring ({style})"
    elif language in ['javascript', 'typescript']:
        return "JSdocs"
    elif language == 'c++':
        return "Doxygen"
    else:
        return "Docs"
