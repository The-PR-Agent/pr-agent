from __future__ import annotations

import copy
import difflib
import html
import json
import re
import textwrap
from datetime import datetime
from enum import Enum
from typing import Any, List, Tuple, TypedDict
from urllib.parse import quote, unquote

import html2text
import yaml
from pydantic import BaseModel

import pr_agent.algo.comment_identity as _ci
from pr_agent.algo.git_patch_processing import (
    extract_hunk_headers,
    extract_hunk_lines_from_patch,
    to_hunk_only_patch,
)
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings, get_verbosity_level
from pr_agent.log import get_logger

_ENCODED_USER_TEXT_PREFIX = "__pr_agent_encoded_text__:"


def encode_user_text_arg(value: str) -> str:
    return _ENCODED_USER_TEXT_PREFIX + quote(value, safe="")


def decode_user_text_args(args: List[str] | None) -> str:
    if not args:
        return ""
    return " ".join(
        unquote(arg[len(_ENCODED_USER_TEXT_PREFIX):])
        if arg.startswith(_ENCODED_USER_TEXT_PREFIX)
        else arg
        for arg in args
    )


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


class ReasoningEffort(str, Enum):
    MAX = "max"
    XHIGH = "xhigh"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MINIMAL = "minimal"
    NONE = "none"


def _expand_minute_suffix(text: str) -> str:
    """Replace minute abbreviations like '30m' with '30 minutes'.

    Only replaces when 'm' appears at a word boundary after digits
    (e.g. "30m" -> "30 minutes"), leaving partial-unit strings like
    "30ms" or "30min" unchanged.
    """
    return re.sub(r'(\d+)m\b', r'\1 minutes', text)


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
        "Risk level": "⚠️",
        "Merge recommendation": "✅",
        "Review priority files": "📂",
    }
    markdown_text = ""
    markdown_text += f"{_ci.format_pr_review_header(incremental=bool(incremental_review))}\n\n"
    if incremental_review:
        markdown_text += f"⏮️ Review for commits since previous PR-Agent review {incremental_review}.\n\n"
    if not output_data or not output_data.get('review', {}):
        return ""

    if get_settings().get("pr_reviewer.enable_intro_text", False):
        markdown_text += "Here are some key observations to aid the review process:\n\n"

    if gfm_supported:
        markdown_text += "<table>\n"

    review_data = {k: v for k, v in output_data["review"].items() if k != "todo_summary"}
    for key, value in review_data.items():
        if value is None or value == '' or value == {} or value == []:
            if key.lower() not in ['can_be_split', 'key_issues_to_review', 'review_priority_files']:
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
            value_int = max(1, min(5, value_int))
            blue_bars = '🔵' * value_int
            white_bars = '⚪' * (5 - value_int)
            value = f"{value_int} {blue_bars}{white_bars}"
            if gfm_supported:
                markdown_text += "<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong>: {value}"
                markdown_text += "</td></tr>\n"
            else:
                markdown_text += f"### {emoji} {key_nice}: {value}\n\n"
        elif 'relevant tests' in key_nice.lower():
            value = str(value).strip().lower()
            if gfm_supported:
                markdown_text += "<tr><td>"
                if is_value_no(value):
                    markdown_text += f"{emoji}&nbsp;<strong>No relevant tests</strong>"
                else:
                    markdown_text += f"{emoji}&nbsp;<strong>PR contains tests</strong>"
                markdown_text += "</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += f'### {emoji} No relevant tests\n\n'
                else:
                    markdown_text += f"### {emoji} PR contains tests\n\n"
        elif 'ticket compliance check' in key_nice.lower():
            markdown_text = ticket_markdown_logic(emoji, markdown_text, value, gfm_supported)
        elif 'contribution time cost estimate' in key_nice.lower():
            if not isinstance(value, dict) or not all(
                    isinstance(value.get(case), str)
                    for case in ("best_case", "average_case", "worst_case")):
                get_logger().warning("Skipping malformed contribution time estimate",
                                     artifact={"value": value})
                continue
            if gfm_supported:
                markdown_text += \
                    f"<tr><td>{emoji}&nbsp;<strong>Contribution time estimate</strong> (best, average, worst case): "
                best = _expand_minute_suffix(value['best_case'])
                avg = _expand_minute_suffix(value['average_case'])
                worst = _expand_minute_suffix(value['worst_case'])
                markdown_text += f"{best} | {avg} | {worst}"
                markdown_text += "</td></tr>\n"
            else:
                markdown_text += f"### {emoji} Contribution time estimate (best, average, worst case): "
                best = _expand_minute_suffix(value['best_case'])
                avg = _expand_minute_suffix(value['average_case'])
                worst = _expand_minute_suffix(value['worst_case'])
                markdown_text += f"{best} | {avg} | {worst}\n\n"
        elif 'security concerns' in key_nice.lower():
            if gfm_supported:
                markdown_text += "<tr><td>"
                if is_value_no(value):
                    markdown_text += f"{emoji}&nbsp;<strong>No security concerns identified</strong>"
                else:
                    markdown_text += f"{emoji}&nbsp;<strong>Security concerns</strong><br><br>\n\n"
                    value = _ci.emphasize_header(value.strip()) if isinstance(value, str) else _ci.as_review_text(value)
                    markdown_text += f"{value}"
                markdown_text += "</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += f'### {emoji} No security concerns identified\n\n'
                else:
                    markdown_text += f"### {emoji} Security concerns\n\n"
                    value = _ci.emphasize_header(
                        value.strip(), only_markdown=True) if isinstance(value, str) else _ci.as_review_text(value)
                    markdown_text += f"{value}\n\n"
        elif 'risk level' in key_nice.lower():
            risk_value = str(value).strip().lower().replace("_", " ")
            risk_display = risk_value.capitalize() if risk_value else "Unknown"
            if gfm_supported:
                markdown_text += "<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>Risk level</strong>: {risk_display}"
                markdown_text += "</td></tr>\n"
            else:
                markdown_text += f"### {emoji} Risk level: {risk_display}\n\n"
        elif 'merge recommendation' in key_nice.lower():
            recommendation = str(value).strip().replace("_", " ")
            recommendation_display = recommendation.capitalize() if recommendation else "Unknown"
            if gfm_supported:
                markdown_text += "<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>Merge recommendation</strong>: {recommendation_display}"
                markdown_text += "</td></tr>\n"
            else:
                markdown_text += f"### {emoji} Merge recommendation: {recommendation_display}\n\n"
        elif 'review priority files' in key_nice.lower():
            priority_files = []
            if isinstance(value, list):
                priority_files = [str(priority_file).strip() for priority_file in value if str(priority_file).strip()]
            if gfm_supported:
                markdown_text += "<tr><td>"
                if not priority_files:
                    markdown_text += f"{emoji}&nbsp;<strong>Priority files</strong>: None"
                else:
                    markdown_text += f"{emoji}&nbsp;<strong>Priority files</strong>\n<br><br>\n"
                    markdown_text += "<ul>\n"
                    for priority_file in priority_files:
                        markdown_text += f"<li>{priority_file}</li>\n"
                    markdown_text += "</ul>\n"
                markdown_text += "</td></tr>\n"
            else:
                if not priority_files:
                    markdown_text += f"### {emoji} Priority files: None\n\n"
                else:
                    markdown_text += f"### {emoji} Priority files\n\n"
                    for priority_file in priority_files:
                        markdown_text += f"- {priority_file}\n"
                    markdown_text += "\n"
        elif 'todo sections' in key_nice.lower():
            if gfm_supported:
                markdown_text += "<tr><td>"
                if is_value_no(value):
                    markdown_text += "✅&nbsp;<strong>No TODO sections</strong>"
                else:
                    markdown_todo_items = format_todo_items(value, git_provider, gfm_supported)
                    markdown_text += f"{emoji}&nbsp;<strong>TODO sections</strong>\n<br><br>\n"
                    markdown_text += markdown_todo_items
                markdown_text += "</td></tr>\n"
            else:
                if is_value_no(value):
                    markdown_text += "### ✅ No TODO sections\n\n"
                else:
                    markdown_todo_items = format_todo_items(value, git_provider, gfm_supported)
                    markdown_text += f"### {emoji} TODO sections\n\n"
                    markdown_text += markdown_todo_items
        elif 'can be split' in key_nice.lower():
            if gfm_supported:
                markdown_text += "<tr><td>"
                markdown_text += process_can_be_split(emoji, value)
                markdown_text += "</td></tr>\n"
        elif 'key issues to review' in key_nice.lower():
            # value is a list of issues
            if is_value_no(value):
                if gfm_supported:
                    markdown_text += "<tr><td>"
                    markdown_text += f"{emoji}&nbsp;<strong>No major issues detected</strong>"
                    markdown_text += "</td></tr>\n"
                else:
                    markdown_text += f"### {emoji} No major issues detected\n\n"
            else:
                issues = value
                if gfm_supported:
                    markdown_text += "<tr><td>"
                    # markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong><br><br>\n\n"
                    markdown_text += f"{emoji}&nbsp;<strong>Recommended focus areas for review</strong><br><br>\n\n"
                else:
                    markdown_text += f"### {emoji} Recommended focus areas for review\n\n#### \n"
                for issue in issues:
                    try:
                        if not issue or not isinstance(issue, dict):
                            continue
                        if any(
                            field in issue and not isinstance(issue[field], str)
                            for field in ('relevant_file', 'issue_header', 'issue_content')
                        ):
                            continue
                        relevant_file = issue.get('relevant_file', '').strip()
                        issue_header = issue.get('issue_header', '').strip()
                        if issue_header.lower() == 'possible bug':
                            issue_header = 'Possible Issue'  # Make the header less frightening
                        issue_content = issue.get('issue_content', '').strip()
                        try:
                            start_line = int(str(issue.get('start_line', 0)).strip())
                            end_line = int(str(issue.get('end_line', 0)).strip())
                        except (TypeError, ValueError):
                            start_line, end_line = 0, 0
                        valid_lines = start_line > 0 and end_line >= start_line
                        relevant_lines_str = extract_relevant_lines_str(
                            end_line, files, relevant_file, start_line, dedent=True) if valid_lines else ""
                        if git_provider and valid_lines:
                            reference_link = git_provider.get_line_link(relevant_file, start_line, end_line)
                        else:
                            reference_link = None

                        if gfm_supported:
                            if reference_link is not None and len(reference_link) > 0:
                                if relevant_lines_str:
                                    issue_str = (
                                        f"<details><summary><a href='{reference_link}'>"
                                        f"<strong>{issue_header}</strong></a>\n\n"
                                        f"{issue_content}\n</summary>\n\n"
                                        f"{relevant_lines_str}\n\n</details>"
                                    )
                                else:
                                    issue_str = (
                                        f"<a href='{reference_link}'>"
                                        f"<strong>{issue_header}</strong></a><br>{issue_content}"
                                    )
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
                    markdown_text += "</td></tr>\n"
        else:
            if gfm_supported:
                markdown_text += "<tr><td>"
                markdown_text += f"{emoji}&nbsp;<strong>{key_nice}</strong>: {value}"
                markdown_text += "</td></tr>\n"
            else:
                markdown_text += f"### {emoji} {key_nice}: {value}\n\n"

    if gfm_supported:
        markdown_text += "</table>\n"

    return markdown_text


def extract_relevant_lines_str(end_line, files, relevant_file, start_line, dedent=False) -> str:
    """
    Finds 'relevant_file' in 'files', and extracts the lines from 'start_line' to 'end_line' str from the file content.
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
                        get_logger().info(
                            f"No content found in file: '{file.filename}' for 'extract_relevant_lines_str'. "
                            f"Using patch instead")
                        _, selected_lines = extract_hunk_lines_from_patch(
                            patch, file.filename, start_line, end_line,side="right")
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

    if isinstance(value, dict):
        value = [value]
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

                if not fully_compliant_str and not not_compliant_str and not requires_further_human_verification:
                    get_logger().debug("Ticket compliance has no requirements",
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
                elif requires_further_human_verification:
                    ticket_compliance_level = 'PR Code Verified'

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
                ticket_compliance_str += (
                    f"\n\n**[{ticket_url.split('/')[-1]}]({ticket_url}) - "
                    f"{ticket_compliance_level}**\n\n{explanation}\n\n"
                )

                # for debugging
                if requires_further_human_verification:
                    get_logger().debug(
                        "Ticket compliance requires further human verification",
                        artifact={
                            "ticket_url": ticket_url,
                            "requires_further_human_verification": requires_further_human_verification,
                            "compliance_level": ticket_compliance_level,
                        },
                    )

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
            markdown_text += "<tr><td>\n\n"
            markdown_text += f"**{emoji} Ticket compliance analysis {compliance_emoji}**\n\n"
            markdown_text += ticket_compliance_str
            markdown_text += "</td></tr>\n"
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
            for split in value:
                title = split.get('title', '')
                relevant_files = split.get('relevant_files', [])
                markdown_text += f"<details><summary>\nSub-PR theme: <b>{title}</b></summary>\n\n"
                markdown_text += "___\n\nRelevant files:\n\n"
                for file in relevant_files:
                    markdown_text += f"- {file}\n"
                markdown_text += "___\n\n"
                markdown_text += "</details>\n\n"

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
            #         markdown_text += (
            #             f"<td><details><summary>\n"
            #             f"Sub-PR theme:<br><strong>{title}</strong></summary>\n\n"
            #         )
            #         markdown_text += f"<hr>\n"
            #         markdown_text += f"Relevant files:\n"
            #         markdown_text += f"<ul>\n"
            #         for file in relevant_files:
            #             markdown_text += f"<li>{file}</li>\n"
            #         markdown_text += f"</ul>\n\n</details></td></tr>\n"
            #     else:
            #         markdown_text += (
            #             f"<tr>\n<td><details><summary>\n"
            #             f"Sub-PR theme:<br><strong>{title}</strong></summary>\n\n"
            #         )
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
                    markdown_text += "<tr><td>relevant line</td>"
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


def load_large_diff(filename, new_file_content_str: str,
                    original_file_content_str: str, show_warning: bool = True) -> str:
    """
    Generate a patch for a modified file by comparing the original content of the file with the new content provided as
    input. The returned patch starts at its first hunk and excludes unified-diff file metadata.
    """
    if not original_file_content_str and not new_file_content_str:
        return ""

    try:
        original_file_content_str = (original_file_content_str or "").rstrip() + "\n"
        new_file_content_str = (new_file_content_str or "").rstrip() + "\n"
        diff = difflib.unified_diff(original_file_content_str.splitlines(keepends=True),
                                    new_file_content_str.splitlines(keepends=True))
        if get_verbosity_level() >= 2 and show_warning:
            get_logger().info(f"File was modified, but no patch was found. Manually creating patch: {filename}.")
        return to_hunk_only_patch(''.join(diff))
    except Exception:
        get_logger().exception(f"Failed to generate patch for file: {filename}")
        return ""


def update_settings_from_args(args: List[str]) -> List[str]:
    """
    Update the settings of the Dynaconf object based on the arguments passed to the function.

    Args:
        args: A list of arguments passed to the function.
        Example args: ['--pr_code_suggestions.extra_instructions="be funny',
                  '--pr_code_suggestions.num_code_suggestions_per_chunk=3']

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


# Control characters that are unambiguously illegal in YAML and never carry meaningful information on their own
# (unlike the \x80-\x9f C1 range, which the "ninth fallback" below relies on being intact to repair latin-1/utf-8
# mojibake - see try_fix_yaml). LLM output occasionally contains a stray byte in this range (e.g. 0x08 BACKSPACE),
# most often introduced when an upstream diff-pruning step truncates the prompt mid multi-byte character, which
# makes PyYAML's strict reader raise `ReaderError: unacceptable character ...` before any fallback gets a chance
# to run. Stripping these characters up front is a cheap, purely-defensive step: they can never be part of valid
# YAML content, so removing them cannot turn a correct parse into an incorrect one.
_YAML_ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def sanitize_yaml_control_chars(text: str, log: bool = True) -> str:
    """Strip control characters that can never be part of valid YAML content and would otherwise make yaml.safe_load
    raise a ReaderError regardless of the document's structure.

    Note: this deliberately removes only a subset of PyYAML's non-printable set - C0 controls other than TAB/LF/CR,
    plus DEL. The \\x80-\\x9f C1 range is intentionally preserved because try_fix_yaml's latin-1->utf-8 fallback
    relies on those bytes to repair mojibake; they must survive to reach the repair logic.

    Set log=False to suppress the removal warning, e.g. when sanitizing a second, largely-overlapping copy of
    text that was already sanitized and logged once."""
    if not text:
        return text
    sanitized, count = _YAML_ILLEGAL_CHARS_RE.subn('', text)
    if count and log:
        get_logger().warning(
            f"Removed {count} unambiguous illegal control character(s) from AI prediction before YAML parsing")
    return sanitized


def _looks_like_more_answer(tail: str) -> bool:
    """Whether the text after the fence is more of the answer rather than a sign-off.

    Two signals, because each alone has a blind spot: a tail that parses as a mapping or a
    list is structured, but one that continues into prose does not parse at all and is
    only recognisable from the shape of its first line.
    """
    first_line = next((line for line in tail.split('\n') if line.strip()), '')
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*:(\s|$)', first_line):
        return True
    try:
        return isinstance(yaml.safe_load(tail), (dict, list))
    except Exception:
        return False


def drop_sign_off_after_wrapper_fence(text: str) -> str:
    """Drop a closing remark the model added after the wrapper's closing fence.

    The prompts ask for YAML "and nothing else", but the model sometimes signs off
    anyway. That either leaves the document unparseable or, for a single block
    scalar, parses the fence and the remark into the value.

    No existing fallback recovers it. The one that extracts a fenced block needs
    both fences, but most prompts end with an open fence for the model to continue
    from, so the reply carries only the closing one.
    """
    lines = text.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() != '```':
            continue
        tail = '\n'.join(lines[i + 1:])
        if not tail.strip():
            return text
        if _looks_like_more_answer(tail):
            # Dropping it would publish a partial answer, where the parse failure it
            # replaces at least triggers a retry.
            return text
        candidate = '\n'.join(lines[:i])
        try:
            if isinstance(yaml.safe_load(candidate), dict):
                return candidate
        except Exception:
            pass
        return text
    return text


def load_yaml(response_text: str, keys_fix_yaml: List[str] | None = None, first_key="", last_key="") -> dict:
    if keys_fix_yaml is None:
        keys_fix_yaml = []
    response_text_original = copy.deepcopy(response_text)
    response_text = response_text.strip('\n')
    # strip the fence label only when it is a complete info string, so a key such as
    # "yml_config" is not truncated to "_config"
    unfenced = re.sub(r'^```[ \t]*(?:(?i:yaml|yml))?[ \t]*(?=\r?\n)', '', response_text)
    if unfenced == response_text:
        unfenced = response_text.removeprefix('yaml')
    response_text = unfenced.rstrip()
    response_text = drop_sign_off_after_wrapper_fence(response_text)
    if response_text.split('\n')[-1] == '```':
        response_text = response_text.removesuffix('```')
    response_text = sanitize_yaml_control_chars(response_text)
    response_text_original_sanitized = sanitize_yaml_control_chars(response_text_original, log=False)
    try:
        # yaml.safe_load('') / yaml.safe_load(' ') returns None without raising, so a response that was
        # non-empty before preprocessing/sanitization but is blank afterwards (e.g. it consisted entirely of
        # illegal control characters) would otherwise silently produce None here — skipping every fallback
        # below and every log line — and then blow up in a caller that assumes a dict. Route this case
        # through the same exception handling as a normal parse failure instead.
        if response_text_original.strip() and not response_text.strip():
            raise ValueError("Preprocessing/sanitization removed all content from a non-empty AI prediction")
        data = yaml.safe_load(response_text)
    except Exception as e:
        get_logger().warning(f"Initial failure to parse AI prediction: {e}")
        data = try_fix_yaml(response_text, keys_fix_yaml=keys_fix_yaml, first_key=first_key, last_key=last_key,
                            response_text_original=response_text_original_sanitized)
        if not data:
            get_logger().error("Failed to parse AI prediction after fallbacks",
                               artifact={'response_text': response_text})
        else:
            get_logger().info("Successfully parsed AI prediction after fallbacks",
                              artifact={'response_text': response_text})
    if data is None:
        return {}
    return data



def try_fix_yaml(response_text: str,
                 keys_fix_yaml: List[str] | None = None,
                 first_key="",
                 last_key="",
                 response_text_original="") -> dict:
    if keys_fix_yaml is None:
        keys_fix_yaml = []
    response_text_lines = response_text.split('\n')

    keys_yaml = ['relevant line:', 'suggestion content:', 'relevant file:', 'existing code:',
                 'improved code:', 'label:', 'why:', 'suggestion_summary:']
    keys_yaml = keys_yaml + keys_fix_yaml

    # first fallback - try to convert 'relevant line: ...' to relevant line: |-\n        ...'
    response_text_lines_copy = response_text_lines.copy()
    for i in range(0, len(response_text_lines_copy)):
        for key in keys_yaml:
            if key in response_text_lines_copy[i] and "|" not in response_text_lines_copy[i]:
                response_text_lines_copy[i] = response_text_lines_copy[i].replace(f'{key}',
                                                                                  f'{key} |\n        ')
    try:
        data = yaml.safe_load('\n'.join(response_text_lines_copy))
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after adding |-\n")
            return data
    except:
        pass

    # 1.5 fallback - try to convert '|' to '|2'. Will solve cases of indent decreasing during the code
    response_text_copy = copy.deepcopy(response_text)
    response_text_copy = response_text_copy.replace('|\n', '|2\n')
    try:
        data = yaml.safe_load(response_text_copy)
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after replacing | with |2")
            return data
    except:
        pass
    # try to add spaces to lines that are not indented properly, and contain '}'.
    # Moved out of the except block so it also runs when safe_load returned None (e.g. empty input).
    response_text_lines_copy = response_text_copy.split('\n')
    for i in range(0, len(response_text_lines_copy)):
        initial_space = len(response_text_lines_copy[i]) - len(response_text_lines_copy[i].lstrip())
        if initial_space == 2 and '|2' not in response_text_lines_copy[i] and '}' in response_text_lines_copy[i]:
            if response_text_lines_copy[i].strip() == '}':
                # Only move a standalone brace into the block scalar when it closes an earlier opening brace.
                block_scalar_lines = []
                should_indent = False
                for previous_line in reversed(response_text_lines_copy[:i]):
                    if not previous_line.strip():
                        block_scalar_lines.append(previous_line)
                        continue
                    previous_space = len(previous_line) - len(previous_line.lstrip())
                    if previous_space < initial_space:
                        break
                    if previous_space == initial_space:
                        if re.search(r':\s*\|[0-9+-]*\s*$', previous_line):
                            block_scalar = '\n'.join(reversed(block_scalar_lines))
                            should_indent = '{' in block_scalar or '}' in block_scalar
                        break
                    block_scalar_lines.append(previous_line)
                if not should_indent:
                    response_text_lines_copy[i] = ''
                    continue
            response_text_lines_copy[i] = '    ' + response_text_lines_copy[i].lstrip()
    try:
        data = yaml.safe_load('\n'.join(response_text_lines_copy))
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after replacing | with |2 and adding spaces")
            return data
    except:
        pass

    # second fallback - try to extract only range from first ```yaml to the last ```
    snippet_pattern = r'```[ \t]*(?:(?i:yaml|yml)[ \t]*)?\r?\n([\s\S]*?)```(?=\s*$|")'
    snippet = re.search(snippet_pattern, '\n'.join(response_text_lines_copy))
    if not snippet:
        snippet = re.search(snippet_pattern, response_text_original) # before we removed the "```"
    if snippet:
        # group(1) is the snippet body, without the ``` fences or the optional yaml/yml language identifier
        snippet_text = snippet.group(1)
        try:
            data = yaml.safe_load(snippet_text)
            if data is not None:
                get_logger().info("Successfully parsed AI prediction after extracting yaml snippet")
                return data
        except Exception as e:
            get_logger().debug(f"Failed to parse AI prediction after extracting yaml snippet: {e}")


    # third fallback - try to remove leading and trailing curly brackets
    response_text_copy = response_text.strip().rstrip().removeprefix('{').removesuffix('}').rstrip(':\n')
    try:
        data = yaml.safe_load(response_text_copy)
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after removing curly brackets")
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
        response_text_copy = response_text[index_start:index_end].strip()
        for fence in ("\n```yaml", "\n```yml"):
            if response_text_copy[-len(fence):].lower() == fence:
                response_text_copy = response_text_copy[: -len(fence)]
                break
        response_text_copy = response_text_copy.strip("`").strip()
        if response_text_copy:
            try:
                data = yaml.safe_load(response_text_copy)
                if data is not None:
                    get_logger().info("Successfully parsed AI prediction after extracting yaml snippet")
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
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after removing leading '+'")
            return data
    except:
        pass

    # 5.5 fallback - try to normalize diff-style removal markers ('-') within list items
    response_text_lines_copy = response_text_lines.copy()
    modified = False

    for i, line in enumerate(response_text_lines_copy):
        if line.startswith('+'):
            response_text_lines_copy[i] = ' ' + line[1:]
            modified = True

    # normalize lines starting with '-'. Distinguish real YAML list items from diff deletions.
    for i, line in enumerate(response_text_lines_copy):
        if not line.startswith('-'):
            continue

        remainder = line[1:]
        if line.startswith('- '):
            second_char = remainder[1] if len(remainder) > 1 else ''
            if second_char and second_char not in (' ', '\t', '+', '-'):
                continue # real list item → keep as-is

        # treat it as a diff "removed" marker inside block content
        cleaned = remainder
        while cleaned and cleaned[0] in ('+', '-'):
            cleaned = cleaned[1:]
        if cleaned and cleaned[0] not in (' ', '\t'):
            cleaned = ' ' + cleaned
        if cleaned != line:
            response_text_lines_copy[i] = cleaned
            modified = True
    if modified:
        try:
            data = yaml.safe_load('\n'.join(response_text_lines_copy))
            if data is not None:
                get_logger().info("Successfully parsed AI prediction after normalizing diff removal markers")
                return data
        except Exception:
            pass


    # sixth fallback - replace tabs with spaces
    if '\t' in response_text:
        response_text_copy = copy.deepcopy(response_text)
        response_text_copy = response_text_copy.replace('\t', '    ')
        try:
            data = yaml.safe_load(response_text_copy)
            if data is not None:
                get_logger().info("Successfully parsed AI prediction after replacing tabs with spaces")
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
        elif (line_stripped.endswith(': |')
              or line_stripped.endswith(': |-')
              or line_stripped.endswith(': |2')
              or any(line_stripped.endswith(key) for key in keys_yaml)):
            start_line = -1
        elif start_line != -1:
            response_text_copy_lines[i] = '    ' + line
    response_text_copy = '\n'.join(response_text_copy_lines)
    response_text_copy = response_text_copy.replace(' |\n', ' |2\n')
    try:
        data = yaml.safe_load(response_text_copy)
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after adding indent for sections of code blocks")
            return data
    except:
        pass

    # eighth fallback - try to remove pipe chars at the root-level dicts
    response_text_copy = copy.deepcopy(response_text)
    response_text_copy = response_text_copy.lstrip('|\n')
    try:
        data = yaml.safe_load(response_text_copy)
        if data is not None:
            get_logger().info("Successfully parsed AI prediction after removing pipe chars")
            return data
    except:
        pass

    # ninth fallback - try to decode the response text with different encodings.
    # GPT-5 can return text that is not utf-8 encoded.
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
        description = v.get('description', '') if isinstance(v, dict) else str(v)
        description = "'" + description.strip('\n').replace('\n', '\\n').replace("'", "\\'") + "'"
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
                skip_hunk = False
                for i, line in enumerate(patch_lines):
                    # new hunk
                    if line.startswith('@@'):
                        delta = 0
                        match = re_hunk_header.match(line)
                        if match:
                            skip_hunk = False
                            section_header, size1, size2, start1, start2 = extract_hunk_headers(match)
                        else:
                            # combined/merge hunk headers (e.g. '@@@ ... @@@') cannot be anchored,
                            # so skip the whole hunk instead of crashing
                            get_logger().warning("Skipping a line that starts with '@@' but is not a "
                                                 "unified hunk header", artifact={"line": line})
                            skip_hunk = True
                            continue
                    elif skip_hunk:
                        continue
                    elif not line.startswith('-'):
                        delta += 1

                    #
                    absolute_position_curr = start2 + delta - 1

                    if absolute_position_curr == absolute_position:
                        position = i
                        break
            elif not relevant_line_in_file:
                get_logger().warning("Cannot locate an empty relevant line in a patch",
                                     artifact={"relevant_file": relevant_file})
                continue
            else:
                # try to find the line in the patch using difflib, with some margin of error
                matches_difflib: list[str | Any] = difflib.get_close_matches(relevant_line_in_file,
                                                                             patch_lines, n=3, cutoff=0.93)
                if len(matches_difflib) == 1 and matches_difflib[0].startswith('+'):
                    relevant_line_in_file = matches_difflib[0]


                def scan_patch_lines(is_match):
                    scan_delta = 0
                    scan_start2 = 0
                    skip_hunk = False
                    for i, line in enumerate(patch_lines):
                        if line.startswith('@@'):
                            scan_delta = 0
                            header_match = re_hunk_header.match(line)
                            if header_match:
                                skip_hunk = False
                                *_, scan_start2 = extract_hunk_headers(header_match)
                            else:
                                skip_hunk = True
                                get_logger().warning("Skipping a line that starts with '@@' but is not a "
                                                     "unified hunk header", artifact={"line": line})
                                continue
                        elif skip_hunk:
                            continue
                        elif not line.startswith('-'):
                            scan_delta += 1

                        if not line.startswith('-') and is_match(line):
                            return i, scan_start2 + scan_delta - 1
                    return -1, absolute_position

                position, absolute_position = scan_patch_lines(
                    lambda line: line == relevant_line_in_file or line[1:] == relevant_line_in_file)
                if position == -1:
                    position, absolute_position = scan_patch_lines(
                        lambda line: relevant_line_in_file in line)

                if position == -1 and relevant_line_in_file[0] == '+':
                    no_plus_line = relevant_line_in_file[1:].lstrip()
                    skip_hunk = False
                    for i, line in enumerate(patch_lines):
                        if line.startswith('@@'):
                            delta = 0
                            match = re_hunk_header.match(line)
                            if match:
                                skip_hunk = False
                                section_header, size1, size2, start1, start2 = extract_hunk_headers(match)
                            else:
                                get_logger().warning("Skipping a line that starts with '@@' but is not a "
                                                     "unified hunk header", artifact={"line": line})
                                skip_hunk = True
                                continue
                        elif skip_hunk:
                            continue
                        elif not line.startswith('-'):
                            delta += 1

                        if no_plus_line in line and line[0] != '-':
                            # The model might add a '+' to the beginning of the relevant_line_in_file even if originally
                            # it's a context line
                            position = i
                            absolute_position = start2 + delta - 1
                            break
    return position, absolute_position


def is_value_no(value):
    if not value:
        return True
    value_str = str(value).strip().lower()
    if value_str == 'no' or value_str == 'none' or value_str == 'false':
        return True
    return False


def process_description(description_full: str) -> Tuple[str, List]:
    if not description_full:
        return "", []

    # description_split = description_full.split(_ci.PRDescriptionHeader.FILE_WALKTHROUGH.value)
    if _ci.PRDescriptionHeader.FILE_WALKTHROUGH.value in description_full:
        try:
            # FILE_WALKTHROUGH are presented in a collapsible section in the description
            regex_pattern = (r"<details.*?>\s*<summary>\s*<h3>\s*"
                             + re.escape(_ci.PRDescriptionHeader.FILE_WALKTHROUGH.value) + r"\s*</h3>\s*</summary>")
            description_split = re.split(regex_pattern, description_full, maxsplit=1, flags=re.DOTALL)

            # If the regex pattern is not found, fallback to the previous method
            if len(description_split) == 1:
                get_logger().debug("Could not find regex pattern for file walkthrough, falling back to simple split")
                description_split = description_full.split(_ci.PRDescriptionHeader.FILE_WALKTHROUGH.value, 1)
        except Exception as e:
            get_logger().warning(f"Failed to split description using regex, falling back to simple split: {e}")
            description_split = description_full.split(_ci.PRDescriptionHeader.FILE_WALKTHROUGH.value, 1)

        if len(description_split) < 2:
            get_logger().error("Failed to split description into base and changes walkthrough",
                               artifact={"description": description_full})
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
                    pattern = (r'<details>\s*<summary><strong>(.*?)</strong>\s*<dd><code>(.*?)</code>.*?'
                               r'</summary>\s*<hr>\s*(.*?)\s*(?:<li>|•)(.*?)</details>')
                    res = re.search(pattern, file_data, re.DOTALL)
                    if not res or res.lastindex != 4:
                        pattern_back = (r'<details>\s*<summary><strong>(.*?)</strong><dd><code>(.*?)</code>.*?'
                                        r'</summary>\s*<hr>\s*(.*?)\n\n\s*(.*?)</details>')
                        res = re.search(pattern_back, file_data, re.DOTALL)
                    if not res or res.lastindex != 4:
                        # looking for hyphen ('- ')
                        pattern_back = (r'<details>\s*<summary><strong>(.*?)</strong>\s*<dd><code>(.*?)</code>.*?'
                                        r'</summary>\s*<hr>\s*(.*?)\s*-\s*(.*?)\s*</details>')
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

def format_todo_item(todo_item: TodoItem | str, git_provider, gfm_supported) -> str:
    """Render one TODO entry, tolerating the free-text form the schema also allows.

    todo_sections is declared as Union[List[TodoSection], str], so a model may summarise the
    TODOs in prose instead of locating each one. Such an entry has no file to link to.
    """
    if not isinstance(todo_item, dict):
        return str(todo_item).strip() if todo_item is not None else ""
    relevant_file = str(todo_item.get('relevant_file', '') or '').strip()
    line_number = todo_item.get('line_number', '')
    content = str(todo_item.get('content', '') or '')
    if not relevant_file:
        return content.strip()
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


def format_todo_items(value: list[TodoItem] | TodoItem | str, git_provider, gfm_supported) -> str:
    markdown_text = ""
    MAX_ITEMS = 5 # limit the number of items to display
    is_list = isinstance(value, list)
    items = value if is_list else [value]
    if len(items) > MAX_ITEMS:
        get_logger().debug(f"Truncating todo items to {MAX_ITEMS} items")
        items = items[:MAX_ITEMS]
    entries = [format_todo_item(todo_item, git_provider, gfm_supported) for todo_item in items]
    entries = [entry for entry in entries if entry]
    if not entries:
        return markdown_text
    if gfm_supported:
        if not is_list:
            return f"<p>{entries[0]}</p>\n"
        markdown_text += "<ul>\n"
        for entry in entries:
            markdown_text += f"<li>{entry}</li>\n"
        markdown_text += "</ul>\n"
    else:
        for entry in entries:
            markdown_text += f"- {entry}\n"
    return markdown_text
