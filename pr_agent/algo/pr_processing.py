from __future__ import annotations

import traceback
from typing import Callable, List, Tuple

from github import RateLimitExceededException

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.git_patch_processing import (
    extend_patch, handle_patch_deletions,
    decouple_and_convert_to_hunks_with_lines_numbers)
from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import ModelType, clip_tokens, get_max_tokens, get_model
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

DELETED_FILES_ = "Deleted files:\n"

MORE_MODIFIED_FILES_ = "Additional modified files (insufficient token budget to process):\n"

ADDED_FILES_ = "Additional added files (insufficient token budget to process):\n"

OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD = 1500
OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD = 1000
MAX_EXTRA_LINES = 10


def cap_and_log_extra_lines(value, direction) -> int:
    if value > MAX_EXTRA_LINES:
        get_logger().warning(f"patch_extra_lines_{direction} was {value}, capping to {MAX_EXTRA_LINES}")
        return MAX_EXTRA_LINES
    return value


def get_pr_diff(git_provider: GitProvider, token_handler: TokenHandler,
                model: str,
                add_line_numbers_to_hunks: bool = False,
                disable_extra_lines: bool = False,
                large_pr_handling=False,
                return_remaining_files=False):
    if disable_extra_lines:
        PATCH_EXTRA_LINES_BEFORE = 0
        PATCH_EXTRA_LINES_AFTER = 0
    else:
        PATCH_EXTRA_LINES_BEFORE = get_settings().config.patch_extra_lines_before
        PATCH_EXTRA_LINES_AFTER = get_settings().config.patch_extra_lines_after
        PATCH_EXTRA_LINES_BEFORE = cap_and_log_extra_lines(PATCH_EXTRA_LINES_BEFORE, "before")
        PATCH_EXTRA_LINES_AFTER = cap_and_log_extra_lines(PATCH_EXTRA_LINES_AFTER, "after")

    try:
        diff_files = git_provider.get_diff_files()
    except RateLimitExceededException as e:
        get_logger().error(f"Rate limit exceeded for git provider API. original message {e}")
        raise

    # get pr languages
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)
    if pr_languages:
        try:
            get_logger().info(f"PR main language: {pr_languages[0]['language']}")
        except Exception as e:
            pass

    # generate a standard diff string, with patch extension
    patches_extended, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
        pr_languages, token_handler, add_line_numbers_to_hunks,
        patch_extra_lines_before=PATCH_EXTRA_LINES_BEFORE, patch_extra_lines_after=PATCH_EXTRA_LINES_AFTER)

    # if we are under the limit, return the full diff
    if total_tokens + OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD < get_max_tokens(model):
        get_logger().info(f"Tokens: {total_tokens}, total tokens under limit: {get_max_tokens(model)}, "
                          f"returning full diff.")
        return "\n".join(patches_extended)

    # if we are over the limit, start pruning (If we got here, we will not extend the patches with extra lines)
    get_logger().info(f"Tokens: {total_tokens}, total tokens over limit: {get_max_tokens(model)}, "
                      f"pruning diff.")
    patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list = \
        pr_generate_compressed_diff(pr_languages, token_handler, model, add_line_numbers_to_hunks, large_pr_handling)

    if large_pr_handling and len(patches_compressed_list) > 1:
        get_logger().info(f"Large PR handling mode, and found {len(patches_compressed_list)} patches with original diff.")
        return "" # return empty string, as we want to generate multiple patches with a different prompt

    # return the first patch
    patches_compressed = patches_compressed_list[0]
    total_tokens_new = total_tokens_list[0]
    files_in_patch = files_in_patches_list[0]

    # Insert additional information about added, modified, and deleted files if there is enough space
    max_tokens = get_max_tokens(model) - OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD
    curr_token = total_tokens_new  # == token_handler.count_tokens(final_diff)+token_handler.prompt_tokens
    final_diff = "\n".join(patches_compressed)
    delta_tokens = 10
    added_list_str = modified_list_str = deleted_list_str = ""
    unprocessed_files = []
    # generate the added, modified, and deleted files lists
    if (max_tokens - curr_token) > delta_tokens:
        for filename, file_values in file_dict.items():
            if filename in files_in_patch:
                continue
            if file_values['edit_type'] == EDIT_TYPE.ADDED:
                unprocessed_files.append(filename)
                if not added_list_str:
                    added_list_str = ADDED_FILES_ + f"\n{filename}"
                else:
                    added_list_str = added_list_str + f"\n{filename}"
            elif file_values['edit_type'] in [EDIT_TYPE.MODIFIED, EDIT_TYPE.RENAMED]:
                unprocessed_files.append(filename)
                if not modified_list_str:
                    modified_list_str = MORE_MODIFIED_FILES_ + f"\n{filename}"
                else:
                    modified_list_str = modified_list_str + f"\n{filename}"
            elif file_values['edit_type'] == EDIT_TYPE.DELETED:
                # unprocessed_files.append(filename) # not needed here, because the file was deleted, so no need to process it
                if not deleted_list_str:
                    deleted_list_str = DELETED_FILES_ + f"\n{filename}"
                else:
                    deleted_list_str = deleted_list_str + f"\n{filename}"

    # prune the added, modified, and deleted files lists, and add them to the final diff
    added_list_str = clip_tokens(added_list_str, max_tokens - curr_token)
    if added_list_str:
        final_diff = final_diff + "\n\n" + added_list_str
        curr_token += token_handler.count_tokens(added_list_str) + 2
    modified_list_str = clip_tokens(modified_list_str, max_tokens - curr_token)
    if modified_list_str:
        final_diff = final_diff + "\n\n" + modified_list_str
        curr_token += token_handler.count_tokens(modified_list_str) + 2
    deleted_list_str = clip_tokens(deleted_list_str, max_tokens - curr_token)
    if deleted_list_str:
        final_diff = final_diff + "\n\n" + deleted_list_str

    get_logger().debug(f"After pruning, added_list_str: {added_list_str}, modified_list_str: {modified_list_str}, "
                       f"deleted_list_str: {deleted_list_str}")
    if not return_remaining_files:
        return final_diff
    else:
        return final_diff, remaining_files_list


def get_pr_diff_multiple_patchs(git_provider: GitProvider, token_handler: TokenHandler, model: str,
                add_line_numbers_to_hunks: bool = False, disable_extra_lines: bool = False):
    try:
        diff_files = git_provider.get_diff_files()
    except RateLimitExceededException as e:
        get_logger().error(f"Rate limit exceeded for git provider API. original message {e}")
        raise

    # get pr languages
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)
    if pr_languages:
        try:
            get_logger().info(f"PR main language: {pr_languages[0]['language']}")
        except Exception as e:
            pass

    patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list = \
        pr_generate_compressed_diff(pr_languages, token_handler, model, add_line_numbers_to_hunks, large_pr_handling=True)

    return patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list


def pr_generate_extended_diff(pr_languages: list,
                              token_handler: TokenHandler,
                              add_line_numbers_to_hunks: bool,
                              patch_extra_lines_before: int = 0,
                              patch_extra_lines_after: int = 0) -> Tuple[list, int, list]:
    total_tokens = token_handler.prompt_tokens  # initial tokens
    patches_extended = []
    patches_extended_tokens = []
    for lang in pr_languages:
        for file in lang['files']:
            original_file_content_str = file.base_file
            new_file_content_str = file.head_file
            patch = file.patch
            if not patch:
                continue

            # extend each patch with extra lines of context
            extended_patch = extend_patch(original_file_content_str, patch,
                                          patch_extra_lines_before, patch_extra_lines_after, file.filename,
                                          new_file_str=new_file_content_str)
            if not extended_patch:
                get_logger().warning(f"Failed to extend patch for file: {file.filename}")
                continue

            if add_line_numbers_to_hunks:
                full_extended_patch = decouple_and_convert_to_hunks_with_lines_numbers(extended_patch, file)
            else:
                extended_patch = extended_patch.replace('\n@@ ', '\n\n@@ ') # add extra line before each hunk
                full_extended_patch = f"\n\n## File: '{file.filename.strip()}'\n\n{extended_patch.strip()}\n"

            # add AI-summary metadata to the patch
            if file.ai_file_summary and get_settings().get("config.enable_ai_metadata", False):
                full_extended_patch = add_ai_summary_top_patch(file, full_extended_patch)

            patch_tokens = token_handler.count_tokens(full_extended_patch)
            file.tokens = patch_tokens
            total_tokens += patch_tokens
            patches_extended_tokens.append(patch_tokens)
            patches_extended.append(full_extended_patch)

    return patches_extended, total_tokens, patches_extended_tokens


def pr_generate_compressed_diff(top_langs: list, token_handler: TokenHandler, model: str,
                                convert_hunks_to_line_numbers: bool,
                                large_pr_handling: bool) -> Tuple[list, list, list, list, dict, list]:
    deleted_files_list = []

    # sort each one of the languages in top_langs by the number of tokens in the diff
    sorted_files = []
    for lang in top_langs:
        sorted_files.extend(sorted(lang['files'], key=lambda x: x.tokens, reverse=True))

    # generate patches for each file, and count tokens
    file_dict = {}
    for file in sorted_files:
        original_file_content_str = file.base_file
        new_file_content_str = file.head_file
        patch = file.patch
        if not patch:
            continue

        # removing delete-only hunks
        patch = handle_patch_deletions(patch, original_file_content_str,
                                       new_file_content_str, file.filename, file.edit_type)
        if patch is None:
            if file.filename not in deleted_files_list:
                deleted_files_list.append(file.filename)
            continue

        if convert_hunks_to_line_numbers:
            patch = decouple_and_convert_to_hunks_with_lines_numbers(patch, file)

        ## add AI-summary metadata to the patch (disabled, since we are in the compressed diff)
        # if file.ai_file_summary and get_settings().config.get('config.is_auto_command', False):
        #     patch = add_ai_summary_top_patch(file, patch)

        new_patch_tokens = token_handler.count_tokens(patch)
        file_dict[file.filename] = {'patch': patch, 'tokens': new_patch_tokens, 'edit_type': file.edit_type}

    max_tokens_model = get_max_tokens(model)

    # first iteration
    files_in_patches_list = []
    remaining_files_list =  [file.filename for file in sorted_files]
    patches_list =[]
    total_tokens_list = []
    total_tokens, patches, remaining_files_list, files_in_patch_list = generate_full_patch(convert_hunks_to_line_numbers, file_dict,
                                       max_tokens_model, remaining_files_list, token_handler)
    patches_list.append(patches)
    total_tokens_list.append(total_tokens)
    files_in_patches_list.append(files_in_patch_list)

    # additional iterations (if needed)
    if large_pr_handling:
        NUMBER_OF_ALLOWED_ITERATIONS = get_settings().pr_description.get("max_ai_calls", 4) - 1 # one more call is to summarize
        for i in range(NUMBER_OF_ALLOWED_ITERATIONS-1):
            if remaining_files_list:
                total_tokens, patches, remaining_files_list, files_in_patch_list = generate_full_patch(convert_hunks_to_line_numbers,
                                                                                 file_dict,
                                                                                  max_tokens_model,
                                                                                  remaining_files_list, token_handler)
                if patches:
                    patches_list.append(patches)
                    total_tokens_list.append(total_tokens)
                    files_in_patches_list.append(files_in_patch_list)
            else:
                break

    return patches_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list


def generate_full_patch(convert_hunks_to_line_numbers, file_dict, max_tokens_model,remaining_files_list_prev, token_handler):
    total_tokens = token_handler.prompt_tokens # initial tokens
    patches = []
    remaining_files_list_new = []
    files_in_patch_list = []
    for filename, data in file_dict.items():
        if filename not in remaining_files_list_prev:
            continue

        patch = data['patch']
        new_patch_tokens = data['tokens']
        edit_type = data['edit_type']

        # Hard Stop, no more tokens
        if total_tokens > max_tokens_model - OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD:
            get_logger().warning(f"File was fully skipped, no more tokens: {filename}.")
            continue

        # If the patch is too large, just show the file name
        if total_tokens + new_patch_tokens > max_tokens_model - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
            # Current logic is to skip the patch if it's too large
            # TODO: Option for alternative logic to remove hunks from the patch to reduce the number of tokens
            #  until we meet the requirements
            if get_settings().config.verbosity_level >= 2:
                get_logger().warning(f"Patch too large, skipping it: '{filename}'")
            remaining_files_list_new.append(filename)
            continue

        if patch:
            if not convert_hunks_to_line_numbers:
                patch_final = f"\n\n## File: '{filename.strip()}'\n\n{patch.strip()}\n"
            else:
                patch_final = "\n\n" + patch.strip()
            patches.append(patch_final)
            total_tokens += token_handler.count_tokens(patch_final)
            files_in_patch_list.append(filename)
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Tokens: {total_tokens}, last filename: {filename}")
    return total_tokens, patches, remaining_files_list_new, files_in_patch_list


async def retry_with_fallback_models(f: Callable, model_type: ModelType = ModelType.REGULAR):
    all_models = _get_all_models(model_type)
    all_deployments = _get_all_deployments(all_models)
    # try each (model, deployment_id) pair until one is successful, otherwise raise exception
    for i, (model, deployment_id) in enumerate(zip(all_models, all_deployments)):
        try:
            get_logger().debug(
                f"Generating prediction with {model}"
                f"{(' from deployment ' + deployment_id) if deployment_id else ''}"
            )
            get_settings().set("openai.deployment_id", deployment_id)
            return await f(model)
        except Exception as e:
            get_logger().warning(
                f"Failed to generate prediction with {model}",
                artifact={"error": e},
            )
            if i == len(all_models) - 1:  # If it's the last iteration
                raise Exception(f"Failed to generate prediction with any model of {all_models}") from e


def _get_all_models(model_type: ModelType = ModelType.REGULAR) -> List[str]:
    if model_type == ModelType.WEAK:
        model = get_model('model_weak')
    elif model_type == ModelType.REASONING:
        model = get_model('model_reasoning')
    elif model_type == ModelType.REGULAR:
        model = get_settings().config.model
    else:
        model = get_settings().config.model
    fallback_models = get_settings().config.fallback_models
    if not isinstance(fallback_models, list):
        fallback_models = [m.strip() for m in fallback_models.split(",")]
    all_models = [model] + fallback_models
    return all_models


def _get_all_deployments(all_models: List[str]) -> List[str]:
    deployment_id = get_settings().get("openai.deployment_id", None)
    fallback_deployments = get_settings().get("openai.fallback_deployments", [])
    if not isinstance(fallback_deployments, list) and fallback_deployments:
        fallback_deployments = [d.strip() for d in fallback_deployments.split(",")]
    if fallback_deployments:
        all_deployments = [deployment_id] + fallback_deployments
        if len(all_deployments) < len(all_models):
            raise ValueError(f"The number of deployments ({len(all_deployments)}) "
                             f"is less than the number of models ({len(all_models)})")
    else:
        all_deployments = [deployment_id] * len(all_models)
    return all_deployments


def get_pr_multi_diffs(git_provider: GitProvider,
                       token_handler: TokenHandler,
                       model: str,
                       max_calls: int = 5,
                       add_line_numbers: bool = True) -> List[str]:
    """
    Retrieves the diff files from a Git provider, sorts them by main language, and generates patches for each file.
    The patches are split into multiple groups based on the maximum number of tokens allowed for the given model.

    Args:
        git_provider (GitProvider): An object that provides access to Git provider APIs.
        token_handler (TokenHandler): An object that handles tokens in the context of a pull request.
        model (str): The name of the model.
        max_calls (int, optional): The maximum number of calls to retrieve diff files. Defaults to 5.

    Returns:
        List[str]: A list of final diff strings, split into multiple groups based on the maximum number of tokens allowed for the given model.

    Raises:
        RateLimitExceededException: If the rate limit for the Git provider API is exceeded.
    """
    try:
        diff_files = git_provider.get_diff_files()
    except RateLimitExceededException as e:
        get_logger().error(f"Rate limit exceeded for git provider API. original message {e}")
        raise

    # Sort files by main language
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)

    # Get the maximum number of extra lines before and after the patch
    PATCH_EXTRA_LINES_BEFORE = get_settings().config.patch_extra_lines_before
    PATCH_EXTRA_LINES_AFTER = get_settings().config.patch_extra_lines_after
    PATCH_EXTRA_LINES_BEFORE = cap_and_log_extra_lines(PATCH_EXTRA_LINES_BEFORE, "before")
    PATCH_EXTRA_LINES_AFTER = cap_and_log_extra_lines(PATCH_EXTRA_LINES_AFTER, "after")

    # try first a single run with standard diff string, with patch extension, and no deletions
    patches_extended, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
        pr_languages, token_handler,
        add_line_numbers_to_hunks=add_line_numbers,
        patch_extra_lines_before=PATCH_EXTRA_LINES_BEFORE,
        patch_extra_lines_after=PATCH_EXTRA_LINES_AFTER)

    # if we are under the limit, return the full diff
    if total_tokens + OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD < get_max_tokens(model):
        return ["\n".join(patches_extended)] if patches_extended else []

    # Sort files within each language group by tokens in descending order
    sorted_files = []
    for lang in pr_languages:
        sorted_files.extend(sorted(lang['files'], key=lambda x: x.tokens, reverse=True))

    patches = []
    final_diff_list = []
    total_tokens = token_handler.prompt_tokens
    call_number = 1
    for file in sorted_files:
        if call_number > max_calls:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Reached max calls ({max_calls})")
            break

        original_file_content_str = file.base_file
        new_file_content_str = file.head_file
        patch = file.patch
        if not patch:
            continue

        # Remove delete-only hunks
        patch = handle_patch_deletions(patch, original_file_content_str, new_file_content_str, file.filename, file.edit_type)
        if patch is None:
            continue

        # Add line numbers and metadata to the patch
        if add_line_numbers:
            patch = decouple_and_convert_to_hunks_with_lines_numbers(patch, file)
        else:
            patch = f"\n\n## File: '{file.filename.strip()}'\n\n{patch.strip()}\n"

        # add AI-summary metadata to the patch
        if file.ai_file_summary and get_settings().get("config.enable_ai_metadata", False):
            patch = add_ai_summary_top_patch(file, patch)
        new_patch_tokens = token_handler.count_tokens(patch)

        if patch and (token_handler.prompt_tokens + new_patch_tokens) > get_max_tokens(
                model) - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
            if get_settings().config.get('large_patch_policy', 'skip') == 'skip':
                get_logger().warning(f"Patch too large, skipping: {file.filename}")
                continue
            elif get_settings().config.get('large_patch_policy') == 'clip':
                delta_tokens = get_max_tokens(model) - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD - token_handler.prompt_tokens
                patch_clipped = clip_tokens(patch, delta_tokens, delete_last_line=True, num_input_tokens=new_patch_tokens)
                new_patch_tokens = token_handler.count_tokens(patch_clipped)
                if patch_clipped and (token_handler.prompt_tokens + new_patch_tokens) > get_max_tokens(
                        model) - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
                    get_logger().warning(f"Patch too large, skipping: {file.filename}")
                    continue
                else:
                    get_logger().info(f"Clipped large patch for file: {file.filename}")
                    patch = patch_clipped
            else:
                get_logger().warning(f"Patch too large, skipping: {file.filename}")
                continue

        if patch and (total_tokens + new_patch_tokens > get_max_tokens(model) - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD):
            final_diff = "\n".join(patches)
            final_diff_list.append(final_diff)
            patches = []
            total_tokens = token_handler.prompt_tokens
            call_number += 1
            if call_number > max_calls: # avoid creating new patches
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"Reached max calls ({max_calls})")
                break
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Call number: {call_number}")

        if patch:
            patches.append(patch)
            total_tokens += new_patch_tokens
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Tokens: {total_tokens}, last filename: {file.filename}")

    # Add the last chunk
    if patches:
        final_diff = "\n".join(patches)
        final_diff_list.append(final_diff.strip())

    return final_diff_list


def add_ai_metadata_to_diff_files(git_provider, pr_description_files):
    """
    Adds AI metadata to the diff files based on the PR description files (FilePatchInfo.ai_file_summary).
    """
    try:
        if not pr_description_files:
            get_logger().warning(f"PR description files are empty.")
            return
        available_files = {pr_file['full_file_name'].strip(): pr_file for pr_file in pr_description_files}
        diff_files = git_provider.get_diff_files()
        found_any_match = False
        for file in diff_files:
            filename = file.filename.strip()
            if filename in available_files:
                file.ai_file_summary = available_files[filename]
                found_any_match = True
        if not found_any_match:
            get_logger().error(f"Failed to find any matching files between PR description and diff files.",
                               artifact={"pr_description_files": pr_description_files})
    except Exception as e:
        get_logger().error(f"Failed to add AI metadata to diff files: {e}",
                           artifact={"traceback": traceback.format_exc()})


def add_ai_summary_top_patch(file, full_extended_patch):
    try:
        # below every instance of '## File: ...' in the patch, add the ai-summary metadata
        full_extended_patch_lines = full_extended_patch.split("\n")
        for i, line in enumerate(full_extended_patch_lines):
            if line.startswith("## File:") or line.startswith("## file:"):
                full_extended_patch_lines.insert(i + 1,
                                                 f"### AI-generated changes summary:\n{file.ai_file_summary['long_summary']}")
                full_extended_patch = "\n".join(full_extended_patch_lines)
                return full_extended_patch

        # if no '## File: ...' was found
        return full_extended_patch
    except Exception as e:
        get_logger().error(f"Failed to add AI summary to the top of the patch: {e}",
                           artifact={"traceback": traceback.format_exc()})
        return full_extended_patch
