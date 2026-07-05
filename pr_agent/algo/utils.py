from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, List, Optional, Tuple

import html2text
import requests
import yaml
from starlette_context import context

from pr_agent.algo import MAX_TOKENS

# Markdown rendering, output repair, and shared types moved to sibling modules;
# re-exported here so existing `from pr_agent.algo.utils import ...` keeps working.
from pr_agent.algo.markdown_render import (  # noqa: F401
    convert_to_markdown_v2,
    emphasize_header,
    extract_relevant_lines_str,
    format_todo_item,
    format_todo_items,
    is_value_no,
    parse_code_suggestion,
    process_can_be_split,
    replace_code_tags,
    ticket_markdown_logic,
)
from pr_agent.algo.output_repair import (  # noqa: F401
    fix_json_escape_char,  # noqa: F401
    load_yaml,
    try_fix_json,
    try_fix_yaml,
)
from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.types import (  # noqa: F401
    FilePatchInfo,
    ModelType,
    PRDescriptionHeader,
    PRReviewHeader,
    Range,
    ReasoningEffort,
    TodoItem,
)
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger


def get_model(model_type: str = "model_weak") -> str:
    if model_type == "model_weak" and get_settings().get("config.model_weak"):
        return get_settings().config.model_weak
    elif model_type == "model_reasoning" and get_settings().get("config.model_reasoning"):
        return get_settings().config.model_reasoning
    return get_settings().config.model


def get_setting(key: str) -> Any:
    try:
        key = key.upper()
        return context.get("settings", global_settings).get(key, global_settings.get(key, None))
    except Exception:
        return global_settings.get(key, None)


def unique_strings(input_list: List[str]) -> List[str]:
    if not input_list or not isinstance(input_list, list):
        return input_list
    seen = set()
    unique_list = []
    for item in input_list:
        if item not in seen:
            unique_list.append(item)
            seen.add(item)
    return unique_list


def convert_str_to_datetime(date_str):
    """
    Convert a string representation of a date and time into a datetime object.

    Args:
        date_str (str): A string representation of a date and time in the format '%a, %d %b %Y %H:%M:%S %Z'

    Returns:
        datetime: A datetime object representing the input date and time.

    Example:
        >>> convert_str_to_datetime('Mon, 01 Jan 2022 12:00:00 UTC')
        datetime.datetime(2022, 1, 1, 12, 0, 0)
    """
    datetime_format = '%a, %d %b %Y %H:%M:%S %Z'
    return datetime.strptime(date_str, datetime_format)


def load_large_diff(filename, new_file_content_str: str, original_file_content_str: str, show_warning: bool = True) -> str:
    """
    Generate a patch for a modified file by comparing the original content of the file with the new content provided as
    input.
    """
    if not original_file_content_str and not new_file_content_str:
        return ""

    try:
        original_file_content_str = (original_file_content_str or "").rstrip() + "\n"
        new_file_content_str = (new_file_content_str or "").rstrip() + "\n"
        diff = difflib.unified_diff(original_file_content_str.splitlines(keepends=True),
                                    new_file_content_str.splitlines(keepends=True))
        if get_settings().config.verbosity_level >= 2 and show_warning:
            get_logger().info(f"File was modified, but no patch was found. Manually creating patch: {filename}.")
        patch = ''.join(diff)
        return patch
    except Exception as e:
        get_logger().exception(f"Failed to generate patch for file: {filename}")
        return ""


def update_settings_from_args(args: List[str]) -> List[str]:
    """
    Update the settings of the Dynaconf object based on the arguments passed to the function.

    Args:
        args: A list of arguments passed to the function.
        Example args: ['--pr_code_suggestions.extra_instructions="be funny',
                  '--pr_code_suggestions.num_code_suggestions=3']

    Returns:
        None

    Raises:
        ValueError: If the argument is not in the correct format.

    """
    other_args = []
    if args:
        for arg in args:
            arg = arg.strip()
            if arg.startswith('--'):
                arg = arg.strip('-').strip()
                vals = arg.split('=', 1)
                if len(vals) != 2:
                    if len(vals) > 2:  # --extended is a valid argument
                        get_logger().error(f'Invalid argument format: {arg}')
                    other_args.append(arg)
                    continue
                key, value = _fix_key_value(*vals)
                get_settings().set(key, value)
                get_logger().info(f'Updated setting {key} to: "{value}"')
            else:
                other_args.append(arg)
    return other_args


def _fix_key_value(key: str, value: str):
    key = key.strip().upper()
    value = value.strip()
    try:
        value = yaml.safe_load(value)
    except Exception as e:
        get_logger().debug(f"Failed to parse YAML for config override {key}={value}", exc_info=e)
    return key, value


def set_custom_labels(variables, git_provider=None):
    if not get_settings().config.enable_custom_labels:
        return

    labels = get_settings().get('custom_labels', {})
    if not labels:
        # set default labels
        labels = ['Bug fix', 'Tests', 'Bug fix with tests', 'Enhancement', 'Documentation', 'Other']
        labels_list = "\n      - ".join(labels) if labels else ""
        labels_list = f"      - {labels_list}" if labels_list else ""
        variables["custom_labels"] = labels_list
        return

    # Set custom labels
    variables["custom_labels_class"] = "class Label(str, Enum):"
    counter = 0
    labels_minimal_to_labels_dict = {}
    for k, v in labels.items():
        description = "'" + v['description'].strip('\n').replace('\n', '\\n') + "'"
        # variables["custom_labels_class"] += f"\n    {k.lower().replace(' ', '_')} = '{k}' # {description}"
        variables["custom_labels_class"] += f"\n    {k.lower().replace(' ', '_')} = {description}"
        labels_minimal_to_labels_dict[k.lower().replace(' ', '_')] = k
        counter += 1
    variables["labels_minimal_to_labels_dict"] = labels_minimal_to_labels_dict

def get_user_labels(current_labels: List[str] = None):
    """
    Only keep labels that has been added by the user
    """
    try:
        enable_custom_labels = get_settings().config.get('enable_custom_labels', False)
        custom_labels = get_settings().get('custom_labels', [])
        if current_labels is None:
            current_labels = []
        user_labels = []
        for label in current_labels:
            if label.lower() in ['bug fix', 'tests', 'enhancement', 'documentation', 'other']:
                continue
            if enable_custom_labels:
                if label in custom_labels:
                    continue
            user_labels.append(label)
        if user_labels:
            get_logger().debug(f"Keeping user labels: {user_labels}")
    except Exception as e:
        get_logger().exception(f"Failed to get user labels: {e}")
        return current_labels
    return user_labels


def _get_litellm_max_input_tokens(model: str) -> Optional[int]:
    """Look up a model's context window from litellm's model metadata, or None if unknown."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        max_input = info.get("max_input_tokens") or info.get("max_tokens")
        if max_input and int(max_input) > 0:
            return int(max_input)
    except Exception:
        return None
    return None


def get_max_tokens(model):
    """
    Get the maximum number of tokens allowed for a model.
    logic:
    (1) If the model is in './pr_agent/algo/__init__.py', use the value from there.
    (2) else, if the user defined explicitly 'config.custom_model_max_tokens', use that.
    (3) else, fall back to litellm's model metadata, so newly released models work
        without waiting for a MAX_TOKENS entry.

    For all cases, we further limit the number of tokens to 'config.max_model_tokens' if it is set.
    This aims to improve the algorithmic quality, as the AI model degrades in performance when the input is too long.
    """
    settings = get_settings()
    if model in MAX_TOKENS:
        max_tokens_model = MAX_TOKENS[model]
    elif settings.config.custom_model_max_tokens > 0:
        max_tokens_model = settings.config.custom_model_max_tokens
    elif (litellm_max_tokens := _get_litellm_max_input_tokens(model)):
        get_logger().info(f"Model {model} is not in MAX_TOKENS; using max_input_tokens={litellm_max_tokens} "
                          f"from litellm model metadata")
        max_tokens_model = litellm_max_tokens
    else:
        get_logger().error(f"Model {model} is not defined in MAX_TOKENS in ./pr_agent/algo/__init__.py, "
                           f"no custom_model_max_tokens is set, and litellm has no metadata for it")
        raise Exception(f"Ensure {model} is defined in MAX_TOKENS in ./pr_agent/algo/__init__.py, or set a "
                        f"positive value for it in config.custom_model_max_tokens")

    if settings.config.max_model_tokens and settings.config.max_model_tokens > 0:
        max_tokens_model = min(settings.config.max_model_tokens, max_tokens_model)
    return max_tokens_model


def clip_tokens(text: str, max_tokens: int, add_three_dots=True, num_input_tokens=None, delete_last_line=False) -> str:
    """
    Clip the number of tokens in a string to a maximum number of tokens.

    This function limits text to a specified token count by calculating the approximate
    character-to-token ratio and truncating the text accordingly. A safety factor of 0.9
    (10% reduction) is applied to ensure the result stays within the token limit.

    Args:
        text (str): The string to clip. If empty or None, returns the input unchanged.
        max_tokens (int): The maximum number of tokens allowed in the string.
                         If negative, returns an empty string.
        add_three_dots (bool, optional): Whether to add "\\n...(truncated)" at the end
                                       of the clipped text to indicate truncation.
                                       Defaults to True.
        num_input_tokens (int, optional): Pre-computed number of tokens in the input text.
                                        If provided, skips token encoding step for efficiency.
                                        If None, tokens will be counted using TokenEncoder.
                                        Defaults to None.
        delete_last_line (bool, optional): Whether to remove the last line from the
                                         clipped content before adding truncation indicator.
                                         Useful for ensuring clean breaks at line boundaries.
                                         Defaults to False.

    Returns:
        str: The clipped string. Returns original text if:
             - Text is empty/None
             - Token count is within limit
             - An error occurs during processing

             Returns empty string if max_tokens <= 0.

    Examples:
        Basic usage:
        >>> text = "This is a sample text that might be too long"
        >>> result = clip_tokens(text, max_tokens=10)
        >>> print(result)
        This is a sample...
        (truncated)

        Without truncation indicator:
        >>> result = clip_tokens(text, max_tokens=10, add_three_dots=False)
        >>> print(result)
        This is a sample

        With pre-computed token count:
        >>> result = clip_tokens(text, max_tokens=5, num_input_tokens=15)
        >>> print(result)
        This...
        (truncated)

        With line deletion:
        >>> multiline_text = "Line 1\\nLine 2\\nLine 3"
        >>> result = clip_tokens(multiline_text, max_tokens=3, delete_last_line=True)
        >>> print(result)
        Line 1
        Line 2
        ...
        (truncated)

    Notes:
        The function uses a safety factor of 0.9 (10% reduction) to ensure the
        result stays within the token limit, as character-to-token ratios can vary.
        If token encoding fails, the original text is returned with a warning logged.
    """
    if not text:
        return text

    try:
        if num_input_tokens is None:
            encoder = TokenEncoder.get_token_encoder()
            num_input_tokens = len(encoder.encode(text))
        if num_input_tokens <= max_tokens:
            return text
        if max_tokens < 0:
            return ""

        # calculate the number of characters to keep
        num_chars = len(text)
        chars_per_token = num_chars / num_input_tokens
        factor = 0.9  # reduce by 10% to be safe
        num_output_chars = int(factor * chars_per_token * max_tokens)

        # clip the text
        if num_output_chars > 0:
            clipped_text = text[:num_output_chars]
            if delete_last_line:
                clipped_text = clipped_text.rsplit('\n', 1)[0]
            if add_three_dots:
                clipped_text += "\n...(truncated)"
        else: # if the text is empty
            clipped_text =  ""

        return clipped_text
    except Exception as e:
        get_logger().warning(f"Failed to clip tokens: {e}")
        return text

def find_line_number_of_relevant_line_in_file(diff_files: List[FilePatchInfo],
                                              relevant_file: str,
                                              relevant_line_in_file: str,
                                              absolute_position: int = None) -> Tuple[int, int]:
    position = -1
    if absolute_position is None:
        absolute_position = -1
    re_hunk_header = re.compile(
        r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")

    if not diff_files:
        return position, absolute_position

    for file in diff_files:
        if file.filename and (file.filename.strip() == relevant_file):
            patch = file.patch
            patch_lines = patch.splitlines()
            delta = 0
            start1, size1, start2, size2 = 0, 0, 0, 0
            if absolute_position != -1: # matching absolute to relative
                for i, line in enumerate(patch_lines):
                    # new hunk
                    if line.startswith('@@'):
                        delta = 0
                        match = re_hunk_header.match(line)
                        start1, size1, start2, size2 = map(int, match.groups()[:4])
                    elif not line.startswith('-'):
                        delta += 1

                    #
                    absolute_position_curr = start2 + delta - 1

                    if absolute_position_curr == absolute_position:
                        position = i
                        break
            else:
                # try to find the line in the patch using difflib, with some margin of error
                matches_difflib: list[str | Any] = difflib.get_close_matches(relevant_line_in_file,
                                                                             patch_lines, n=3, cutoff=0.93)
                if len(matches_difflib) == 1 and matches_difflib[0].startswith('+'):
                    relevant_line_in_file = matches_difflib[0]


                for i, line in enumerate(patch_lines):
                    if line.startswith('@@'):
                        delta = 0
                        match = re_hunk_header.match(line)
                        start1, size1, start2, size2 = map(int, match.groups()[:4])
                    elif not line.startswith('-'):
                        delta += 1

                    if relevant_line_in_file in line and line[0] != '-':
                        position = i
                        absolute_position = start2 + delta - 1
                        break

                if position == -1 and relevant_line_in_file[0] == '+':
                    no_plus_line = relevant_line_in_file[1:].lstrip()
                    for i, line in enumerate(patch_lines):
                        if line.startswith('@@'):
                            delta = 0
                            match = re_hunk_header.match(line)
                            start1, size1, start2, size2 = map(int, match.groups()[:4])
                        elif not line.startswith('-'):
                            delta += 1

                        if no_plus_line in line and line[0] != '-':
                            # The model might add a '+' to the beginning of the relevant_line_in_file even if originally
                            # it's a context line
                            position = i
                            absolute_position = start2 + delta - 1
                            break
    return position, absolute_position

def get_rate_limit_status(github_token) -> dict:
    GITHUB_API_URL = get_settings(use_context=False).get("GITHUB.BASE_URL", "https://api.github.com").rstrip("/")  # "https://api.github.com"
    # GITHUB_API_URL = "https://api.github.com"
    RATE_LIMIT_URL = f"{GITHUB_API_URL}/rate_limit"
    HEADERS = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {github_token}"
    }

    try:
        response = requests.get(RATE_LIMIT_URL, headers=HEADERS, timeout=10)
        rate_limit_info = response.json()
        if rate_limit_info.get('message') == 'Rate limiting is not enabled.':  # for github enterprise
            return {'resources': {}}
        response.raise_for_status()  # Check for HTTP errors
    except:  # retry
        time.sleep(0.1)
        response = requests.get(RATE_LIMIT_URL, headers=HEADERS, timeout=10)
        return response.json()
    return rate_limit_info


def validate_rate_limit_github(github_token, installation_id=None, threshold=0.1) -> bool:
    try:
        rate_limit_status = get_rate_limit_status(github_token)
        if installation_id:
            get_logger().debug(f"installation_id: {installation_id}, Rate limit status: {rate_limit_status['rate']}")
    # validate that the rate limit is not exceeded
        # validate that the rate limit is not exceeded
        for key, value in rate_limit_status['resources'].items():
            if value['remaining'] < value['limit'] * threshold:
                get_logger().error(f"key: {key}, value: {value}")
                return False
        return True
    except Exception as e:
        get_logger().error(f"Error in rate limit {e}",
                           artifact={"traceback": traceback.format_exc()})
        return True


def validate_and_await_rate_limit(github_token):
    try:
        rate_limit_status = get_rate_limit_status(github_token)
        # validate that the rate limit is not exceeded
        for key, value in rate_limit_status['resources'].items():
            if value['remaining'] < value['limit'] // 80:
                get_logger().error(f"key: {key}, value: {value}")
                sleep_time_sec = value['reset'] - datetime.now().timestamp()
                sleep_time_hour = sleep_time_sec / 3600.0
                get_logger().error(f"Rate limit exceeded. Sleeping for {sleep_time_hour} hours")
                if sleep_time_sec > 0:
                    time.sleep(sleep_time_sec + 1)
                rate_limit_status = get_rate_limit_status(github_token)
        return rate_limit_status
    except:
        get_logger().error("Error in rate limit")
        return None


def github_action_output(output_data: dict, key_name: str):
    try:
        enable_output = get_settings().get('github_action_config.enable_output', False)
        if isinstance(enable_output, str):
            enable_output = enable_output.lower().strip() not in ("false", "0", "no", "")
        if not enable_output:
            return

        key_data = output_data.get(key_name, {})
        with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
            print(f"{key_name}={json.dumps(key_data, indent=None, ensure_ascii=False)}", file=fh)
    except Exception as e:
        get_logger().error(f"Failed to write to GitHub Action output: {e}")
    return


def show_relevant_configurations(relevant_section: str) -> str:
    skip_keys = ['ai_disclaimer', 'ai_disclaimer_title', 'ANALYTICS_FOLDER', 'secret_provider', "skip_keys", "app_id", "redirect",
                      'trial_prefix_message', 'no_eligible_message', 'identity_provider', 'ALLOWED_REPOS','APP_NAME']
    extra_skip_keys = get_settings().config.get('config.skip_keys', [])
    if extra_skip_keys:
        skip_keys.extend(extra_skip_keys)

    markdown_text = ""
    markdown_text += "\n<hr>\n<details> <summary><strong>🛠️ Relevant configurations:</strong></summary> \n\n"
    markdown_text +="<br>These are the relevant [configurations](https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml) for this tool:\n\n"
    markdown_text += "**[config**]\n```yaml\n\n"
    for key, value in get_settings().config.items():
        if key in skip_keys:
            continue
        markdown_text += f"{key}: {value}\n"
    markdown_text += "\n```\n"
    markdown_text += f"\n**[{relevant_section}]**\n```yaml\n\n"
    for key, value in get_settings().get(relevant_section, {}).items():
        if key in skip_keys:
            continue
        markdown_text += f"{key}: {value}\n"
    markdown_text += "\n```"
    markdown_text += "\n</details>\n"
    return markdown_text

def set_pr_string(repo_name, pr_number):
    return f"{repo_name}#{pr_number}"


def string_to_uniform_number(s: str) -> float:
    """
    Convert a string to a uniform number in the range [0, 1].
    The uniform distribution is achieved by the nature of the SHA-256 hash function, which produces a uniformly distributed hash value over its output space.
    """
    # Generate a hash of the string
    hash_object = hashlib.sha256(s.encode())
    # Convert the hash to an integer
    hash_int = int(hash_object.hexdigest(), 16)
    # Normalize the integer to the range [0, 1]
    max_hash_int = 2 ** 256 - 1
    uniform_number = float(hash_int) / max_hash_int
    return uniform_number


def process_description(description_full: str) -> Tuple[str, List]:
    if not description_full:
        return "", []

    # description_split = description_full.split(PRDescriptionHeader.FILE_WALKTHROUGH.value)
    if PRDescriptionHeader.FILE_WALKTHROUGH.value in description_full:
        try:
            # FILE_WALKTHROUGH are presented in a collapsible section in the description
            regex_pattern = r'<details.*?>\s*<summary>\s*<h3>\s*' + re.escape(PRDescriptionHeader.FILE_WALKTHROUGH.value) + r'\s*</h3>\s*</summary>'
            description_split = re.split(regex_pattern, description_full, maxsplit=1, flags=re.DOTALL)

            # If the regex pattern is not found, fallback to the previous method
            if len(description_split) == 1:
                get_logger().debug("Could not find regex pattern for file walkthrough, falling back to simple split")
                description_split = description_full.split(PRDescriptionHeader.FILE_WALKTHROUGH.value, 1)
        except Exception as e:
            get_logger().warning(f"Failed to split description using regex, falling back to simple split: {e}")
            description_split = description_full.split(PRDescriptionHeader.FILE_WALKTHROUGH.value, 1)

        if len(description_split) < 2:
            get_logger().error("Failed to split description into base and changes walkthrough", artifact={'description': description_full})
            return description_full.strip(), []

        base_description_str = description_split[0].strip()
        changes_walkthrough_str = ""
        files = []
        if len(description_split) > 1:
            changes_walkthrough_str = description_split[1]
        else:
            get_logger().debug("No changes walkthrough found")
    else:
        base_description_str = description_full.strip()
        return base_description_str, []

    try:
        if changes_walkthrough_str:
            # get the end of the table
            if '</table>\n\n___' in changes_walkthrough_str:
                end = changes_walkthrough_str.index("</table>\n\n___")
            elif '\n___' in changes_walkthrough_str:
                end = changes_walkthrough_str.index("\n___")
            else:
                end = len(changes_walkthrough_str)
            changes_walkthrough_str = changes_walkthrough_str[:end]

            h = html2text.HTML2Text()
            h.body_width = 0  # Disable line wrapping

            # find all the files
            pattern = r'<tr>\s*<td>\s*(<details>\s*<summary>(.*?)</summary>(.*?)</details>)\s*</td>'
            files_found = re.findall(pattern, changes_walkthrough_str, re.DOTALL)
            for file_data in files_found:
                try:
                    if isinstance(file_data, tuple):
                        file_data = file_data[0]
                    pattern = r'<details>\s*<summary><strong>(.*?)</strong>\s*<dd><code>(.*?)</code>.*?</summary>\s*<hr>\s*(.*?)\s*(?:<li>|•)(.*?)</details>'
                    res = re.search(pattern, file_data, re.DOTALL)
                    if not res or res.lastindex != 4:
                        pattern_back = r'<details>\s*<summary><strong>(.*?)</strong><dd><code>(.*?)</code>.*?</summary>\s*<hr>\s*(.*?)\n\n\s*(.*?)</details>'
                        res = re.search(pattern_back, file_data, re.DOTALL)
                    if not res or res.lastindex != 4:
                        pattern_back = r'<details>\s*<summary><strong>(.*?)</strong>\s*<dd><code>(.*?)</code>.*?</summary>\s*<hr>\s*(.*?)\s*-\s*(.*?)\s*</details>' # looking for hyphen ('- ')
                        res = re.search(pattern_back, file_data, re.DOTALL)
                    if res and res.lastindex == 4:
                        short_filename = res.group(1).strip()
                        short_summary = res.group(2).strip()
                        long_filename = res.group(3).strip()
                        if long_filename.endswith('<ul>'):
                            long_filename = long_filename[:-4].strip()
                        long_summary =  res.group(4).strip()
                        long_summary = long_summary.replace('<br> *', '\n*').replace('<br>','').replace('\n','<br>')
                        long_summary = h.handle(long_summary).strip()
                        if long_summary.startswith('\\-'):
                            long_summary = "* " + long_summary[2:]
                        elif not long_summary.startswith('*'):
                            long_summary = f"* {long_summary}"

                        files.append({
                            'short_file_name': short_filename,
                            'full_file_name': long_filename,
                            'short_summary': short_summary,
                            'long_summary': long_summary
                        })
                    else:
                        if '<code>...</code>' in file_data:
                            pass # PR with many files. some did not get analyzed
                        else:
                            get_logger().warning("Failed to parse description", artifact={'description': file_data})
                except Exception as e:
                    get_logger().exception(f"Failed to process description: {e}", artifact={'description': file_data})


    except Exception as e:
        get_logger().exception(f"Failed to process description: {e}")

    return base_description_str, files

def get_version() -> str:
    # First check pyproject.toml if running directly out of repository
    if os.path.exists("pyproject.toml"):
        if sys.version_info >= (3, 11):
            import tomllib
            with open("pyproject.toml", "rb") as f:
                data = tomllib.load(f)
                if "project" in data and "version" in data["project"]:
                    return data["project"]["version"]
                else:
                    get_logger().warning("Version not found in pyproject.toml")
        else:
            get_logger().warning("Unable to determine local version from pyproject.toml")

    # Otherwise get the installed pip package version
    try:
        return version('pr-agent')
    except PackageNotFoundError:
        get_logger().warning("Unable to find package named 'pr-agent'")
        return "unknown"


def set_file_languages(diff_files) -> List[FilePatchInfo]:
    try:
        # if the language is already set, do not change it
        if hasattr(diff_files[0], 'language') and diff_files[0].language:
            return diff_files

        # map file extensions to programming languages
        language_extension_map_org = get_settings().language_extension_map_org
        extension_to_language = {}
        for language, extensions in language_extension_map_org.items():
            for ext in extensions:
                extension_to_language[ext] = language
        for file in diff_files:
            extension_s = '.' + file.filename.rsplit('.')[-1]
            language_name = "txt"
            if extension_s and (extension_s in extension_to_language):
                language_name = extension_to_language[extension_s]
            file.language = language_name.lower()
    except Exception as e:
        get_logger().exception(f"Failed to set file languages: {e}")

    return diff_files
