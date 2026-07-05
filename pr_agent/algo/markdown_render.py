"""Markdown rendering of tool outputs (review tables, code suggestions, todo items)."""

import html
import textwrap

from pr_agent.algo.git_patch_processing import extract_hunk_lines_from_patch
from pr_agent.algo.types import PRReviewHeader, TodoItem
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


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
        # local import: set_file_languages lives in utils, which re-exports this module
        from pr_agent.algo.utils import set_file_languages

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


def replace_code_tags(text):
    """
    Replace odd instances of ` with <code> and even instances of ` with </code>
    """
    text = html.escape(text)
    parts = text.split('`')
    for i in range(1, len(parts), 2):
        parts[i] = '<code>' + parts[i] + '</code>'
    return ''.join(parts)


def is_value_no(value):
    if not value:
        return True
    value_str = str(value).strip().lower()
    if value_str == 'no' or value_str == 'none' or value_str == 'false':
        return True
    return False


def format_todo_item(todo_item: TodoItem, git_provider, gfm_supported) -> str:
    relevant_file = todo_item.get('relevant_file', '').strip()
    line_number = todo_item.get('line_number', '')
    content = todo_item.get('content', '')
    reference_link = git_provider.get_line_link(relevant_file, line_number, line_number) if git_provider else ""
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
