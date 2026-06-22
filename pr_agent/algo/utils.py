from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import html
import json
import os
import re
import sys
import textwrap
import time
import traceback
from datetime import datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from typing import Any, List, Tuple, TypedDict

import html2text
import requests
import yaml
from pydantic import BaseModel
from starlette_context import context

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.git_patch_processing import extract_hunk_lines_from_patch
from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger


def get_model(model_type: str = "model_weak") -> str:
    if model_type == "model_weak" and get_settings().get("config.model_weak"):
        return get_settings().config.model_weak
    elif model_type == "model_reasoning" and get_settings().get("config.model_reasoning"):
        return get_settings().config.model_reasoning
    return get_settings().config.model


class Range(BaseModel):
    line_start: int  # should be 0-indexed
    line_end: int
    column_start: int = -1
    column_end: int = -1


class ModelType(str, Enum):
    REGULAR = "regular"
    WEAK = "weak"
    REASONING = "reasoning"


class TodoItem(TypedDict):
    relevant_file: str
    line_range: Tuple[int, int]
    content: str


class PRReviewHeader(str, Enum):
    REGULAR = "## PR Reviewer Guide"
    INCREMENTAL = "## Incremental PR Reviewer Guide"


class ReasoningEffort(str, Enum):
    XHIGH = "xhigh"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MINIMAL = "minimal"
    NONE = "none"


class PRDescriptionHeader(str, Enum):
    DIAGRAM_WALKTHROUGH = "Diagram Walkthrough"
    FILE_WALKTHROUGH = "File Walkthrough"


def get_setting(key: str) -> Any:
    try:
        key = key.upper()
        return context.get("settings", global_settings).get(key, global_settings.get(key, None))
    except Exception:
        return global_settings.get(key, None)


def emphasize_header(text: str, only_markdown=False, reference_link=None) -> str:
    try:
        # Finding the position of the first occurrence of ": "
        colon_position = text.find(": ")

        # Splitting the string and wrapping the first part in <strong> tags
        if colon_position != -1:
            # Everything before the colon (inclusive) is wrapped in <strong> tags
            if only_markdown:
                if reference_link:
                    transformed_string = f"[**{text[:colon_position + 1]}**]({reference_link})\n" + text[colon_position + 1:]
                else:
                    transformed_string = f"**{text[:colon_position + 1]}**\n" + text[colon_position + 1:]
            else:
                if reference_link:
                    transformed_string = f"<strong><a href='{reference_link}'>{text[:colon_position + 1]}</a></strong><br>" + text[colon_position + 1:]
                else:
                    transformed_string = "<strong>" + text[:colon_position + 1] + "</strong>" +'<br>' + text[colon_position + 1:]
        else:
            # If there's no ": ", return the original string
            transformed_string = text

        return transformed_string
    except Exception as e:
        get_logger().exception(f"Failed to emphasize header: {e}")
        return text


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


def convert_to_markdown_v2(output_data: dict,
                           gfm_supported: bool = True,
                           incremental_review=None,
                           git_provider=None,
                           files=None) -> str:
    """
    Convert a dictionary of data into markdown format.
    Args:
        output_data (dict): A dictionary containing data to be converted to markdown format.
    Returns:
        str: The markdown formatted text generated from the input dictionary.
    """

    emojis = {
        "Can be split": "🔀",
        "Key issues to review": "⚡",
        "Recommended focus areas for review": "⚡",
        "Score": "🏅",
        "Relevant tests": "🧪",
        "Focused PR": "✨",
        "Relevant ticket": "🎫",
        "Security concerns": "🔒",
        "Todo sections": "📝",
        "Insights from user's answers": "📝",
        "Code feedback": "🤖",
        "Estimated effort to review [1-5]": "⏱️",
        "Contribution time cost estimate": "⏳",
        "Ticket compliance check": "🎫",
    }
    markdown_text = ""
    if not incremental_review:
        markdown_text += f"{PRReviewHeader.REGULAR.value} 🔍\n\n"
    else:
        markdown_text += f"{PRReviewHeader.INCREMENTAL.value} 🔍\n\n"
        markdown_text += f"⏮️ Review for commits since previous PR-Agent review {incremental_review}.\n\n"
    if not output_data or not output_data.get('review', {}):
        return ""

    if get_settings().get("pr_reviewer.enable_intro_text", False):
        markdown_text += f"Here are some key observations to aid the review process:\n\n"

    if gfm_supported:
        markdown_text += "<table>\n"

    todo_summary = output_data['review'].pop('todo_summary', '')
    for key, value in output_data['review'].items():
        if value is None or value == '' or value == {} or value == []:
            if key.lower() not in ['can_be_split', 'key_issues_to_review']:
                continue
        key_nice = key.replace('_', ' ').capitalize()
        emoji = emojis.get(key_nice, "")
        if 'Estimated effort to review' in key_nice:
            key_nice = 'Estimated effort to review'
            value = str(value).strip()
            if value.isnumeric():
                value_int = int(value)
            else:
                try:
                    value_int = int(value.split(',')[0])
                except ValueError:
                    continue
            blue_bars = '🔵' * value_int
            white_bars = '⚪' * (5 - value_int)
            value = f"{value_int} {blue_bars}{white_bars}"
            if gfm_supported:
                markdown_text += f"<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong>: {value}"
                markdown_text += f"</td></tr>\n"
            else:
                markdown_text += f"### {emoji} {key_nice}: {value}\n\n"
        elif 'relevant tests' in key_nice.lower():
            value = str(value).strip().lower()
            if gfm_supported:
                markdown_text += f"<tr><td>"
                if is_value_no(value):
                    markdown_text += f"{emoji}&nbsp;<strong>No relevant tests</strong>"
                else:
                    markdown_text += f"{emoji}&nbsp;<strong>PR contains tests</strong>"
                markdown_text += f"</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += f'### {emoji} No relevant tests\n\n'
                else:
                    markdown_text += f"### {emoji} PR contains tests\n\n"
        elif 'ticket compliance check' in key_nice.lower():
            markdown_text = ticket_markdown_logic(emoji, markdown_text, value, gfm_supported)
        elif 'contribution time cost estimate' in key_nice.lower():
            if gfm_supported:
                markdown_text += f"<tr><td>{emoji}&nbsp;<strong>Contribution time estimate</strong> (best, average, worst case): "
                markdown_text += f"{value['best_case'].replace('m', ' minutes')} | {value['average_case'].replace('m', ' minutes')} | {value['worst_case'].replace('m', ' minutes')}"
                markdown_text += f"</td></tr>\n"
            else:
                markdown_text += f"### {emoji} Contribution time estimate (best, average, worst case): "
                markdown_text += f"{value['best_case'].replace('m', ' minutes')} | {value['average_case'].replace('m', ' minutes')} | {value['worst_case'].replace('m', ' minutes')}\n\n"
        elif 'security concerns' in key_nice.lower():
            if gfm_supported:
                markdown_text += f"<tr><td>"
                if is_value_no(value):
                    markdown_text += f"{emoji}&nbsp;<strong>No security concerns identified</strong>"
                else:
                    markdown_text += f"{emoji}&nbsp;<strong>Security concerns</strong><br><br>\n\n"
                    value = emphasize_header(value.strip())
                    markdown_text += f"{value}"
                markdown_text += f"</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += f'### {emoji} No security concerns identified\n\n'
                else:
                    markdown_text += f"### {emoji} Security concerns\n\n"
                    value = emphasize_header(value.strip(), only_markdown=True)
                    markdown_text += f"{value}\n\n"
        elif 'todo sections' in key_nice.lower():
            if gfm_supported:
                markdown_text += "<tr><td>"
                if is_value_no(value):
                    markdown_text += f"✅&nbsp;<strong>No TODO sections</strong>"
                else:
                    markdown_todo_items = format_todo_items(value, git_provider, gfm_supported)
                    markdown_text += f"{emoji}&nbsp;<strong>TODO sections</strong>\n<br><br>\n"
                    markdown_text += markdown_todo_items
                markdown_text += "</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += f"### ✅ No TODO sections\n\n"
                else:
                    markdown_todo_items = format_todo_items(value, git_provider, gfm_supported)
                    markdown_text += f"### {emoji} TODO sections\n\n"
                    markdown_text += markdown_todo_items
        elif 'can be split' in key_nice.lower():
            if gfm_supported:
                markdown_text += f"<tr><td>"
                markdown_text += process_can_be_split(emoji, value)
                markdown_text += f"</td></tr>\n"
        elif 'key issues to review' in key_nice.lower():
            # value is a list of issues
            if is_value_no(value):
                if gfm_supported:
                    markdown_text += f"<tr><td>"
                    markdown_text += f"{emoji}&nbsp;<strong>No major issues detected</strong>"
                    markdown_text += f"</td></tr>\n"
                else:
                    markdown_text += f"### {emoji} No major issues detected\n\n"
            else:
                issues = value
                if gfm_supported:
                    markdown_text += f"<tr><td>"
                    # markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong><br><br>\n\n"
                    markdown_text += f"{emoji}&nbsp;<strong>Recommended focus areas for review</strong><br><br>\n\n"
                else:
                    markdown_text += f"### {emoji} Recommended focus areas for review\n\n#### \n"
                for i, issue in enumerate(issues):
                    try:
                        if not issue or not isinstance(issue, dict):
                            continue
                        relevant_file = issue.get('relevant_file', '').strip()
                        issue_header = issue.get('issue_header', '').strip()
                        if issue_header.lower() == 'possible bug':
                            issue_header = 'Possible Issue'  # Make the header less frightening
                        issue_content = issue.get('issue_content', '').strip()
                        start_line = int(str(issue.get('start_line', 0)).strip())
                        end_line = int(str(issue.get('end_line', 0)).strip())

                        relevant_lines_str = extract_relevant_lines_str(end_line, files, relevant_file, start_line, dedent=True)
                        if git_provider:
                            reference_link = git_provider.get_line_link(relevant_file, start_line, end_line)
                        else:
                            reference_link = None

                        if gfm_supported:
                            if reference_link is not None and len(reference_link) > 0:
                                if relevant_lines_str:
                                    issue_str = f"<details><summary><a href='{reference_link}'><strong>{issue_header}</strong></a>\n\n{issue_content}\n</summary>\n\n{relevant_lines_str}\n\n</details>"
                                else:
                                    issue_str = f"<a href='{reference_link}'><strong>{issue_header}</strong></a><br>{issue_content}"
                            else:
                                issue_str = f"<strong>{issue_header}</strong><br>{issue_content}"
                        else:
                            if reference_link is not None and len(reference_link) > 0:
                                issue_str = f"[**{issue_header}**]({reference_link})\n\n{issue_content}\n\n"
                            else:
                                issue_str = f"**{issue_header}**\n\n{issue_content}\n\n"
                        markdown_text += f"{issue_str}\n\n"
                    except Exception as e:
                        get_logger().exception(f"Failed to process 'Recommended focus areas for review': {e}")
                if gfm_supported:
                    markdown_text += f"</td></tr>\n"
        else:
            if gfm_supported:
                markdown_text += f"<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong>: {value}"
                markdown_text += f"</td></tr>\n"
            else:
                markdown_text += f"### {emoji} {key_nice}: {value}\n\n"

    if gfm_supported:
        markdown_text += "</table>\n"

    return markdown_text


def extract_relevant_lines_str(end_line, files, relevant_file, start_line, dedent=False) -> str:
    """
    Finds 'relevant_file' in 'files', and extracts the lines from 'start_line' to 'end_line' string from the file content.
    """
    try:
        relevant_lines_str = ""
        if files:
            files = set_file_languages(files)
            for file in files:
                if file.filename.strip() == relevant_file:
                    if not file.head_file:
                        # as a fallback, extract relevant lines directly from patch
                        patch = file.patch
                        get_logger().info(f"No content found in file: '{file.filename}' for 'extract_relevant_lines_str'. Using patch instead")
                        _, selected_lines = extract_hunk_lines_from_patch(patch, file.filename, start_line, end_line,side='right')
                        if not selected_lines:
                            get_logger().error(f"Failed to extract relevant lines from patch: {file.filename}")
                            return ""
                        # filter out '-' lines
                        relevant_lines_str = ""
                        for line in selected_lines.splitlines():
                            if line.startswith('-'):
                                continue
                            relevant_lines_str += line[1:] + '\n'
                    else:
                        relevant_file_lines = file.head_file.splitlines()
                        relevant_lines_str = "\n".join(relevant_file_lines[start_line - 1:end_line])

                    if dedent and relevant_lines_str:
                        # Remove the longest leading string of spaces and tabs common to all lines.
                        relevant_lines_str = textwrap.dedent(relevant_lines_str)
                    relevant_lines_str = f"```{file.language}\n{relevant_lines_str}\n```"
                    break

        return relevant_lines_str
    except Exception as e:
        get_logger().exception(f"Failed to extract relevant lines: {e}")
        return ""


def ticket_markdown_logic(emoji, markdown_text, value, gfm_supported) -> str:
    ticket_compliance_str = ""
    compliance_emoji = ''
    # Track compliance levels across all tickets
    all_compliance_levels = []

    if isinstance(value, list):
        for ticket_analysis in value:
            try:
                ticket_url = ticket_analysis.get('ticket_url', '').strip()
                explanation = ''
                ticket_compliance_level = ''  # Individual ticket compliance
                fully_compliant_str = ticket_analysis.get('fully_compliant_requirements', '').strip()
                not_compliant_str = ticket_analysis.get('not_compliant_requirements', '').strip()
                requires_further_human_verification = ticket_analysis.get('requires_further_human_verification',
                                                                          '').strip()

                if not fully_compliant_str and not not_compliant_str:
                    get_logger().debug(f"Ticket compliance has no requirements",
                                       artifact={'ticket_url': ticket_url})
                    continue

                # Calculate individual ticket compliance level
                if fully_compliant_str:
                    if not_compliant_str:
                        ticket_compliance_level = 'Partially compliant'
                    else:
                        if not requires_further_human_verification:
                            ticket_compliance_level = 'Fully compliant'
                        else:
                            ticket_compliance_level = 'PR Code Verified'
                elif not_compliant_str:
                    ticket_compliance_level = 'Not compliant'

                # Store the compliance level for aggregation
                if ticket_compliance_level:
                    all_compliance_levels.append(ticket_compliance_level)

                # build compliance string
                if fully_compliant_str:
                    explanation += f"Compliant requirements:\n\n{fully_compliant_str}\n\n"
                if not_compliant_str:
                    explanation += f"Non-compliant requirements:\n\n{not_compliant_str}\n\n"
                if requires_further_human_verification:
                    explanation += f"Requires further human verification:\n\n{requires_further_human_verification}\n\n"
                ticket_compliance_str += f"\n\n**[{ticket_url.split('/')[-1]}]({ticket_url}) - {ticket_compliance_level}**\n\n{explanation}\n\n"

                # for debugging
                if requires_further_human_verification:
                    get_logger().debug(f"Ticket compliance requires further human verification",
                                       artifact={'ticket_url': ticket_url,
                                                 'requires_further_human_verification': requires_further_human_verification,
                                                 'compliance_level': ticket_compliance_level})

            except Exception as e:
                get_logger().exception(f"Failed to process ticket compliance: {e}")
                continue

        # Calculate overall compliance level and emoji
        if all_compliance_levels:
            if all(level == 'Fully compliant' for level in all_compliance_levels):
                compliance_level = 'Fully compliant'
                compliance_emoji = '✅'
            elif all(level == 'PR Code Verified' for level in all_compliance_levels):
                compliance_level = 'PR Code Verified'
                compliance_emoji = '✅'
            elif any(level == 'Not compliant' for level in all_compliance_levels):
                # If there's a mix of compliant and non-compliant tickets
                if any(level in ['Fully compliant', 'PR Code Verified'] for level in all_compliance_levels):
                    compliance_level = 'Partially compliant'
                    compliance_emoji = '🔶'
                else:
                    compliance_level = 'Not compliant'
                    compliance_emoji = '❌'
            elif any(level == 'Partially compliant' for level in all_compliance_levels):
                compliance_level = 'Partially compliant'
                compliance_emoji = '🔶'
            else:
                compliance_level = 'PR Code Verified'
                compliance_emoji = '✅'

            # Set extra statistics outside the ticket loop
            get_settings().set('config.extra_statistics', {'compliance_level': compliance_level})

        # editing table row for ticket compliance analysis
        if gfm_supported:
            markdown_text += f"<tr><td>\n\n"
            markdown_text += f"**{emoji} Ticket compliance analysis {compliance_emoji}**\n\n"
            markdown_text += ticket_compliance_str
            markdown_text += f"</td></tr>\n"
        else:
            markdown_text += f"### {emoji} Ticket compliance analysis {compliance_emoji}\n\n"
            markdown_text += ticket_compliance_str + "\n\n"

    return markdown_text


def process_can_be_split(emoji, value):
    try:
        # key_nice = "Can this PR be split?"
        key_nice = "Multiple PR themes"
        markdown_text = ""
        if not value or isinstance(value, list) and len(value) == 1:
            value = "No"
            # markdown_text += f"<tr><td> {emoji}&nbsp;<strong>{key_nice}</strong></td><td>\n\n{value}\n\n</td></tr>\n"
            # markdown_text += f"### {emoji} No multiple PR themes\n\n"
            markdown_text += f"{emoji} <strong>No multiple PR themes</strong>\n\n"
        else:
            markdown_text += f"{emoji} <strong>{key_nice}</strong><br><br>\n\n"
            for i, split in enumerate(value):
                title = split.get('title', '')
                relevant_files = split.get('relevant_files', [])
                markdown_text += f"<details><summary>\nSub-PR theme: <b>{title}</b></summary>\n\n"
                markdown_text += f"___\n\nRelevant files:\n\n"
                for file in relevant_files:
                    markdown_text += f"- {file}\n"
                markdown_text += f"___\n\n"
                markdown_text += f"</details>\n\n"

                # markdown_text += f"#### Sub-PR theme: {title}\n\n"
                # markdown_text += f"Relevant files:\n\n"
                # for file in relevant_files:
                #     markdown_text += f"- {file}\n"
                # markdown_text += "\n"
            # number_of_splits = len(value)
            # markdown_text += f"<tr><td rowspan={number_of_splits}> {emoji}&nbsp;<strong>{key_nice}</strong></td>\n"
            # for i, split in enumerate(value):
            #     title = split.get('title', '')
            #     relevant_files = split.get('relevant_files', [])
            #     if i == 0:
            #         markdown_text += f"<td><details><summary>\nSub-PR theme:<br><strong>{title}</strong></summary>\n\n"
            #         markdown_text += f"<hr>\n"
            #         markdown_text += f"Relevant files:\n"
            #         markdown_text += f"<ul>\n"
            #         for file in relevant_files:
            #             markdown_text += f"<li>{file}</li>\n"
            #         markdown_text += f"</ul>\n\n</details></td></tr>\n"
            #     else:
            #         markdown_text += f"<tr>\n<td><details><summary>\nSub-PR theme:<br><strong>{title}</strong></summary>\n\n"
            #         markdown_text += f"<hr>\n"
            #         markdown_text += f"Relevant files:\n"
            #         markdown_text += f"<ul>\n"
            #         for file in relevant_files:
            #             markdown_text += f"<li>{file}</li>\n"
            #         markdown_text += f"</ul>\n\n</details></td></tr>\n"
    except Exception as e:
        get_logger().exception(f"Failed to process can be split: {e}")
        return ""
    return markdown_text


def parse_code_suggestion(code_suggestion: dict, i: int = 0, gfm_supported: bool = True) -> str:
    """
    Convert a dictionary of data into markdown format.

    Args:
        code_suggestion (dict): A dictionary containing data to be converted to markdown format.

    Returns:
        str: A string containing the markdown formatted text generated from the input dictionary.
    """
    markdown_text = ""
    if gfm_supported and 'relevant_line' in code_suggestion:
        markdown_text += '<table>'
        for sub_key, sub_value in code_suggestion.items():
            try:
                if sub_key.lower() == 'relevant_file':
                    relevant_file = sub_value.strip('`').strip('"').strip("'")
                    markdown_text += f"<tr><td>relevant file</td><td>{relevant_file}</td></tr>"
                    # continue
                elif sub_key.lower() == 'suggestion':
                    markdown_text += (f"<tr><td>{sub_key} &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</td>"
                                      f"<td>\n\n<strong>\n\n{sub_value.strip()}\n\n</strong>\n</td></tr>")
                elif sub_key.lower() == 'relevant_line':
                    markdown_text += f"<tr><td>relevant line</td>"
                    sub_value_list = sub_value.split('](')
                    relevant_line = sub_value_list[0].lstrip('`').lstrip('[')
                    if len(sub_value_list) > 1:
                        link = sub_value_list[1].rstrip(')').strip('`')
                        markdown_text += f"<td><a href='{link}'>{relevant_line}</a></td>"
                    else:
                        markdown_text += f"<td>{relevant_line}</td>"
                    markdown_text += "</tr>"
            except Exception as e:
                get_logger().exception(f"Failed to parse code suggestion: {e}")
                pass
        markdown_text += '</table>'
        markdown_text += "<hr>"
    else:
        for sub_key, sub_value in code_suggestion.items():
            if isinstance(sub_key, str):
                sub_key = sub_key.rstrip()
            if isinstance(sub_value,str):
                sub_value = sub_value.rstrip()
            if isinstance(sub_value, dict):  # "code example"
                markdown_text += f"  - **{sub_key}:**\n"
                for code_key, code_value in sub_value.items():  # 'before' and 'after' code
                    code_str = f"```\n{code_value}\n```"
                    code_str_indented = textwrap.indent(code_str, '        ')
                    markdown_text += f"    - **{code_key}:**\n{code_str_indented}\n"
            else:
                if "relevant_file" in sub_key.lower():
                    markdown_text += f"\n  - **{sub_key}:** {sub_value}  \n"
                else:
                    markdown_text += f"   **{sub_key}:** {sub_value}  \n"
                if "relevant_line" not in sub_key.lower():  # nicer presentation
                    # markdown_text = markdown_text.rstrip('\n') + "\\\n" # works for gitlab
                    markdown_text = markdown_text.rstrip('\n') + "   \n"  # works for gitlab and bitbucker

        markdown_text += "\n"
    return markdown_text


def try_fix_json(review, max_iter=10, code_suggestions=False):
    """
    Fix broken or incomplete JSON messages and return the parsed JSON data.

    Args:
    - review: A string containing the JSON message to be fixed.
    - max_iter: An integer representing the maximum number of iterations to try and fix the JSON message.
    - code_suggestions: A boolean indicating whether to try and fix JSON messages with code feedback.

    Returns:
    - data: A dictionary containing the parsed JSON data.

    The function attempts to fix broken or incomplete JSON messages by parsing until the last valid code suggestion.
    If the JSON message ends with a closing bracket, the function calls the fix_json_escape_char function to fix the
    message.
    If code_suggestions is True and the JSON message contains code feedback, the function tries to fix the JSON
    message by parsing until the last valid code suggestion.
    The function uses regular expressions to find the last occurrence of "}," with any number of whitespaces or
    newlines.
    It tries to parse the JSON message with the closing bracket and checks if it is valid.
    If the JSON message is valid, the parsed JSON data is returned.
    If the JSON message is not valid, the last code suggestion is removed and the process is repeated until a valid JSON
    message is obtained or the maximum number of iterations is reached.
    If a valid JSON message is not obtained, an error is logged and an empty dictionary is returned.
    """

    if review.endswith("}"):
        return fix_json_escape_char(review)

    data = {}
    if code_suggestions:
        closing_bracket = "]}"
    else:
        closing_bracket = "]}}"

    if (review.rfind("'Code feedback': [") > 0 or review.rfind('"Code feedback": [') > 0) or \
            (review.rfind("'Code suggestions': [") > 0 or review.rfind('"Code suggestions": [') > 0) :
        last_code_suggestion_ind = [m.end() for m in re.finditer(r"\}\s*,", review)][-1] - 1
        valid_json = False
        iter_count = 0

        while last_code_suggestion_ind > 0 and not valid_json and iter_count < max_iter:
            try:
                data = json.loads(review[:last_code_suggestion_ind] + closing_bracket)
                valid_json = True
                review = review[:last_code_suggestion_ind].strip() + closing_bracket
            except json.decoder.JSONDecodeError:
                review = review[:last_code_suggestion_ind]
                last_code_suggestion_ind = [m.end() for m in re.finditer(r"\}\s*,", review)][-1] - 1
                iter_count += 1

        if not valid_json:
            get_logger().error("Unable to decode JSON response from AI")
            data = {}

    return data


def fix_json_escape_char(json_message=None):
    """
    Fix broken or incomplete JSON messages and return the parsed JSON data.

    Args:
        json_message (str): A string containing the JSON message to be fixed.

    Returns:
        dict: A dictionary containing the parsed JSON data.

    Raises:
        None

    """
    try:
        result = json.loads(json_message)
    except Exception as e:
        # Find the offending character index:
        idx_to_replace = int(str(e).split(' ')[-1].replace(')', ''))
        # Remove the offending character:
        json_message = list(json_message)
        json_message[idx_to_replace] = ' '
        new_message = ''.join(json_message)
        return fix_json_escape_char(json_message=new_message)
    return result


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


def load_yaml(response_text: str, keys_fix_yaml: List[str] = [], first_key="", last_key="") -> dict:
    response_text_original = copy.deepcopy(response_text)
    response_text = response_text.strip('\n').removeprefix('yaml').removeprefix('```yaml').rstrip().removesuffix('```')
    try:
        data = yaml.safe_load(response_text)
    except Exception as e:
        get_logger().warning(f"Initial failure to parse AI prediction: {e}")
        data = try_fix_yaml(response_text, keys_fix_yaml=keys_fix_yaml, first_key=first_key, last_key=last_key,
                            response_text_original=response_text_original)
        if not data:
            get_logger().error(f"Failed to parse AI prediction after fallbacks",
                               artifact={'response_text': response_text})
        else:
            get_logger().info(f"Successfully parsed AI prediction after fallbacks",
                              artifact={'response_text': response_text})
    return data



def try_fix_yaml(response_text: str,
                 keys_fix_yaml: List[str] = [],
                 first_key="",
                 last_key="",
                 response_text_original="") -> dict:
    response_text_lines = response_text.split('\n')

    keys_yaml = ['relevant line:', 'suggestion content:', 'relevant file:', 'existing code:',
                 'improved code:', 'label:', 'why:', 'suggestion_summary:']
    keys_yaml = keys_yaml + keys_fix_yaml

    # first fallback - try to convert 'relevant line: ...' to relevant line: |-\n        ...'
    response_text_lines_copy = response_text_lines.copy()
    for i in range(0, len(response_text_lines_copy)):
        for key in keys_yaml:
            if key in response_text_lines_copy[i] and not '|' in response_text_lines_copy[i]:
                response_text_lines_copy[i] = response_text_lines_copy[i].replace(f'{key}',
                                                                                  f'{key} |\n        ')
    try:
        data = yaml.safe_load('\n'.join(response_text_lines_copy))
        get_logger().info(f"Successfully parsed AI prediction after adding |-\n")
        return data
    except:
        pass

    # 1.5 fallback - try to convert '|' to '|2'. Will solve cases of indent decreasing during the code
    response_text_copy = copy.deepcopy(response_text)
    response_text_copy = response_text_copy.replace('|\n', '|2\n')
    try:
        data = yaml.safe_load(response_text_copy)
        get_logger().info(f"Successfully parsed AI prediction after replacing | with |2")
        return data
    except:
        # if it fails, we can try to add spaces to the lines that are not indented properly, and contain '}'.
        response_text_lines_copy = response_text_copy.split('\n')
        for i in range(0, len(response_text_lines_copy)):
            initial_space = len(response_text_lines_copy[i]) - len(response_text_lines_copy[i].lstrip())
            if initial_space == 2 and '|2' not in response_text_lines_copy[i] and '}' in response_text_lines_copy[i]:
                response_text_lines_copy[i] = '    ' + response_text_lines_copy[i].lstrip()
        try:
            data = yaml.safe_load('\n'.join(response_text_lines_copy))
            get_logger().info(f"Successfully parsed AI prediction after replacing | with |2 and adding spaces")
            return data
        except:
            pass

    # second fallback - try to extract only range from first ```yaml to the last ```
    snippet_pattern = r'```(yaml|yml)?([\s\S]*?)```(?=\s*$|")'
    snippet = re.search(snippet_pattern, '\n'.join(response_text_lines_copy))
    if not snippet:
        snippet = re.search(snippet_pattern, response_text_original) # before we removed the "```"
    if snippet:
        # group(2) is the snippet body, without the ``` fences or the optional yaml/yml language identifier
        snippet_text = snippet.group(2)
        try:
            data = yaml.safe_load(snippet_text)
            get_logger().info(f"Successfully parsed AI prediction after extracting yaml snippet")
            return data
        except Exception as e:
            get_logger().debug(f"Failed to parse AI prediction after extracting yaml snippet: {e}")


    # third fallback - try to remove leading and trailing curly brackets
    response_text_copy = response_text.strip().rstrip().removeprefix('{').removesuffix('}').rstrip(':\n')
    try:
        data = yaml.safe_load(response_text_copy)
        get_logger().info(f"Successfully parsed AI prediction after removing curly brackets")
        return data
    except:
        pass


    # forth fallback - try to extract yaml snippet by 'first_key' and 'last_key'
    # note that 'last_key' can be in practice a key that is not the last key in the yaml snippet.
    # it just needs to be some inner key, so we can look for newlines after it
    if first_key and last_key:
        index_start = response_text.find(f"\n{first_key}:")
        if index_start == -1:
            index_start = response_text.find(f"{first_key}:")
        index_last_code = response_text.rfind(f"{last_key}:")
        index_end = response_text.find("\n\n", index_last_code) # look for newlines after last_key
        if index_end == -1:
            index_end = len(response_text)
        response_text_copy = response_text[index_start:index_end].strip().strip('```yaml').strip('`').strip()
        if response_text_copy:
            try:
                data = yaml.safe_load(response_text_copy)
                get_logger().info(f"Successfully parsed AI prediction after extracting yaml snippet")
                return data
            except:
                pass

    # fifth fallback - try to remove leading '+' (sometimes added by AI for 'existing code' and 'improved code')
    response_text_lines_copy = response_text_lines.copy()
    for i in range(0, len(response_text_lines_copy)):
        if response_text_lines_copy[i].startswith('+'):
            response_text_lines_copy[i] = ' ' + response_text_lines_copy[i][1:]
    try:
        data = yaml.safe_load('\n'.join(response_text_lines_copy))
        get_logger().info(f"Successfully parsed AI prediction after removing leading '+'")
        return data
    except:
        pass

    # sixth fallback - replace tabs with spaces
    if '\t' in response_text:
        response_text_copy = copy.deepcopy(response_text)
        response_text_copy = response_text_copy.replace('\t', '    ')
        try:
            data = yaml.safe_load(response_text_copy)
            get_logger().info(f"Successfully parsed AI prediction after replacing tabs with spaces")
            return data
        except:
            pass

    # seventh fallback - add indent for sections of code blocks
    response_text_copy = copy.deepcopy(response_text)
    response_text_copy_lines = response_text_copy.split('\n')
    start_line = -1
    improve_sections = ['existing_code:', 'improved_code:', 'response:', 'why:']
    describe_sections = ['description:', 'title:', 'changes_diagram:', 'pr_files:', 'pr_ticket:']
    for i, line in enumerate(response_text_copy_lines):
        line_stripped = line.rstrip()
        if any(key in line_stripped for key in (improve_sections+describe_sections)):
            start_line = i
        elif line_stripped.endswith(': |') or line_stripped.endswith(': |-') or line_stripped.endswith(': |2') or any(line_stripped.endswith(key) for key in keys_yaml):
            start_line = -1
        elif start_line != -1:
            response_text_copy_lines[i] = '    ' + line
    response_text_copy = '\n'.join(response_text_copy_lines)
    response_text_copy = response_text_copy.replace(' |\n', ' |2\n')
    try:
        data = yaml.safe_load(response_text_copy)
        get_logger().info(f"Successfully parsed AI prediction after adding indent for sections of code blocks")
        return data
    except:
        pass

    # eighth fallback - try to remove pipe chars at the root-level dicts
    response_text_copy = copy.deepcopy(response_text)
    response_text_copy = response_text_copy.lstrip('|\n')
    try:
        data = yaml.safe_load(response_text_copy)
        get_logger().info(f"Successfully parsed AI prediction after removing pipe chars")
        return data
    except:
        pass

    # ninth fallback - try to decode the response text with different encodings. GPT-5 can return text that is not utf-8 encoded.
    encodings_to_try = ['latin-1', 'utf-16']
    for encoding in encodings_to_try:
        try:
            data = yaml.safe_load(response_text.encode(encoding).decode("utf-8"))
            if data:
                get_logger().info(f"Successfully parsed AI prediction after decoding with {encoding} encoding")
                return data
        except:
            pass

    # # sixth fallback - try to remove last lines
    # for i in range(1, len(response_text_lines)):
    #     response_text_lines_tmp = '\n'.join(response_text_lines[:-i])
    #     try:
    #         data = yaml.safe_load(response_text_lines_tmp)
    #         get_logger().info(f"Successfully parsed AI prediction after removing {i} lines")
    #         return data
    #     except:
    #         pass



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


def get_max_tokens(model):
    """
    Get the maximum number of tokens allowed for a model.
    logic:
    (1) If the model is in './pr_agent/algo/__init__.py', use the value from there.
    (2) else, the user needs to define explicitly 'config.custom_model_max_tokens'

    For both cases, we further limit the number of tokens to 'config.max_model_tokens' if it is set.
    This aims to improve the algorithmic quality, as the AI model degrades in performance when the input is too long.
    """
    settings = get_settings()
    if model in MAX_TOKENS:
        max_tokens_model = MAX_TOKENS[model]
    elif settings.config.custom_model_max_tokens > 0:
        max_tokens_model = settings.config.custom_model_max_tokens
    else:
        get_logger().error(f"Model {model} is not defined in MAX_TOKENS in ./pr_agent/algo/__init__.py and no custom_model_max_tokens is set")
        raise Exception(f"Ensure {model} is defined in MAX_TOKENS in ./pr_agent/algo/__init__.py or set a positive value for it in config.custom_model_max_tokens")

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

def replace_code_tags(text):
    """
    Replace odd instances of ` with <code> and even instances of ` with </code>
    """
    text = html.escape(text)
    parts = text.split('`')
    for i in range(1, len(parts), 2):
        parts[i] = '<code>' + parts[i] + '</code>'
    return ''.join(parts)


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

    response = requests.get(RATE_LIMIT_URL, headers=HEADERS)
    try:
        rate_limit_info = response.json()
        if rate_limit_info.get('message') == 'Rate limiting is not enabled.':  # for github enterprise
            return {'resources': {}}
        response.raise_for_status()  # Check for HTTP errors
    except:  # retry
        time.sleep(0.1)
        response = requests.get(RATE_LIMIT_URL, headers=HEADERS)
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
    markdown_text += f"**[config**]\n```yaml\n\n"
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

def is_value_no(value):
    if not value:
        return True
    value_str = str(value).strip().lower()
    if value_str == 'no' or value_str == 'none' or value_str == 'false':
        return True
    return False


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
                            get_logger().warning(f"Failed to parse description", artifact={'description': file_data})
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

def format_todo_item(todo_item: TodoItem, git_provider, gfm_supported) -> str:
    relevant_file = todo_item.get('relevant_file', '').strip()
    line_number = todo_item.get('line_number', '')
    content = todo_item.get('content', '')
    reference_link = git_provider.get_line_link(relevant_file, line_number, line_number)
    file_ref = f"{relevant_file} [{line_number}]"
    if reference_link:
        if gfm_supported:
            file_ref = f"<a href='{reference_link}'>{file_ref}</a>"
        else:
            file_ref = f"[{file_ref}]({reference_link})"

    if content:
        return f"{file_ref}: {content.strip()}"
    else:
        # if content is empty, return only the file reference
        return file_ref


def format_todo_items(value: list[TodoItem] | TodoItem, git_provider, gfm_supported) -> str:
    markdown_text = ""
    MAX_ITEMS = 5 # limit the number of items to display
    if gfm_supported:
        if isinstance(value, list):
            markdown_text += "<ul>\n"
            if len(value) > MAX_ITEMS:
                get_logger().debug(f"Truncating todo items to {MAX_ITEMS} items")
                value = value[:MAX_ITEMS]
            for todo_item in value:
                markdown_text += f"<li>{format_todo_item(todo_item, git_provider, gfm_supported)}</li>\n"
            markdown_text += "</ul>\n"
        else:
            markdown_text += f"<p>{format_todo_item(value, git_provider, gfm_supported)}</p>\n"
    else:
        if isinstance(value, list):
            if len(value) > MAX_ITEMS:
                get_logger().debug(f"Truncating todo items to {MAX_ITEMS} items")
                value = value[:MAX_ITEMS]
            for todo_item in value:
                markdown_text += f"- {format_todo_item(todo_item, git_provider, gfm_supported)}\n"
        else:
            markdown_text += f"- {format_todo_item(value, git_provider, gfm_supported)}\n"
    return markdown_text
