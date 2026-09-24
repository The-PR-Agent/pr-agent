import copy
import re
from functools import partial
from importlib.resources import files as package_files
from importlib.resources.abc import Traversable
from math import ceil, isfinite
from pathlib import Path, PurePosixPath

from jinja2 import Environment, StrictUndefined, select_autoescape
from litellm import token_counter

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import FallbackEligibleError, retry_with_fallback_models
from pr_agent.algo.token_budget import get_max_tokens
from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.utils import ModelType, load_yaml
from pr_agent.command_descriptions import COMMAND_DESCRIPTIONS
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

DOCS_SITE_URL = "https://docs.pr-agent.ai"
HELP_OUTPUT_TOKEN_RESERVE = 2_000
MESSAGE_FRAMING_TOKEN_ALLOWANCE = 16
REPLY_FRAMING_TOKEN_ALLOWANCE = 16
TRUNCATION_MARKER = "\n...(truncated)\n"
HELP_DOCS_RESOURCE = "_help_docs"
HELP_DOCS_UNAVAILABLE_MESSAGE = (
    "PR-Agent help documentation is unavailable. Please contact the service administrator."
)
HELP_DOCS_EXCLUDED_DIRECTORIES = {"finetuning_benchmark"}
HELP_DOCS_EXCLUDED_FILENAMES = {"compression_strategy.md"}
HELP_DOCS_PRIORITY_PATHS = (
    "/usage-guide/",
    "/tools/describe.md",
    "/tools/review.md",
    "/tools/improve.md",
    "/faq/",
)


def _is_help_doc_included(relative_path: PurePosixPath) -> bool:
    return (
        relative_path.suffix == ".md"
        and relative_path.name not in HELP_DOCS_EXCLUDED_FILENAMES
        and not any(part in HELP_DOCS_EXCLUDED_DIRECTORIES for part in relative_path.parts[:-1])
    )


def _get_help_docs_root() -> Traversable:
    try:
        packaged_docs = package_files("pr_agent").joinpath(HELP_DOCS_RESOURCE)
        if packaged_docs.is_dir():
            return packaged_docs
    except Exception:
        get_logger().opt(exception=True).debug("Unable to inspect packaged /help documentation")

    try:
        source_docs = Path(__file__).resolve().parents[2] / "docs" / "docs"
        if source_docs.is_dir():
            return source_docs
    except Exception as error:
        get_logger().opt(exception=True).error("Unable to inspect source-tree /help documentation")
        raise FileNotFoundError(
            "The /help documentation corpus is unavailable; unable to inspect the source checkout"
        ) from error

    raise FileNotFoundError(
        "The /help documentation corpus is unavailable; reinstall PR-Agent from a complete package"
    )


def _iter_help_docs(root: Traversable) -> list[tuple[PurePosixPath, Traversable]]:
    documents: list[tuple[PurePosixPath, Traversable]] = []

    def visit(directory: Traversable, relative_directory: PurePosixPath) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda child: child.name)
        except Exception:
            get_logger().opt(exception=True).error(
                f"Error while listing the documentation directory {directory}"
            )
            return

        for child in children:
            relative_path = relative_directory / child.name
            try:
                if child.is_dir():
                    if child.name not in HELP_DOCS_EXCLUDED_DIRECTORIES:
                        visit(child, relative_path)
                elif child.is_file() and _is_help_doc_included(relative_path):
                    documents.append((relative_path, child))
            except Exception:
                get_logger().opt(exception=True).error(
                    f"Error while inspecting the documentation path {child}"
                )

    visit(root, PurePosixPath())

    def sort_key(document: tuple[PurePosixPath, Traversable]) -> tuple[int, str]:
        relative_name = f"/{document[0].as_posix()}"
        priority_rank = 0 if relative_name == "/index.md" else 1
        if priority_rank and not any(priority in relative_name for priority in HELP_DOCS_PRIORITY_PATHS):
            priority_rank = 2
        return priority_rank, relative_name

    return sorted(documents, key=sort_key)


def _load_help_docs_prompt() -> tuple[str, set[str]]:
    docs_root = _get_help_docs_root()
    sections = []
    available_docs_files = set()
    for relative_path, document in _iter_help_docs(docs_root):
        try:
            content = document.read_text(encoding="utf-8").strip()
        except Exception:
            get_logger().opt(exception=True).error(
                f"Error while reading the documentation file {document}"
            )
            continue
        if not content:
            continue
        relative_file_path = relative_path.as_posix()
        sections.append(
            f"\n==file name==\n\n/{relative_file_path}\n\n"
            f"==file content==\n\n{content}\n=========\n\n"
        )
        available_docs_files.add(relative_file_path)

    if not sections:
        raise FileNotFoundError(f"No readable Markdown documentation was found for /help in {docs_root}")

    return "".join(sections).strip(), available_docs_files


class PRHelpMessage:
    def __init__(self, pr_url: str, args=None,
             ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler, return_as_string=False):
        self.git_provider = get_git_provider_with_context(pr_url)
        self.ai_handler = ai_handler()
        self.question_str = self.parse_args(args)
        self.return_as_string = return_as_string
        if self.question_str:
            self.vars = {
                "question": self.question_str,
                "snippets": "",
            }

    async def _prepare_prediction(self, model: str):
        variables = copy.deepcopy(self.vars)
        system_prompt, user_prompt, fitted_snippets = self._fit_prompts(variables, model)
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        self._model_visible_docs_files = self._get_fitted_docs_files(
            variables.get("snippets", ""),
            fitted_snippets,
            getattr(self, "_available_docs_files", set()),
        )
        return response

    @staticmethod
    def _render_prompts(variables):
        # These string templates produce plain-text model prompts, not HTML.
        environment = Environment(
            autoescape=select_autoescape(default_for_string=False),
            undefined=StrictUndefined,
        )
        system_prompt = environment.from_string(get_settings().pr_help_prompts.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_help_prompts.user).render(variables)
        return system_prompt, user_prompt

    @staticmethod
    def _coerce_output_token_limit(value) -> int:
        try:
            output_tokens = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return output_tokens if output_tokens > 0 else 0

    def _get_prompt_budget(self, model: str) -> int:
        output_tokens = 0
        get_output_token_reserve = getattr(self.ai_handler, "get_output_token_reserve", None)
        if callable(get_output_token_reserve):
            try:
                handler_output_tokens = get_output_token_reserve(model, HELP_OUTPUT_TOKEN_RESERVE)
            except Exception as e:
                get_logger().debug(f"Failed to resolve the output token reserve for {model}: {e}")
            else:
                if (
                    isinstance(handler_output_tokens, int)
                    and not isinstance(handler_output_tokens, bool)
                    and handler_output_tokens > 0
                ):
                    output_tokens = handler_output_tokens
        get_output_token_limit = getattr(self.ai_handler, "get_output_token_limit", None)
        if output_tokens <= 0 and callable(get_output_token_limit):
            try:
                handler_output_tokens = get_output_token_limit(model)
            except Exception as e:
                get_logger().debug(f"Failed to resolve the output token limit for {model}: {e}")
            else:
                if (
                    isinstance(handler_output_tokens, int)
                    and not isinstance(handler_output_tokens, bool)
                    and handler_output_tokens > 0
                ):
                    output_tokens = handler_output_tokens
        if output_tokens <= 0:
            raw_output_tokens = get_settings().config.get("max_output_tokens", 0)
            output_tokens = self._coerce_output_token_limit(raw_output_tokens)
        if output_tokens <= 0:
            output_tokens = HELP_OUTPUT_TOKEN_RESERVE
        return max(get_max_tokens(model, ignore_max_model_tokens=True) - output_tokens, 0)

    @staticmethod
    def _count_prompt_tokens(model: str, system_prompt: str, user_prompt: str) -> int:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            model_token_count = token_counter(model=model, messages=messages)
            if isinstance(model_token_count, int) and not isinstance(model_token_count, bool) and model_token_count > 0:
                return model_token_count
        except Exception as e:
            get_logger().debug(f"Model-aware token counting failed for {model}: {e}")

        get_logger().debug(f"Using a local token estimate for {model}")
        encoder = TokenEncoder.get_token_encoder(model)
        content_tokens = sum(
            len(encoder.encode(message["content"], disallowed_special=())) for message in messages
        )
        framing_tokens = MESSAGE_FRAMING_TOKEN_ALLOWANCE * len(messages) + REPLY_FRAMING_TOKEN_ALLOWANCE
        raw_factor = get_settings().get("config.model_token_count_estimate_factor", 0)
        try:
            extra_factor = float(raw_factor)
        except (TypeError, ValueError, OverflowError):
            extra_factor = 0
        if isinstance(raw_factor, bool) or not isfinite(extra_factor):
            extra_factor = 0
        multiplier = max(1.0, 1.0 + extra_factor)
        raw_estimate = content_tokens + framing_tokens
        try:
            estimated_tokens = raw_estimate * multiplier
            if not isfinite(estimated_tokens):
                raise ValueError("non-finite token estimate")
            return ceil(estimated_tokens)
        except (OverflowError, ValueError):
            get_logger().warning(
                f"model_token_count_estimate_factor is too large ({raw_factor!r}), using the estimate as is"
            )
            return raw_estimate

    def _normalize_request_prompts(self, model: str, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        normalize_request_prompts = getattr(self.ai_handler, "normalize_request_prompts", None)
        if not callable(normalize_request_prompts):
            return system_prompt, user_prompt
        try:
            normalized_prompts = normalize_request_prompts(model, system_prompt, user_prompt)
        except Exception as e:
            get_logger().debug(f"Failed to normalize prompts for {model}: {e}")
            return system_prompt, user_prompt
        if (
            isinstance(normalized_prompts, tuple)
            and len(normalized_prompts) == 2
            and all(isinstance(prompt, str) for prompt in normalized_prompts)
        ):
            return normalized_prompts
        get_logger().debug(f"Ignoring unusable prompt normalization result for {model}")
        return system_prompt, user_prompt

    def _fit_prompts(self, variables, model: str):
        prompt_budget = self._get_prompt_budget(model)
        raw_snippets = variables.get("snippets", "")

        def render(snippets):
            attempt_variables = copy.deepcopy(variables)
            attempt_variables["snippets"] = snippets
            rendered_prompts = self._render_prompts(attempt_variables)
            return self._normalize_request_prompts(model, *rendered_prompts)

        full_prompts = render(raw_snippets)
        if self._count_prompt_tokens(model, *full_prompts) <= prompt_budget:
            return full_prompts[0], full_prompts[1], raw_snippets

        empty_prompts = render("")
        if self._count_prompt_tokens(model, *empty_prompts) > prompt_budget:
            raise FallbackEligibleError(f"The /help prompt exceeds the token limit for {model} without documentation")

        marker_prompts = render(TRUNCATION_MARKER)
        if self._count_prompt_tokens(model, *marker_prompts) > prompt_budget:
            raise FallbackEligibleError(
                f"The /help prompt exceeds the token limit for {model} with a truncation marker"
            )

        keep_chars = max(len(raw_snippets) - 1, 0)
        while keep_chars > 0:
            candidate = raw_snippets[:keep_chars].rstrip() + TRUNCATION_MARKER
            candidate_prompts = render(candidate)
            candidate_tokens = self._count_prompt_tokens(model, *candidate_prompts)
            if candidate_tokens <= prompt_budget:
                get_logger().warning(
                    f"Documentation was clipped for /help to fit the {prompt_budget}-token input limit for {model}"
                )
                return candidate_prompts[0], candidate_prompts[1], candidate
            next_keep_chars = max(1, int(keep_chars * prompt_budget / candidate_tokens))
            keep_chars = min(keep_chars - 1, next_keep_chars)

        get_logger().warning(
            f"Documentation was clipped for /help to fit the {prompt_budget}-token input limit for {model}"
        )
        return marker_prompts[0], marker_prompts[1], TRUNCATION_MARKER

    @staticmethod
    def _get_fitted_docs_files(
        raw_snippets: str,
        fitted_snippets: str,
        available_docs_files: set[str],
    ) -> set[str]:
        if not raw_snippets or not fitted_snippets or not available_docs_files:
            return set()

        section_starts = []
        for file_name in available_docs_files:
            section_prefix = f"==file name==\n\n/{file_name}\n\n==file content==\n\n"
            section_start = raw_snippets.find(section_prefix)
            if section_start >= 0:
                section_starts.append((section_start, file_name))
        section_starts.sort()

        fitted_docs_files = set()
        for index, (section_start, file_name) in enumerate(section_starts):
            section_end = section_starts[index + 1][0] if index + 1 < len(section_starts) else len(raw_snippets)
            section = raw_snippets[section_start:section_end]
            delimiter_start = section.rfind("\n=========")
            if delimiter_start < 0:
                continue
            complete_document = section[:delimiter_start]
            if complete_document and complete_document in fitted_snippets:
                fitted_docs_files.add(file_name)
        return fitted_docs_files

    def parse_args(self, args):
        if args and len(args) > 0:
            question_str = " ".join(args)
        else:
            question_str = ""
        return question_str

    def format_markdown_header(self, header: str) -> str:
        try:
            # First, strip common characters from both ends
            cleaned = header.strip('# 💎\n')

            # Define all characters to be removed/replaced in a single pass
            replacements = {
                "'": '',
                "`": '',
                '(': '',
                ')': '',
                ',': '',
                '.': '',
                '?': '',
                '!': '',
                ' ': '-'
            }

            # Compile regex pattern for characters to remove
            pattern = re.compile('|'.join(map(re.escape, replacements.keys())))

            # Perform replacements in a single pass and convert to lowercase
            return pattern.sub(lambda m: replacements[m.group()], cleaned).lower()
        except Exception:
            get_logger().exception("Error while formatting markdown header", artifacts={'header': header})
            return ""

    def format_docs_url(self, file_name: str, header: str) -> str:
        relative_path = file_name.strip().lstrip('/').removesuffix('.md')
        if relative_path == 'index':
            relative_path = ''
        elif relative_path.endswith('/index'):
            relative_path = relative_path.removesuffix('index')
        elif relative_path:
            relative_path += '/'

        docs_url = f"{DOCS_SITE_URL}/{relative_path}"
        if str(header).strip():
            docs_url += f"#{self.format_markdown_header(header)}"
        return docs_url

    def _format_relevant_sources(self, relevant_sections, available_docs_files: set[str]) -> list[str]:
        if not isinstance(relevant_sections, list):
            return []

        source_urls = []
        for section in relevant_sections:
            if not isinstance(section, dict):
                continue

            file_name = section.get("file_name")
            if not isinstance(file_name, str):
                continue
            normalized_file_name = file_name
            if normalized_file_name.startswith("/"):
                normalized_file_name = normalized_file_name[1:]
            if normalized_file_name not in available_docs_files:
                get_logger().warning(f"Skipping a /help citation to a document that was not loaded: {file_name}")
                continue

            header = section.get("relevant_section_header_string", "")
            if not isinstance(header, str):
                header = ""
            source_urls.append(self.format_docs_url(normalized_file_name, header))
        return source_urls


    async def run(self):
        if self.question_str:
            try:
                self.vars["snippets"], self._available_docs_files = _load_help_docs_prompt()
                self._model_visible_docs_files = set()
            except FileNotFoundError:
                get_logger().exception("Unable to load the PR-Agent help documentation")
                if get_settings().config.publish_output:
                    try:
                        self.git_provider.publish_comment(HELP_DOCS_UNAVAILABLE_MESSAGE)
                    except Exception:
                        get_logger().exception("Unable to publish the help documentation failure message")
                raise

        try:
            if self.question_str:
                get_logger().info(f'Answering a PR question about the PR {self.git_provider.pr_url} ')

                # run the AI model
                response = await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
                response_yaml = load_yaml(response)
                if isinstance(response_yaml, str):
                    get_logger().warning(f"failing to parse response: {response_yaml}, publishing the response as is")
                    if get_settings().config.publish_output:
                        answer_str = f"### Question: \n{self.question_str}\n\n"
                        answer_str += "### Answer:\n\n"
                        answer_str += response_yaml
                        self.git_provider.publish_comment(answer_str)
                    return ""
                response_str = response_yaml.get('response')
                relevant_sections = response_yaml.get('relevant_sections')

                if not relevant_sections:
                    get_logger().info(f"Could not find relevant answer for the question: {self.question_str}")
                    if get_settings().config.publish_output:
                        answer_str = f"### Question: \n{self.question_str}\n\n"
                        answer_str += "### Answer:\n\n"
                        answer_str += (
                            "Could not find relevant information to answer the question. "
                            "Please provide more details and try again."
                        )
                        self.git_provider.publish_comment(answer_str)
                    return ""

                # prepare the answer
                answer_str = ""
                if response_str:
                    source_urls = self._format_relevant_sources(relevant_sections, self._model_visible_docs_files)
                    answer_str += f"### Question: \n{self.question_str}\n\n"
                    answer_str += f"### Answer:\n{response_str.strip()}\n\n"
                    if source_urls:
                        answer_str += "#### Relevant Sources:\n\n"
                    for docs_url in source_urls:
                        answer_str += f"> - {docs_url}\n"


                # publish the answer
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(answer_str)
                else:
                    get_logger().info(f"Answer:\n{answer_str}")
            else:
                supports_gfm_markdown = self.git_provider.is_supported("gfm_markdown")
                if not supports_gfm_markdown and not self.git_provider.supports_markdown_tables():
                    self.git_provider.publish_comment(
                        "The `Help` tool requires gfm markdown, which is not supported by your code platform.")
                    return

                get_logger().info('Getting PR Help Message...')
                relevant_configs = {'pr_help': dict(get_settings().get("pr_help", {})),
                                    'config': dict(get_settings().config)}
                get_logger().debug("Relevant configs", artifacts=relevant_configs)
                pr_comment = "## PR Agent Walkthrough 🤖\n\n"
                pr_comment += ("Welcome to the PR Agent, an AI-powered tool for automated pull request analysis, "
               "feedback, suggestions and more.")
                pr_comment += "\n\nHere is a list of tools you can use to interact with the PR Agent:\n"
                base_path = f"{DOCS_SITE_URL}/tools"

                tool_names = []
                tool_names.append(f"[DESCRIBE]({base_path}/describe/)")
                tool_names.append(f"[REVIEW]({base_path}/review/)")
                tool_names.append(f"[IMPROVE]({base_path}/improve/)")
                tool_names.append(f"[UPDATE CHANGELOG]({base_path}/update_changelog/)")
                tool_names.append(f"[ADD DOCS]({base_path}/add_docs/)")
                tool_names.append(f"[ASK]({base_path}/ask/)")
                tool_names.append(f"[GENERATE CUSTOM LABELS]({base_path}/generate_labels/)")

                descriptions = []
                descriptions.append(COMMAND_DESCRIPTIONS["describe"])
                descriptions.append(COMMAND_DESCRIPTIONS["review"])
                descriptions.append(COMMAND_DESCRIPTIONS["improve"])
                descriptions.append("Automatically updates the changelog")
                descriptions.append("Generates documentation to methods/functions/classes that changed in the PR")
                descriptions.append("Answering free-text questions about the PR")
                descriptions.append(
                    "Generates custom labels for the PR, based on specific guidelines defined by the user"
                )

                commands  =[]
                commands.append("`/describe`")
                commands.append("`/review`")
                commands.append("`/improve`")
                commands.append("`/update_changelog`")
                commands.append("`/add_docs`")
                commands.append("`/ask`")
                commands.append("`/generate_labels`")

                checkbox_list = []
                checkbox_list.append(" - [ ] Run <!-- /describe -->")
                checkbox_list.append(" - [ ] Run <!-- /review -->")
                checkbox_list.append(" - [ ] Run <!-- /improve -->")
                checkbox_list.append(" - [ ] Run <!-- /update_changelog -->")
                checkbox_list.append(" - [ ] Run <!-- /add_docs -->")
                checkbox_list.append("[*]")
                checkbox_list.append("[*]")
                checkbox_list.append("[*]")
                checkbox_list.append("[*]")

                if (supports_gfm_markdown and self.git_provider.supports_checkbox_commands()
                        and not get_settings().config.get('disable_checkboxes', False)):
                    pr_comment += (
                        "<table><tr align='left'><th align='left'>Tool</th>"
                        "<th align='left'>Description</th><th align='left'>Trigger Interactively :gem:</th></tr>"
                    )
                    for i in range(len(tool_names)):
                        pr_comment += (
                            f"\n<tr><td align='left'>\n\n<strong>{tool_names[i]}</strong></td>\n"
                            f"<td>{descriptions[i]}</td>\n<td>\n\n{checkbox_list[i]}\n</td></tr>"
                        )
                    pr_comment += "</table>\n\n"
                    pr_comment += (
                        "\n\n(1) Note that each tool can be "
                        "[triggered automatically]("
                        "https://docs.pr-agent.ai/usage-guide/automations_and_usage/#github-app-automatic-tools-when-a-new-pr-is-opened"
                        ") when a new PR is opened, or called manually by "
                        "[commenting on a PR]("
                        "https://docs.pr-agent.ai/usage-guide/automations_and_usage/#online-usage)."
                    )
                    pr_comment += (
                        "\n\n(2) Tools marked with [*] require additional parameters to be passed. "
                        "For example, to invoke the `/ask` tool, you need to comment on a PR: "
                        "`/ask \"<question content>\"`. See the relevant documentation for each tool for more details."
                    )
                elif not supports_gfm_markdown:
                    # only basic commands, in a plain Markdown table (e.g. BBDC)
                    pr_comment = generate_bbdc_table(tool_names[:4], descriptions[:4])
                else:
                    pr_comment += (
                        "<table><tr align='left'><th align='left'>Tool</th>"
                        "<th align='left'>Command</th><th align='left'>Description</th></tr>"
                    )
                    for i in range(len(tool_names)):
                        pr_comment += (
                            f"\n<tr><td align='left'>\n\n<strong>{tool_names[i]}</strong></td>"
                            f"<td>{commands[i]}</td><td>{descriptions[i]}</td></tr>"
                        )
                    pr_comment += "</table>\n\n"
                    pr_comment += (
                        "\n\nNote that each tool can be "
                        "[invoked automatically](https://docs.pr-agent.ai/usage-guide/automations_and_usage/) "
                        "when a new PR is opened, or called manually by "
                        "[commenting on a PR](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#online-usage)."
                    )

                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(pr_comment)
        except Exception as e:
            get_logger().exception(f"Error while running PRHelpMessage: {e}")
            if get_settings().config.get("propagate_tool_errors", False):
                raise
        return ""


def generate_bbdc_table(column_arr_1, column_arr_2):
    # Generating header row
    header_row = "| Tool  | Description | \n"

    # Generating separator row
    separator_row = "|--|--|\n"

    # Generating data rows
    data_rows = ""
    max_len = max(len(column_arr_1), len(column_arr_2))
    for i in range(max_len):
        col1 = column_arr_1[i] if i < len(column_arr_1) else ""
        col2 = column_arr_2[i] if i < len(column_arr_2) else ""
        data_rows += f"| {col1} | {col2} |\n"

    # Combine all parts to form the complete table
    markdown_table = header_row + separator_row + data_rows
    return markdown_table
