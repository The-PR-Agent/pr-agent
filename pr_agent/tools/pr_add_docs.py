import copy
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.git_patch_processing import split_diff_lines
from pr_agent.algo.pr_processing import (
    OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    FallbackEligibleError,
    get_pr_diff,
    retry_with_fallback_models,
)
from pr_agent.algo.prompt_fragments import render_diff_hunk_format
from pr_agent.algo.run_details import record_command_failure
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.utils import get_suggestion_fence, load_yaml
from pr_agent.config_loader import get_settings, get_verbosity_level
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.git_provider import IncompleteProviderPullRequestFilesError, get_main_pr_language
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
            "diff_hunk_format": render_diff_hunk_format(
                include_line_numbers=True,
                include_ai_metadata=False,
            ),
            'docs_for_language': get_docs_for_language(self.main_language,
                                                       get_settings().pr_add_docs.docs_style),
        }

    async def run(self):
        temporary_comment_published = False
        publication_failed = False
        try:
            get_logger().info('Generating code Docs for PR...')
            if get_settings().config.publish_output:
                self.git_provider.publish_comment("Generating Documentation...", is_temporary=True)
                temporary_comment_published = True

            get_logger().info('Preparing PR documentation...')
            try:
                data = await retry_with_fallback_models(self._prepare_prediction, git_provider=self.git_provider)
            except Exception as prediction_error:
                if (
                    get_settings().config.publish_output
                    and not isinstance(prediction_error, IncompleteProviderPullRequestFilesError)
                ):
                    try:
                        self.git_provider.publish_tool_failure(
                            "add_docs", "Failed to generate code documentation for this PR."
                        )
                    except Exception as comment_error:
                        get_logger().warning(f"Failed to publish the documentation failure comment: {comment_error}")
                raise
            if self.prediction is None:
                return
            if (not data) or ("Code Documentation" not in data):
                get_logger().info('No code documentation found for PR.')
                return

            if get_settings().config.publish_output:
                get_logger().info('Pushing PR documentation...')
                self.git_provider.remove_initial_comment()
                temporary_comment_published = False
                get_logger().info('Pushing inline code documentation...')
                publication_result = self.push_inline_docs(data)
                if publication_result is False:
                    publication_failed = True
                    self.git_provider.publish_tool_failure(
                        "add_docs", "Failed to publish code documentation for this PR."
                    )
                    raise RuntimeError("Failed to publish code documentation after individual retries")
        except Exception as e:
            get_logger().error(f"Failed to generate code documentation for PR, error: {e}")
            record_command_failure()
            if (
                isinstance(e, IncompleteProviderPullRequestFilesError)
                or get_settings().config.get("propagate_tool_errors", False)
            ):
                raise
            if publication_failed:
                return False
        finally:
            if temporary_comment_published:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception as cleanup_error:
                    get_logger().warning(
                        f"Failed to remove the temporary documentation comment: {cleanup_error}"
                    )

    async def _prepare_prediction(self, model: str):
        self.prediction = None
        get_logger().info('Getting PR diff...')

        variables = copy.deepcopy(self.vars)
        output_token_reserve = getattr(self.ai_handler, "get_output_token_reserve", None)
        budget = AttemptTokenBudget.for_prompt_attempt(
            model,
            getattr(self.git_provider, "pr", None),
            variables,
            get_settings().pr_add_docs_prompt.system,
            get_settings().pr_add_docs_prompt.user,
            ai_handler=self.ai_handler,
            output_token_reserve=output_token_reserve,
        )
        output = get_pr_diff(
            self.git_provider,
            budget.token_handler,
            model,
            add_line_numbers_to_hunks=True,
            disable_extra_lines=False,
            return_remaining_files=True,
            output_token_reserve=output_token_reserve,
        )
        patches_diff, remaining_files_list = output if isinstance(output, tuple) else (output, [])
        if not patches_diff and not remaining_files_list:
            # Every changed file was filtered out (lockfile, asset, ignore rule), so there is
            # nothing to document. Skip the model chain instead of failing the command.
            get_logger().info("PR has no analyzable files, skipping add_docs")
            return
        if not patches_diff:
            raise FallbackEligibleError("No PR diff fits the /add_docs request")
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
                f"The complete packed documentation diff does not fit the token limit for {model}"
            )
        self.patches_diff = fitted.optional_text
        self._attempt_system_prompt = fitted.system_prompt
        self._attempt_user_prompt = fitted.user_prompt

        get_logger().info('Getting AI prediction...')
        self.prediction = await self._get_prediction(model)
        return self._prepare_pr_code_docs()

    async def _get_prediction(self, model: str):
        system_prompt = self._attempt_system_prompt
        user_prompt = self._attempt_user_prompt
        if get_verbosity_level() >= 2:
            get_logger().info(f"\nSystem prompt:\n{system_prompt}")
            get_logger().info(f"\nUser prompt:\n{user_prompt}")
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)

        return response

    def _prepare_pr_code_docs(self) -> dict:
        if not isinstance(self.prediction, str):
            raise FallbackEligibleError("The model did not return a Code Documentation string")
        docs = self.prediction.strip()
        data = load_yaml(docs)
        if isinstance(data, list):
            data = {'Code Documentation': data}
        if isinstance(data, dict) and 'Code Documentation' in data and data['Code Documentation'] is None:
            data['Code Documentation'] = []
        if not isinstance(data, dict) or not isinstance(data.get('Code Documentation'), list):
            get_logger().warning("The model did not return a Code Documentation list",
                                 artifact={'prediction': docs})
            raise FallbackEligibleError("The model did not return a Code Documentation list")
        required_fields = {'relevant file', 'relevant line', 'doc placement', 'documentation'}

        def has_usable_fields(entry):
            if not isinstance(entry, dict) or not required_fields.issubset(entry):
                return False
            if not all(isinstance(entry[field], str) for field in ('relevant file', 'doc placement', 'documentation')):
                return False
            if not entry['relevant file'].strip() or not entry['doc placement'].strip():
                return False
            try:
                int(entry['relevant line'])
            except (TypeError, ValueError, OverflowError):
                return False
            return True

        if data['Code Documentation'] and not any(has_usable_fields(entry) for entry in data['Code Documentation']):
            raise FallbackEligibleError("The model did not return any documentation entries")
        return data

    def push_inline_docs(self, data):
        docs = []

        if not data['Code Documentation']:
            self.git_provider.publish_tool_failure(
                "add_docs", "No code documentation found to improve this PR."
            )
            return None

        for d in data['Code Documentation']:
            try:
                if get_verbosity_level() >= 2:
                    get_logger().info(f"add_docs: {d}")
                relevant_file = d['relevant file'].strip()
                relevant_line = int(d['relevant line'])  # absolute position
                documentation = d['documentation']
                doc_placement = d['doc placement'].strip()
                if documentation:
                    new_code_snippet = self.dedent_code(relevant_file, relevant_line, documentation, doc_placement,
                                                        add_original_line=True)
                    if new_code_snippet is None:
                        continue

                    fence = get_suggestion_fence(new_code_snippet)
                    body = (f"**Suggestion:** Proposed documentation\n{fence}suggestion\n"
                            + new_code_snippet + f"\n{fence}")
                    docs.append({'body': body, 'relevant_file': relevant_file,
                                 "relevant_lines_start": relevant_line,
                                 "relevant_lines_end": relevant_line})
            except Exception:
                if get_verbosity_level() >= 2:
                    get_logger().info(f"Could not parse code docs: {d}")

        if not docs:
            return None
        is_successful = self.git_provider.publish_code_suggestions(docs)
        if is_successful is True:
            return True
        if not is_successful:
            get_logger().info("Failed to publish code docs, trying to publish each docs separately")
            retry_results = []
            for doc_suggestion in docs:
                retry_results.append(self.git_provider.publish_code_suggestions([doc_suggestion]))
            if is_successful is False and all(result is False for result in retry_results):
                return False
            if any(result is True for result in retry_results):
                return True
        return None

    @staticmethod
    def _indent_reference_line(file_lines, index, step):
        """Return the closest non-blank line to index, searching in the direction of step."""
        while 0 <= index < len(file_lines):
            if file_lines[index].strip():
                return file_lines[index]
            index += step
        return None

    def dedent_code(self, relevant_file, relevant_lines_start, new_code_snippet, doc_placement="after",
                    add_original_line=False):
        try:  # Find the source line that supplies the indentation.
            self.diff_files = self.git_provider.diff_files or self.git_provider.get_diff_files()
            original_initial_line = None
            file_lines = []
            for file in self.diff_files:
                if file.filename.strip() == relevant_file:
                    if add_original_line and not getattr(file, "head_file_is_complete", True):
                        return None
                    file_lines = split_diff_lines(file.head_file) if file.head_file else []
                    if not 1 <= relevant_lines_start <= len(file_lines):
                        get_logger().warning(
                            "Could not dedent code snippet, relevant_lines_start is out of range",
                            artifact={'filename': file.filename,
                                      'relevant_lines_start': relevant_lines_start,
                                      'num_lines': len(file_lines)})
                        return None if add_original_line else new_code_snippet
                    original_initial_line = file_lines[relevant_lines_start - 1]
                    break
            if original_initial_line is not None:
                if doc_placement == 'after' or not original_initial_line.strip():
                    # A docstring placed after a "def" line belongs to the function body, so a
                    # following line supplies the indentation. A blank target line has no
                    # indentation of its own, and the next line reveals the block holding it.
                    # Skip blank lines either way: anchoring on one indents the docstring to
                    # column 0, which drops it out of the enclosing block.
                    line = self._indent_reference_line(file_lines, relevant_lines_start, 1)
                else:
                    line = original_initial_line
                if line is None:
                    line = self._indent_reference_line(file_lines, relevant_lines_start - 1, -1)
                if line is None:
                    return None if add_original_line else new_code_snippet
            else:
                return None if add_original_line else new_code_snippet
        except Exception as e:
            if get_verbosity_level() >= 2:
                get_logger().info(f"Could not dedent code snippet for file {relevant_file}, error: {e}")
            return None if add_original_line else new_code_snippet

        try:
            snippet_lines = new_code_snippet.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            first_line = next((item for item in snippet_lines if item.strip()), None)
            if first_line is None:
                return None
            target_prefix = line[:len(line) - len(line.lstrip())]
            snippet_prefix = first_line[:len(first_line) - len(first_line.lstrip())]
            aligned_lines = []
            for item in snippet_lines:
                if not item.strip():
                    aligned_lines.append("")
                elif item.startswith(snippet_prefix):
                    aligned_lines.append(target_prefix + item[len(snippet_prefix):])
                else:
                    return None
            aligned = "\n".join(aligned_lines)
            if add_original_line:
                if doc_placement == 'after':
                    return original_initial_line + "\n" + aligned
                return aligned.rstrip() + "\n" + original_initial_line
            return aligned
        except Exception:
            # Once an anchor exists, never publish documentation that could not be aligned.
            return None


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
