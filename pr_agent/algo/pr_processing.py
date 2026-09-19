from __future__ import annotations

import traceback
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Callable, List, Tuple

from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extend_patch,
    handle_patch_deletions,
)
from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.model_routing import route_primary_model
from pr_agent.algo.run_details import record_model_used
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import ModelType, clip_tokens, get_model
from pr_agent.config_loader import get_settings, get_verbosity_level
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

DELETED_FILES_ = "Deleted files:\n"

MORE_MODIFIED_FILES_ = "Additional modified files (insufficient token budget to process):\n"

ADDED_FILES_ = "Additional added files (insufficient token budget to process):\n"

OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD = 1500
OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD = 1000
MAX_EXTRA_LINES = 10

_effective_fallback_chain: ContextVar[tuple[tuple[str, str | None], ...] | None] = ContextVar(
    "pr_agent_effective_fallback_chain", default=None
)


def get_effective_fallback_chain() -> tuple[tuple[str, str | None], ...] | None:
    """Return the model/deployment chain selected for the current retry invocation."""
    return _effective_fallback_chain.get()


def _count_raw_and_stripped_tokens(token_handler: TokenHandler, text: str) -> int:
    """Count both raw and stripped forms because either representation may be rendered."""
    token_count = token_handler.count_tokens(text)
    stripped = text.strip()
    if stripped != text:
        token_count = max(token_count, token_handler.count_tokens(stripped))
    return token_count


def _append_metadata_section(
    final_diff: str,
    curr_token: int,
    section: str,
    max_tokens: int,
    token_handler: TokenHandler,
) -> tuple[str, int, str]:
    """Append one clipped metadata section when its rendered form fits the input budget."""
    if not section:
        return final_diff, curr_token, section

    separator = "\n\n"
    available_tokens = max_tokens - curr_token
    separator_tokens = token_handler.count_tokens(separator)
    section_budget = available_tokens - separator_tokens
    if section_budget <= 0:
        return final_diff, curr_token, ""

    section_tokens = token_handler.count_tokens(section)
    clipped_section = clip_tokens(section, section_budget, num_input_tokens=section_tokens)
    if not clipped_section:
        return final_diff, curr_token, ""

    candidate = final_diff + separator + clipped_section
    candidate_tokens = token_handler.prompt_tokens + _count_raw_and_stripped_tokens(token_handler, candidate)
    if candidate_tokens <= max_tokens:
        return candidate, candidate_tokens, clipped_section

    return final_diff, curr_token, ""


def _find_verified_fitting_prefix_length(items, max_length: int, fits: Callable[[list], bool]) -> int:
    """Find a fitting ordered prefix with logarithmic exact-count probes.

    Token counts are not assumed to be monotone across concatenated strings. The returned prefix
    is always one that ``fits`` verified directly, although a longer fitting prefix may exist.
    """
    low = 0
    high = max_length
    while low < high:
        middle = (low + high + 1) // 2
        if fits(items[:middle]):
            low = middle
        else:
            high = middle - 1

    if low == 0 and max_length > 0 and fits(items[:1]):
        return 1
    return low


@dataclass
class PreparedPRDiff:
    """The single-call diff and compressed file data prepared for one model attempt.

    The compressed file data is request-scoped. It is only reused by a caller that keeps the
    same source or bound token handler and model. A different model rebuilds its own counts.
    """

    diff: str
    remaining_files_list: list
    file_dict: dict | None = None
    files_by_name: dict | None = None
    model: str | None = None
    add_line_numbers_to_hunks: bool = False
    token_handler: TokenHandler | None = None
    attempt_budget: AttemptTokenBudget | None = None


def cap_and_log_extra_lines(value, direction) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        get_logger().warning(
            f"patch_extra_lines_{direction} is not a number ({value!r}), using 0")
        return 0
    if value > MAX_EXTRA_LINES:
        get_logger().warning(f"patch_extra_lines_{direction} was {value}, capping to {MAX_EXTRA_LINES}")
        return MAX_EXTRA_LINES
    return value


def get_pr_diff(git_provider: GitProvider, token_handler: TokenHandler,
                model: str,
                add_line_numbers_to_hunks: bool = False,
                disable_extra_lines: bool = False,
                large_pr_handling=False,
                return_remaining_files=False,
                return_prepared=False,
                output_token_reserve: Callable[[str, int], int] | None = None):
    budget = AttemptTokenBudget.for_attempt(
        model, token_handler, output_token_reserve=output_token_reserve
    )
    token_handler = budget.token_handler
    soft_token_budget = budget.available_tokens(
        OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD, preserve_minimum=True, clamp=False
    )
    hard_token_budget = budget.available_tokens(
        OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD, preserve_minimum=True, clamp=False
    )
    if disable_extra_lines:
        PATCH_EXTRA_LINES_BEFORE = 0
        PATCH_EXTRA_LINES_AFTER = 0
    else:
        PATCH_EXTRA_LINES_BEFORE = get_settings().config.patch_extra_lines_before
        PATCH_EXTRA_LINES_AFTER = get_settings().config.patch_extra_lines_after
        PATCH_EXTRA_LINES_BEFORE = cap_and_log_extra_lines(PATCH_EXTRA_LINES_BEFORE, "before")
        PATCH_EXTRA_LINES_AFTER = cap_and_log_extra_lines(PATCH_EXTRA_LINES_AFTER, "after")

    diff_files = git_provider.get_diff_files()

    # get pr languages
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)
    if pr_languages:
        try:
            get_logger().info(f"PR main language: {pr_languages[0]['language']}")
        except Exception:
            pass

    # generate a standard diff string, with patch extension
    patches_extended, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
        pr_languages, token_handler, add_line_numbers_to_hunks,
        patch_extra_lines_before=PATCH_EXTRA_LINES_BEFORE, patch_extra_lines_after=PATCH_EXTRA_LINES_AFTER)

    # if we are under the limit, return the full diff
    if total_tokens - token_handler.prompt_tokens < soft_token_budget:
        get_logger().info(f"Tokens: {total_tokens}, total tokens under limit: {budget.context_window}, "
                          f"returning full diff.")
        full_diff = "\n".join(patches_extended)
        if return_prepared:
            return PreparedPRDiff(full_diff, [], model=model,
                                  add_line_numbers_to_hunks=add_line_numbers_to_hunks,
                                  token_handler=token_handler, attempt_budget=budget)
        return full_diff

    # if we are over the limit, start pruning (If we got here, we will not extend the patches with extra lines)
    get_logger().info(f"Tokens: {total_tokens}, total tokens over limit: {budget.context_window}, "
                      f"pruning diff.")
    patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list = \
        pr_generate_compressed_diff(
            pr_languages,
            token_handler,
            soft_token_budget,
            hard_token_budget,
            add_line_numbers_to_hunks,
            large_pr_handling,
        )

    if large_pr_handling and len(patches_compressed_list) > 1:
        get_logger().info(f"Large PR handling mode, and found {len(patches_compressed_list)} patches with original diff.")
        return "" # return empty string, as we want to generate multiple patches with a different prompt

    # return the first patch
    patches_compressed = patches_compressed_list[0]
    files_in_patch = files_in_patches_list[0]

    # Insert additional information about added, modified, and deleted files if there is enough space
    max_tokens = token_handler.prompt_tokens + hard_token_budget
    final_diff = "\n".join(patches_compressed)
    curr_token = token_handler.prompt_tokens + token_handler.count_tokens(final_diff)
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
    final_diff, curr_token, added_list_str = _append_metadata_section(
        final_diff, curr_token, added_list_str, max_tokens, token_handler
    )
    final_diff, curr_token, modified_list_str = _append_metadata_section(
        final_diff, curr_token, modified_list_str, max_tokens, token_handler
    )
    final_diff, curr_token, deleted_list_str = _append_metadata_section(
        final_diff, curr_token, deleted_list_str, max_tokens, token_handler
    )

    get_logger().debug(f"After pruning, added_list_str: {added_list_str}, modified_list_str: {modified_list_str}, "
                       f"deleted_list_str: {deleted_list_str}")
    if return_prepared:
        files_by_name = {
            file.filename: file
            for language in pr_languages
            for file in language["files"]
        }
        return PreparedPRDiff(
            final_diff,
            remaining_files_list,
            file_dict,
            files_by_name,
            model=model,
            add_line_numbers_to_hunks=add_line_numbers_to_hunks,
            token_handler=token_handler,
            attempt_budget=budget,
        )
    if not return_remaining_files:
        return final_diff
    else:
        return final_diff, remaining_files_list


def get_pr_diff_multiple_patchs(git_provider: GitProvider, token_handler: TokenHandler, model: str,
                add_line_numbers_to_hunks: bool = False, disable_extra_lines: bool = False,
                output_token_reserve: Callable[[str, int], int] | None = None):
    budget = AttemptTokenBudget.for_attempt(
        model, token_handler, output_token_reserve=output_token_reserve
    )
    token_handler = budget.token_handler
    diff_files = git_provider.get_diff_files()

    # get pr languages
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)
    if pr_languages:
        try:
            get_logger().info(f"PR main language: {pr_languages[0]['language']}")
        except Exception:
            pass

    soft_token_budget = budget.available_tokens(
        OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD, preserve_minimum=True, clamp=False
    )
    hard_token_budget = budget.available_tokens(
        OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD, preserve_minimum=True, clamp=False
    )
    patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list = \
        pr_generate_compressed_diff(
            pr_languages,
            token_handler,
            soft_token_budget,
            hard_token_budget,
            add_line_numbers_to_hunks,
            large_pr_handling=True,
        )

    return patches_compressed_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list


def _pack_pr_multi_diffs(file_dict: dict,
                         token_handler: TokenHandler,
                         max_calls: int,
                         return_remaining_files: bool,
                         token_budget: int):
    """Pack diffs additively, then verify each rendered group against the input budget."""
    final_diff_list = []
    files_in_patches = set()

    def count_chunk(candidate_patches):
        rendered = "\n".join(candidate_patches)
        return _count_raw_and_stripped_tokens(token_handler, rendered)

    def clip_single_patch(filename, patch):
        if get_settings().config.get("large_patch_policy", "skip") != "clip":
            get_logger().warning(f"Patch too large, skipping: {filename}")
            return None
        patch_tokens = count_chunk([patch])
        patch_clipped = clip_tokens(
            patch,
            token_budget,
            delete_last_line=True,
            num_input_tokens=patch_tokens,
        )
        clipped_tokens = count_chunk([patch_clipped]) if patch_clipped else token_budget + 1
        if clipped_tokens > token_budget:
            get_logger().warning(f"Patch too large, skipping: {filename}")
            return None
        get_logger().info(f"Clipped large patch for file: {filename}")
        return filename, patch_clipped, clipped_tokens

    packable = []
    for filename, data in file_dict.items():
        patch = data["patch"]
        new_patch_tokens = data["tokens"]
        if not patch:
            continue

        if new_patch_tokens > token_budget:
            clipped_item = clip_single_patch(filename, patch)
            if not clipped_item:
                continue
            filename, patch, new_patch_tokens = clipped_item

        packable.append((filename, patch, new_patch_tokens))

    separator_tokens = token_handler.count_tokens("\n")
    additive_groups = []
    current_group = []
    current_tokens = 0
    for item in packable:
        item_tokens = item[2]
        item_cost = item_tokens + (separator_tokens if current_group else 0)
        if current_group and current_tokens + item_cost > token_budget:
            additive_groups.append(current_group)
            current_group = []
            current_tokens = 0
            item_cost = item_tokens
        current_group.append(item)
        current_tokens += item_cost
    if current_group:
        additive_groups.append(current_group)

    packed_groups = []

    def append_group(group):
        if not group or len(packed_groups) >= max_calls:
            return False
        packed_groups.append(group)
        files_in_patches.update(filename for filename, _, _ in group)
        return True

    for group in additive_groups:
        if len(packed_groups) >= max_calls:
            break

        pending_group = group
        while pending_group and len(packed_groups) < max_calls:
            pending_patches = [patch for _, patch, _ in pending_group]
            if count_chunk(pending_patches) <= token_budget:
                append_group(pending_group)
                break

            prefix_length = _find_verified_fitting_prefix_length(
                pending_group,
                len(pending_group) - 1,
                lambda prefix: count_chunk([patch for _, patch, _ in prefix]) <= token_budget,
            )
            if prefix_length:
                append_group(pending_group[:prefix_length])
                pending_group = pending_group[prefix_length:]
                continue

            item = pending_group[0]
            clipped_item = clip_single_patch(item[0], item[1])
            if clipped_item:
                append_group([clipped_item])
            pending_group = pending_group[1:]

    packed_all = len(files_in_patches) == len(packable)
    for index, group in enumerate(packed_groups):
        rendered_chunk = "\n".join(patch for _, patch, _ in group)
        if packed_all and index == len(packed_groups) - 1:
            rendered_chunk = rendered_chunk.strip()
        final_diff_list.append(rendered_chunk)

    if len(files_in_patches) < len(packable) and get_verbosity_level() >= 2:
        get_logger().info(f"Reached max calls ({max_calls})")

    if not return_remaining_files:
        return final_diff_list

    remaining_files_list = [
        filename for filename in file_dict
        if filename not in files_in_patches
    ]
    return final_diff_list, remaining_files_list


def _get_pr_multi_diffs_from_prepared(prepared_diff: PreparedPRDiff,
                                      token_handler: TokenHandler,
                                      max_calls: int,
                                      return_remaining_files: bool,
                                      token_budget: int):
    """Pack already transformed file patches without repeating preparation work.

    ``get_pr_diff`` and the review chunking path use the same model-specific token handler and
    line-number format. Reusing its compressed file dictionary preserves the existing packing
    and large-patch policy while avoiding a second provider fetch, patch conversion, and token
    count for every file.
    """
    file_dict = {}
    for filename, data in (prepared_diff.file_dict or {}).items():
        patch = data["patch"]
        tokens = data["tokens"]
        file = (prepared_diff.files_by_name or {}).get(filename)
        if file and file.ai_file_summary and get_settings().get("config.enable_ai_metadata", False):
            patch = add_ai_summary_top_patch(file, patch)
            tokens = token_handler.count_tokens(patch)
        file_dict[filename] = {**data, "patch": patch, "tokens": tokens}

    return _pack_pr_multi_diffs(
        file_dict,
        token_handler,
        max_calls,
        return_remaining_files,
        token_budget,
    )


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
            patches_extended_tokens.append(patch_tokens)
            patches_extended.append(full_extended_patch)

    if patches_extended:
        total_tokens += _count_raw_and_stripped_tokens(token_handler, "\n".join(patches_extended))
    return patches_extended, total_tokens, patches_extended_tokens


def pr_generate_compressed_diff(top_langs: list, token_handler: TokenHandler,
                                soft_token_budget: int, hard_token_budget: int,
                                convert_hunks_to_line_numbers: bool,
                                large_pr_handling: bool,
                                ) -> Tuple[list, list, list, list, dict, list]:
    """Prepare patches using the caller's soft and hard diff-only capacities."""
    deleted_files_list = []

    for lang in top_langs:
        for file in lang["files"]:
            if file.tokens is None or file.tokens < 0:
                file.tokens = token_handler.count_tokens(file.patch) if file.patch else 0

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

    # first iteration
    files_in_patches_list = []
    remaining_files_list =  [file.filename for file in sorted_files]
    patches_list =[]
    total_tokens_list = []
    total_tokens, patches, remaining_files_list, files_in_patch_list = generate_full_patch(convert_hunks_to_line_numbers, file_dict,
                                       soft_token_budget, remaining_files_list, token_handler,
                                       hard_token_budget=hard_token_budget)
    patches_list.append(patches)
    total_tokens_list.append(total_tokens)
    files_in_patches_list.append(files_in_patch_list)

    # additional iterations (if needed)
    if large_pr_handling:
        NUMBER_OF_ALLOWED_ITERATIONS = get_settings().pr_description.get("max_ai_calls", 4) - 1 # one more call is to summarize
        for _ in range(NUMBER_OF_ALLOWED_ITERATIONS-1):
            if remaining_files_list:
                total_tokens, patches, remaining_files_list, files_in_patch_list = generate_full_patch(convert_hunks_to_line_numbers,
                                                                                 file_dict,
                                                                                  soft_token_budget,
                                                                                  remaining_files_list, token_handler,
                                                                                  hard_token_budget=hard_token_budget)
                if patches:
                    patches_list.append(patches)
                    total_tokens_list.append(total_tokens)
                    files_in_patches_list.append(files_in_patch_list)
            else:
                break

    return patches_list, total_tokens_list, deleted_files_list, remaining_files_list, file_dict, files_in_patches_list


def generate_full_patch(convert_hunks_to_line_numbers, file_dict, soft_token_budget, remaining_files_list_prev,
                        token_handler, *, hard_token_budget: int):
    """Admit rendered patches using diff-only budgets; return prompt-inclusive totals."""
    total_tokens = token_handler.prompt_tokens # initial tokens
    patches = []
    remaining_files_list_new = []
    files_in_patch_list = []
    separator_tokens = None
    for filename, data in file_dict.items():
        if filename not in remaining_files_list_prev:
            continue

        patch = data['patch']
        if total_tokens - token_handler.prompt_tokens > hard_token_budget:
            get_logger().warning(f"File was fully skipped, no more tokens: {filename}.")
            remaining_files_list_new.append(filename)
            continue

        if patch:
            if not convert_hunks_to_line_numbers:
                patch_final = f"\n\n## File: '{filename.strip()}'\n\n{patch.strip()}\n"
            else:
                patch_final = "\n\n" + patch.strip()
            new_patch_tokens = token_handler.count_tokens(patch_final)
            if patches and separator_tokens is None:
                separator_tokens = token_handler.count_tokens("\n")
            rendered_patch_tokens = new_patch_tokens + (separator_tokens or 0)
            if total_tokens + rendered_patch_tokens > token_handler.prompt_tokens + soft_token_budget:
                if get_verbosity_level() >= 2:
                    get_logger().warning(f"Patch too large, skipping it: '{filename}'")
                remaining_files_list_new.append(filename)
                continue
            patches.append(patch_final)
            files_in_patch_list.append(filename)
            total_tokens += rendered_patch_tokens
            if get_verbosity_level() >= 2:
                get_logger().info(f"Tokens: {total_tokens}, last filename: {filename}")

    def count_patches(candidate_patches):
        return _count_raw_and_stripped_tokens(token_handler, "\n".join(candidate_patches))

    if patches:
        exact_total = token_handler.prompt_tokens + count_patches(patches)
        if exact_total - token_handler.prompt_tokens > soft_token_budget:
            best_count = _find_verified_fitting_prefix_length(
                patches,
                len(patches) - 1,
                lambda prefix: count_patches(prefix) <= soft_token_budget,
            )

            for filename in files_in_patch_list[best_count:]:
                if filename not in remaining_files_list_new:
                    remaining_files_list_new.append(filename)
            patches = patches[:best_count]
            files_in_patch_list = files_in_patch_list[:best_count]
            total_tokens = (
                token_handler.prompt_tokens + count_patches(patches)
                if patches
                else token_handler.prompt_tokens
            )
        else:
            total_tokens = exact_total

    return total_tokens, patches, remaining_files_list_new, files_in_patch_list


async def retry_with_fallback_models(f: Callable, model_type: ModelType = ModelType.REGULAR,
                                     git_provider: GitProvider | None = None):
    all_models = _get_all_models(model_type)
    all_deployments = _get_all_deployments(all_models)
    routed = route_primary_model(model_type, git_provider)
    if routed:
        # A cheaper primary for a small pull request; config.fallback_models still follow it.
        all_models[0], all_deployments[0] = routed
    # Ignore surplus deployment entries when fewer fallback models are configured; this matches
    # the existing retry loop, which stops as soon as the first successful model returns.
    effective_chain = tuple(zip(all_models, all_deployments[:len(all_models)], strict=True))
    original_deployment_id = get_settings().get("openai.deployment_id", None)
    context_token = _effective_fallback_chain.set(effective_chain)
    try:
        # try each (model, deployment_id) pair until one is successful, otherwise raise exception
        for i, (model, deployment_id) in enumerate(effective_chain):
            try:
                get_logger().debug(
                    f"Generating prediction with {model}"
                    f"{(' from deployment ' + deployment_id) if deployment_id else ''}"
                )
                get_settings().set("openai.deployment_id", deployment_id)
                result = await f(model)
            except Exception as e:
                get_logger().warning(
                    f"Failed to generate prediction with {model}",
                    artifact={"error": e},
                )
                if i == len(all_models) - 1:  # If it's the last iteration
                    raise Exception(f"Failed to generate prediction with any model of {all_models}") from e
            else:
                record_model_used(model, is_fallback=i > 0)
                return result
    finally:
        _effective_fallback_chain.reset(context_token)
        get_settings().set("openai.deployment_id", original_deployment_id)


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
        fallback_models = fallback_models.split(",")
    fallback_models = [m.strip() for m in fallback_models if isinstance(m, str) and m.strip()]
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
                       add_line_numbers: bool = True,
                       return_remaining_files: bool = False,
                       prepared_diff: PreparedPRDiff | None = None,
                       output_token_reserve: Callable[[str, int], int] | None = None):
    """
    Retrieves the diff files from a Git provider, sorts them by main language, and generates patches for each file.
    The patches are split into multiple groups based on the maximum number of tokens allowed for the given model.

    Args:
        git_provider (GitProvider): An object that provides access to Git provider APIs.
        token_handler (TokenHandler): An object that handles tokens in the context of a pull request.
        model (str): The name of the model.
        max_calls (int, optional): Maximum number of groups for split diffs; the full-diff fast path may still return one group. Defaults to 5.
        return_remaining_files (bool, optional): Also return the files the token budget left out, in the
            same shape as `get_pr_diff`. Files without a patch, and delete-only files, are not reported:
            nothing was omitted for them. Defaults to False.
        prepared_diff (PreparedPRDiff, optional): Reuse compressed file data prepared by a preceding
            `get_pr_diff` call for the same model attempt. Defaults to None.

    Returns:
        List[str]: A list of final diff strings, split into multiple groups based on the maximum number of tokens allowed for the given model.
        With `return_remaining_files`, a tuple of that list and the list of omitted file names.

    """
    can_reuse_prepared = (
        prepared_diff is not None
        and prepared_diff.file_dict is not None
        and prepared_diff.model == model
        and add_line_numbers
        and prepared_diff.add_line_numbers_to_hunks == add_line_numbers
        and (
            prepared_diff.attempt_budget.matches(model, token_handler)
            if prepared_diff.attempt_budget is not None
            else prepared_diff.token_handler is token_handler
        )
    )
    if can_reuse_prepared and prepared_diff.attempt_budget is not None:
        # Reuse the model-bound prompt count while honoring this call's reserve policy.
        budget = replace(prepared_diff.attempt_budget, output_token_reserve=output_token_reserve)
    else:
        budget = AttemptTokenBudget.for_attempt(
            model, token_handler, output_token_reserve=output_token_reserve
        )
    token_handler = budget.token_handler
    soft_token_budget = budget.available_tokens(
        OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD, preserve_minimum=True, clamp=False
    )

    if can_reuse_prepared:
        return _get_pr_multi_diffs_from_prepared(
            prepared_diff,
            token_handler,
            max_calls,
            return_remaining_files,
            soft_token_budget,
        )

    diff_files = git_provider.get_diff_files()

    # Sort files by main language
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), diff_files)

    # Get the maximum number of extra lines before and after the patch
    PATCH_EXTRA_LINES_BEFORE = get_settings().config.patch_extra_lines_before
    PATCH_EXTRA_LINES_AFTER = get_settings().config.patch_extra_lines_after
    PATCH_EXTRA_LINES_BEFORE = cap_and_log_extra_lines(PATCH_EXTRA_LINES_BEFORE, "before")
    PATCH_EXTRA_LINES_AFTER = cap_and_log_extra_lines(PATCH_EXTRA_LINES_AFTER, "after")

    # First try a single run with the full diff and extended patch context.
    patches_extended, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
        pr_languages, token_handler,
        add_line_numbers_to_hunks=add_line_numbers,
        patch_extra_lines_before=PATCH_EXTRA_LINES_BEFORE,
        patch_extra_lines_after=PATCH_EXTRA_LINES_AFTER)

    # if we are under the limit, return the full diff
    if total_tokens - token_handler.prompt_tokens < soft_token_budget:
        full_diff_list = ["\n".join(patches_extended)] if patches_extended else []
        return (full_diff_list, []) if return_remaining_files else full_diff_list

    # Sort files within each language group by tokens in descending order
    sorted_files = []
    for lang in pr_languages:
        sorted_files.extend(sorted(lang['files'], key=lambda x: x.tokens, reverse=True))

    # Build the same transformed file dictionary used by the prepared path, preserving the
    # descending token order established above. The shared packer then owns chunk boundaries,
    # large-patch policy, and remaining-file tracking for both paths.
    file_dict = {}
    for file in sorted_files:
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
        file_dict[file.filename] = {
            'patch': patch,
            'tokens': new_patch_tokens,
            'edit_type': file.edit_type,
        }

    return _pack_pr_multi_diffs(
        file_dict,
        token_handler,
        max_calls,
        return_remaining_files,
        soft_token_budget,
    )


def add_ai_metadata_to_diff_files(git_provider, pr_description_files):
    """
    Adds AI metadata to the diff files based on the PR description files (FilePatchInfo.ai_file_summary).
    """
    try:
        if not pr_description_files:
            get_logger().warning("PR description files are empty.")
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
            get_logger().error("Failed to find any matching files between PR description and diff files.",
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
