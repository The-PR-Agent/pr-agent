import copy
from functools import partial

from jinja2 import Environment, StrictUndefined
import math
import os
import re
from tempfile import TemporaryDirectory

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import clip_tokens, get_max_tokens, load_yaml, ModelType
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage


#Common code that can be called from similar tools:
def modify_answer_section(ai_response: str) -> str | None:
    # Gets the model's answer and relevant sources section, replacing the heading of the answer section with:
    # :bulb: Auto-generated documentation-based answer:
    """
    For example: The following input:

    ### Question: \nThe following general issue was asked by a user: Title: How does one request to re-review a PR? More Info: I cannot seem to find to do this.
    ### Answer:\nAccording to the documentation, one needs to invoke the command: /review
    #### Relevant Sources...

    Should become:

    ### :bulb: Auto-generated documentation-based answer:\n
    According to the documentation, one needs to invoke the command: /review
    #### Relevant Sources...
    """
    model_answer_and_relevant_sections_in_response \
        = extract_model_answer_and_relevant_sources(ai_response)
    if model_answer_and_relevant_sections_in_response is not None:
        cleaned_question_with_answer = "### :bulb: Auto-generated documentation-based answer:\n"
        cleaned_question_with_answer += model_answer_and_relevant_sections_in_response
        return cleaned_question_with_answer
    get_logger().warning(f"Either no answer section found, or that section is malformed: {ai_response}")
    return None

def extract_model_answer_and_relevant_sources(ai_response: str) -> str | None:
    # It is assumed that the input contains several sections with leading "### ",
    # where the answer is the last one of them having the format: "### Answer:\n"), since the model returns the answer
    # AFTER the user question. By splitting using the string: "### Answer:\n" and grabbing the last part,
    # the model answer is guaranteed to be in that last part, provided it is followed by a "#### Relevant Sources:\n\n".
    # (for more details, see here: https://github.com/Codium-ai/pr-agent-pro/blob/main/pr_agent/tools/pr_help_message.py#L173)
    """
    For example:
    ### Question: \nHow does one request to re-review a PR?\n\n
    ### Answer:\nAccording to the documentation, one needs to invoke the command: /review\n\n
    #### Relevant Sources:\n\n...

    The answer part is: "According to the documentation, one needs to invoke the command: /review\n\n"
    followed by "Relevant Sources:\n\n".
    """
    if "### Answer:\n" in ai_response:
        model_answer_and_relevant_sources_sections_in_response = ai_response.split("### Answer:\n")[-1]
        # Split such part by "Relevant Sources" section to contain only the model answer:
        if "#### Relevant Sources:\n\n" in model_answer_and_relevant_sources_sections_in_response:
            model_answer_section_in_response \
                = model_answer_and_relevant_sources_sections_in_response.split("#### Relevant Sources:\n\n")[0]
            get_logger().info(f"Found model answer: {model_answer_section_in_response}")
            return model_answer_and_relevant_sources_sections_in_response \
                if len(model_answer_section_in_response) > 0 else None
    get_logger().warning(f"Either no answer section found, or that section is malformed: {ai_response}")
    return None

def get_maximal_text_input_length_for_token_count_estimation():
    model = get_settings().config.model
    if 'claude-3-7-sonnet' in model.lower():
        return 900000 #Claude API for token estimation allows maximal text input of 900K chars
    return math.inf #Otherwise, no known limitation on input text just for token estimation

def return_document_headings(text: str, ext: str) -> str:
    try:
        lines = text.split('\n')
        headings = set()

        if not text or not re.search(r'[a-zA-Z]', text):
            get_logger().error(f"Empty or non text content found in text: {text}.")
            return ""

        if ext in ['.md', '.mdx']:
            # Extract Markdown headings (lines starting with #)
            headings = {line.strip() for line in lines if line.strip().startswith('#')}
        elif ext == '.rst':
            # Find indices of lines that have all same character:
            #Allowed characters according to list from: https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#sections
            section_chars = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')

            # Find potential section marker lines (underlines/overlines): They have to be the same character
            marker_lines = []
            for i, line in enumerate(lines):
                line = line.rstrip()
                if line and all(c == line[0] for c in line) and line[0] in section_chars:
                    marker_lines.append((i, len(line)))

            # Check for headings adjacent to marker lines (below + text must be in length equal or less)
            for idx, length in marker_lines:
                # Check if it's an underline (heading is above it)
                if idx > 0 and lines[idx - 1].rstrip() and len(lines[idx - 1].rstrip()) <= length:
                    headings.add(lines[idx - 1].rstrip())
        else:
            get_logger().error(f"Unsupported file extension: {ext}")
            return ""

        return '\n'.join(headings)
    except Exception as e:
        get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
        return ""

# Load documentation files to memory: full file path (as will be given as prompt) -> doc contents
def map_documentation_files_to_contents(base_path: str, doc_files: list[str], max_allowed_file_len=5000) -> dict[str, str]:
    try:
        returned_dict = {}
        for file in doc_files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    # Skip files with no text content
                    if not re.search(r'[a-zA-Z]', content):
                        continue
                    if len(content) > max_allowed_file_len:
                        get_logger().warning(f"File {file} length: {len(content)} exceeds limit: {max_allowed_file_len}, so it will be trimmed.")
                        content = content[:max_allowed_file_len]
                    file_path = str(file).replace(str(base_path), '')
                    returned_dict[file_path] = content.strip()
            except Exception as e:
                get_logger().warning(f"Error while reading the file {file}: {e}")
                continue
        if not returned_dict:
            get_logger().error("Couldn't find any usable documentation files. Returning empty dict.")
        return returned_dict
    except Exception as e:
        get_logger().exception(f"Unexpected exception thrown. Returning empty dict.")
        return {}

# Goes over files' contents, generating payload for prompt while decorating them with a header to mark where each file begins,
# as to help the LLM to give a better answer.
def aggregate_documentation_files_for_prompt_contents(file_path_to_contents: dict[str, str], return_just_headings=False) -> str:
    try:
        docs_prompt = ""
        for idx, file_path in enumerate(file_path_to_contents):
            file_contents = file_path_to_contents[file_path].strip()
            if not file_contents:
                get_logger().error(f"Got empty file contents for: {file_path}. Skipping this file.")
                continue
            if return_just_headings:
                file_headings = return_document_headings(file_contents, os.path.splitext(file_path)[-1]).strip()
                if file_headings:
                    docs_prompt += f"\n==file name==\n\n{file_path}\n\n==index==\n\n{idx}\n\n==file headings==\n\n{file_headings}\n=========\n\n"
                else:
                    get_logger().warning(f"No headers for: {file_path}. Will only use filename")
                    docs_prompt += f"\n==file name==\n\n{file_path}\n\n==index==\n\n{idx}\n\n"
            else:
                docs_prompt += f"\n==file name==\n\n{file_path}\n\n==file content==\n\n{file_contents}\n=========\n\n"
        return docs_prompt
    except Exception as e:
        get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
        return ""

def format_markdown_q_and_a_response(question_str: str, response_str: str, relevant_sections: list[dict[str, str]],
                                     supported_suffixes: list[str], base_url_prefix: str, base_url_suffix: str="") -> str:
    try:
        base_url_prefix = base_url_prefix.strip('/') #Sanitize base_url_prefix
        answer_str = ""
        answer_str += f"### Question: \n{question_str}\n\n"
        answer_str += f"### Answer:\n{response_str.strip()}\n\n"
        answer_str += f"#### Relevant Sources:\n\n"
        for section in relevant_sections:
            file = section.get('file_name').lstrip('/').strip() #Remove any '/' in the beginning, since some models do it anyway
            ext = [suffix for suffix in supported_suffixes if file.endswith(suffix)]
            if not ext:
                get_logger().warning(f"Unsupported file extension: {file}")
                continue
            if str(section['relevant_section_header_string']).strip():
                markdown_header = format_markdown_header(section['relevant_section_header_string'])
                if base_url_prefix:
                    answer_str += f"> - {base_url_prefix}/{file}{base_url_suffix}#{markdown_header}\n"
            else:
                answer_str += f"> - {base_url_prefix}/{file}{base_url_suffix}\n"
        return answer_str
    except Exception as e:
        get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
        return ""

def format_markdown_header(header: str) -> str:
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
        get_logger().exception(f"Error while formatting markdown header", artifacts={'header': header})
        return ""

def clean_markdown_content(content: str) -> str:
    """
    Remove hidden comments and unnecessary elements from markdown content to reduce size.

    Args:
        content: The original markdown content

    Returns:
        Cleaned markdown content
    """
    try:
        # Remove HTML comments
        content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

        # Remove frontmatter (YAML between --- or +++ delimiters)
        content = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL)
        content = re.sub(r'^\+\+\+\s*\n.*?\n\+\+\+\s*\n', '', content, flags=re.DOTALL)

        # Remove excessive blank lines (more than 2 consecutive)
        content = re.sub(r'\n{3,}', '\n\n', content)

        # Remove HTML tags that are often used for styling only
        content = re.sub(r'<div.*?>|</div>|<span.*?>|</span>', '', content, flags=re.DOTALL)

        # Remove image alt text which can be verbose
        content = re.sub(r'!\[(.*?)\]', '![]', content)

        # Remove images completely
        content = re.sub(r'!\[.*?\]\(.*?\)', '', content)

        # Remove simple HTML tags but preserve content between them
        content = re.sub(r'<(?!table|tr|td|th|thead|tbody)([a-zA-Z][a-zA-Z0-9]*)[^>]*>(.*?)</\1>',
                         r'\2', content, flags=re.DOTALL)
        return content.strip()
    except Exception as e:
        get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
        return ""

class PredictionPreparator:
    def __init__(self, ai_handler, vars, system_prompt, user_prompt):
        try:
            self.ai_handler = ai_handler
            variables = copy.deepcopy(vars)
            environment = Environment(undefined=StrictUndefined)
            self.system_prompt = environment.from_string(system_prompt).render(variables)
            self.user_prompt = environment.from_string(user_prompt).render(variables)
        except Exception as e:
            get_logger().exception(f"Caught exception during init. Setting ai_handler to None to prevent __call__.")
            self.ai_handler = None

    #Called by retry_with_fallback_models and therefore, on any failure must throw an exception:
    async def __call__(self, model: str) -> str:
        if not self.ai_handler:
            get_logger().error("ai handler not set. Cannot invoke model!")
            raise ValueError("PredictionPreparator not initialized")
        try:
            response, finish_reason = await self.ai_handler.chat_completion(
                model=model, temperature=get_settings().config.temperature, system=self.system_prompt, user=self.user_prompt)
            return response
        except Exception as e:
            get_logger().exception("Caught exception during prediction.", artifacts={'system': self.system_prompt, 'user': self.user_prompt})
            raise e


class PRHelpDocs(object):
    def __init__(self, ctx_url, ai_handler:partial[BaseAiHandler,] = LiteLLMAIHandler, args: tuple[str]=None, return_as_string: bool=False):
        try:
            self.ctx_url = ctx_url
            self.question = self._parse_args(args)
            self.return_as_string = return_as_string
            self.repo_url_given_explicitly = True
            self.repo_url = get_settings().get('PR_HELP_DOCS.REPO_URL', '')
            self.repo_desired_branch = get_settings().get('PR_HELP_DOCS.REPO_DEFAULT_BRANCH', 'main') #Ignored if self.repo_url is empty
            self.include_root_readme_file = not(get_settings()['PR_HELP_DOCS.EXCLUDE_ROOT_README'])
            self.supported_doc_exts = get_settings()['PR_HELP_DOCS.SUPPORTED_DOC_EXTS']
            self.docs_path = get_settings()['PR_HELP_DOCS.DOCS_PATH']

            retrieved_settings = [self.include_root_readme_file, self.supported_doc_exts, self.docs_path]
            if any([setting is None for setting in retrieved_settings]):
                raise Exception(f"One of the settings is invalid: {retrieved_settings}")

            self.git_provider = get_git_provider_with_context(ctx_url)
            if not self.git_provider:
                raise Exception(f"No git provider found at {ctx_url}")
            if not self.repo_url:
                self.repo_url_given_explicitly = False
                get_logger().debug(f"No explicit repo url provided, deducing it from type: {self.git_provider.__class__.__name__} "
                                  f"context url: {self.ctx_url}")
                self.repo_url = self.git_provider.get_git_repo_url(self.ctx_url)
                if not self.repo_url:
                    raise Exception(f"Unable to deduce repo url from type: {self.git_provider.__class__.__name__} url: {self.ctx_url}")
                get_logger().debug(f"deduced repo url: {self.repo_url}")
                self.repo_desired_branch = None #Inferred from the repo provider.

            self.ai_handler = ai_handler()
            self.vars = {
                "docs_url": self.repo_url,
                "question": self.question,
                "snippets": "",
            }
            self.token_handler = TokenHandler(None,
                                                  self.vars,
                                                  get_settings().pr_help_docs_prompts.system,
                                                  get_settings().pr_help_docs_prompts.user)
        except Exception as e:
            get_logger().exception(f"Caught exception during init. Setting self.question to None to prevent run() to do anything.")
            self.question = None

    @staticmethod
    def _parse_args(args) -> str | None:
        if not args:
            return None
        return " ".join(args).strip()

    async def run(self):
        if not self.question:
            get_logger().warning('No question provided. Will do nothing.')
            return None

        try:
            # Clone the repository and gather relevant documentation files.
            docs_filepath_to_contents = self._gen_filenames_to_contents_map_from_repo()

            #Generate prompt for the AI model. This will be the full text of all the documentation files combined.
            docs_prompt = aggregate_documentation_files_for_prompt_contents(docs_filepath_to_contents)
            if not docs_filepath_to_contents or not docs_prompt:
                get_logger().warning(f"Could not find any usable documentation. Returning with no result...")
                return None
            docs_prompt_to_send_to_model = docs_prompt

            # Estimate how many tokens will be needed.
            # In case the expected number of tokens exceeds LLM limits, retry with just headings, asking the LLM to rank according to relevance to the question.
            # Based on returned ranking, rerun but sort the documents accordingly, this time, trim in case of exceeding limit.

            #First, check if the text is not too long to even query the LLM provider:
            max_allowed_txt_input = get_maximal_text_input_length_for_token_count_estimation()
            invoke_llm_just_with_headings = self._trim_docs_input(docs_prompt_to_send_to_model, max_allowed_txt_input,
                                                                  only_return_if_trim_needed=True)
            if invoke_llm_just_with_headings:
                #Entire docs is too long. Rank and return according to relevance.
                docs_prompt_to_send_to_model = await self._rank_docs_and_return_them_as_prompt(docs_filepath_to_contents,
                                                                                         max_allowed_txt_input)

            if not docs_prompt_to_send_to_model:
                get_logger().error("Failed to generate docs prompt for model. Returning with no result...")
                return
            # At this point, either all original documents be used (if their total length doesn't exceed limits), or only those selected.
            self.vars['snippets'] = docs_prompt_to_send_to_model.strip()
            # Run the AI model and extract sections from its response
            response = await retry_with_fallback_models(PredictionPreparator(self.ai_handler, self.vars,
                                                                             get_settings().pr_help_docs_prompts.system,
                                                                             get_settings().pr_help_docs_prompts.user),
                                                        model_type=ModelType.REGULAR)
            response_yaml = load_yaml(response)
            if not response_yaml:
                get_logger().error("Failed to parse the AI response.", artifacts={'response': response})
                return
            response_str = response_yaml.get('response')
            relevant_sections = response_yaml.get('relevant_sections')
            if not response_str or not relevant_sections:
                get_logger().error("Failed to extract response/relevant sections.",
                                       artifacts={'raw_response': response, 'response_str': response_str, 'relevant_sections': relevant_sections})
                return
            if int(response_yaml.get('question_is_relevant', '1')) == 0:
                get_logger().warning(f"Question is not relevant. Returning without an answer...",
                                         artifacts={'raw_response': response})
                return

            # Format the response as markdown
            answer_str = self._format_model_answer(response_str, relevant_sections)
            if self.return_as_string: #Skip publishing
                return answer_str
            #Otherwise, publish the answer if answer is non empty and publish is not turned off:
            if answer_str and get_settings().config.publish_output:
                self.git_provider.publish_comment(answer_str)
            else:
                get_logger().info("Answer:", artifacts={'answer_str': answer_str})
            return answer_str
        except Exception as e:
            get_logger().exception('failed to provide answer to given user question as a result of a thrown exception (see above)')

    def _find_all_document_files_matching_exts(self, abs_docs_path: str, ignore_readme=False, max_allowed_files=5000) -> list[str]:
        try:
            matching_files = []

            # Ensure extensions don't have leading dots and are lowercase
            dotless_extensions = [ext.lower().lstrip('.') for ext in self.supported_doc_exts]

            # Walk through directory and subdirectories
            file_cntr = 0
            for root, _, files in os.walk(abs_docs_path):
                for file in files:
                    if ignore_readme and root == abs_docs_path and file.lower() in [f"readme.{ext}" for ext in dotless_extensions]:
                        continue
                    # Check if file has one of the specified extensions
                    if any(file.lower().endswith(f'.{ext}') for ext in dotless_extensions):
                        file_cntr+=1
                        matching_files.append(os.path.join(root, file))
                        if file_cntr >= max_allowed_files:
                            get_logger().warning(f"Found at least {max_allowed_files} files in {abs_docs_path}, skipping the rest.")
                            return matching_files
            return matching_files
        except Exception as e:
            get_logger().exception(f"Unexpected exception thrown. Returning empty list.")
            return []

    def _gen_filenames_to_contents_map_from_repo(self) -> dict[str, str]:
        try:
            with TemporaryDirectory() as tmp_dir:
                get_logger().debug(f"About to clone repository: {self.repo_url} to temporary directory: {tmp_dir}...")
                returned_cloned_repo_root = self.git_provider.clone(self.repo_url, tmp_dir, remove_dest_folder=False)
                if not returned_cloned_repo_root:
                    raise Exception(f"Failed to clone {self.repo_url} to {tmp_dir}")

                get_logger().debug(f"About to gather relevant documentation files...")
                doc_files = []
                if self.include_root_readme_file:
                    for root, _, files in os.walk(returned_cloned_repo_root.path):
                        # Only look at files in the root directory, not subdirectories
                        if root == returned_cloned_repo_root.path:
                            for file in files:
                                if file.lower().startswith("readme."):
                                    doc_files.append(os.path.join(root, file))
                abs_docs_path = os.path.join(returned_cloned_repo_root.path, self.docs_path)
                if os.path.exists(abs_docs_path):
                    doc_files.extend(self._find_all_document_files_matching_exts(abs_docs_path,
                                                                                 ignore_readme=(self.docs_path=='.')))
                    if not doc_files:
                        get_logger().warning(f"No documentation files found matching file extensions: "
                                             f"{self.supported_doc_exts} under repo: {self.repo_url} "
                                             f"path: {self.docs_path}. Returning empty dict.")
                        return {}

                get_logger().info(f'For context {self.ctx_url} and repo: {self.repo_url}'
                                  f' will be using the following documentation files: ',
                                  artifacts={'doc_files': doc_files})

                return map_documentation_files_to_contents(returned_cloned_repo_root.path, doc_files)
        except Exception as e:
            get_logger().exception(f"Unexpected exception thrown. Returning empty dict.")
            return {}

    def _trim_docs_input(self, docs_input: str, max_allowed_txt_input: int, only_return_if_trim_needed=False) -> bool|str:
        try:
            if len(docs_input) >= max_allowed_txt_input:
                get_logger().warning(
                    f"Text length: {len(docs_input)} exceeds the current returned limit of {max_allowed_txt_input} just for token count estimation. Trimming the text...")
                if only_return_if_trim_needed:
                    return True
                docs_input = docs_input[:max_allowed_txt_input]
            # Then, count the tokens in the prompt. If the count exceeds the limit, trim the text.
            token_count = self.token_handler.count_tokens(docs_input, force_accurate=True)
            get_logger().debug(f"Estimated token count of documentation to send to model: {token_count}")
            model = get_settings().config.model
            if model in MAX_TOKENS:
                max_tokens_full = MAX_TOKENS[
                    model]  # note - here we take the actual max tokens, without any reductions. we do aim to get the full documentation website in the prompt
            else:
                max_tokens_full = get_max_tokens(model)
            delta_output = 5000  # Elbow room to reduce chance of exceeding token limit or model paying less attention to prompt guidelines.
            if token_count > max_tokens_full - delta_output:
                if only_return_if_trim_needed:
                    return True
                docs_input = clean_markdown_content(
                    docs_input)  # Reduce unnecessary text/images/etc.
                get_logger().info(
                    f"Token count {token_count} exceeds the limit {max_tokens_full - delta_output}. Attempting to clip text to fit within the limit...")
                docs_input = clip_tokens(docs_input, max_tokens_full - delta_output,
                                                           num_input_tokens=token_count)
            if only_return_if_trim_needed:
                return False
            return docs_input
        except Exception as e:
            # Unexpected exception. Rethrowing it since:
            # 1. This is an internal function.
            # 2. An empty str/False result is a valid one - would require now checking also for None.
            get_logger().exception(f"Unexpected exception thrown. Rethrowing it...")
            raise e

    async def _rank_docs_and_return_them_as_prompt(self, docs_filepath_to_contents: dict[str, str], max_allowed_txt_input: int) -> str:
        try:
            #Return just file name and their headings (if exist):
            docs_prompt_to_send_to_model = (
                aggregate_documentation_files_for_prompt_contents(docs_filepath_to_contents,
                                                                  return_just_headings=True))
            # Verify list of headings does not exceed limits - trim it if it does.
            docs_prompt_to_send_to_model = self._trim_docs_input(docs_prompt_to_send_to_model, max_allowed_txt_input,
                                                                 only_return_if_trim_needed=False)
            if not docs_prompt_to_send_to_model:
                get_logger().error("_trim_docs_input returned an empty result.")
                return ""

            self.vars['snippets'] = docs_prompt_to_send_to_model.strip()
            # Run the AI model and extract sections from its response
            response = await retry_with_fallback_models(PredictionPreparator(self.ai_handler, self.vars,
                                                                             get_settings().pr_help_docs_headings_prompts.system,
                                                                             get_settings().pr_help_docs_headings_prompts.user),
                                                        model_type=ModelType.REGULAR)
            response_yaml = load_yaml(response)
            if not response_yaml:
                get_logger().error("Failed to parse the AI response.", artifacts={'response': response})
                return ""
            # else: Sanitize the output so that the file names match 1:1 dictionary keys. Do this via the file index and not its name, which may be altered by the model.
            valid_indices = [int(entry['idx']) for entry in response_yaml.get('relevant_files_ranking')
                             if int(entry['idx']) >= 0 and int(entry['idx']) < len(docs_filepath_to_contents)]
            valid_file_paths = [list(docs_filepath_to_contents.keys())[idx] for idx in valid_indices]
            selected_docs_dict = {file_path: docs_filepath_to_contents[file_path] for file_path in valid_file_paths}
            docs_prompt = aggregate_documentation_files_for_prompt_contents(selected_docs_dict)
            docs_prompt_to_send_to_model = docs_prompt

            # Check if the updated list of documents does not exceed limits and trim if it does:
            docs_prompt_to_send_to_model = self._trim_docs_input(docs_prompt_to_send_to_model, max_allowed_txt_input,
                                                                 only_return_if_trim_needed=False)
            if not docs_prompt_to_send_to_model:
                get_logger().error("_trim_docs_input returned an empty result.")
                return ""
            return docs_prompt_to_send_to_model
        except Exception as e:
            get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
            return ""

    def _format_model_answer(self, response_str: str, relevant_sections: list[dict[str, str]]) -> str:
        try:
            canonical_url_prefix, canonical_url_suffix = (
                self.git_provider.get_canonical_url_parts(repo_git_url=self.repo_url if self.repo_url_given_explicitly else None,
                                                          desired_branch=self.repo_desired_branch))
            answer_str = format_markdown_q_and_a_response(self.question, response_str, relevant_sections,
                                                          self.supported_doc_exts, canonical_url_prefix, canonical_url_suffix)
            if answer_str:
                #Remove the question phrase and replace with light bulb and a heading mentioning this is an automated answer:
                answer_str = modify_answer_section(answer_str)
            #In case the response should not be published and returned as string, stop here:
            if answer_str and self.return_as_string:
                get_logger().info(f"Chat help docs answer", artifacts={'answer_str': answer_str})
                return answer_str
            if not answer_str:
                get_logger().info(f"No answer found")
                return ""
            if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_help_docs.enable_help_text:
                answer_str += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
                answer_str += HelpMessage.get_help_docs_usage_guide()
                answer_str += "\n</details>\n"
            return answer_str
        except Exception as e:
            get_logger().exception(f"Unexpected exception thrown. Returning empty result.")
            return ""
