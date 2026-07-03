"""Repair helpers for malformed JSON/YAML model output."""

import copy
import json
import re
from typing import List

import yaml

from pr_agent.log import get_logger


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

