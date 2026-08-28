import asyncio
import copy
import difflib
import re
import textwrap
import traceback
from datetime import datetime
from functools import partial
from typing import Dict, List, Optional

from jinja2 import Environment, StrictUndefined

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.git_patch_processing import \
    decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.algo.pr_processing import (_get_all_models,
                                         add_ai_metadata_to_diff_files,
                                         get_pr_diff, get_pr_multi_diffs,
                                         retry_with_fallback_models)
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.run_details import init_run_details
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (ModelType, PRCodeSuggestionsHeader,
                                 PRCodeSuggestionsIdentity,
                                 add_comment_identity, clip_tokens,
                                 comment_matches_identity,
                                 format_pr_code_suggestions_header,
                                 get_max_tokens, get_model, load_yaml,
                                 replace_code_tags,
                                 show_relevant_configurations,
                                 show_run_details)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import (AzureDevopsProvider, GithubProvider,
                                    GitLabProvider, get_git_provider,
                                    get_git_provider_with_context)
from pr_agent.git_providers.git_provider import (GitProvider, IncrementalPR,
                                                 get_main_pr_language)
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.pr_description import insert_br_after_x_chars
from pr_agent.tools.progress_comment import build_progress_comment


class PRCodeSuggestions:
    def __init__(self, pr_url: str, cli_mode=False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):

        self.git_provider = get_git_provider_with_context(pr_url)
        self.pr_url = pr_url  # set early so the no-op log line in `run()` can reference it
        self.args = args
        self.incremental = self._parse_incremental(args)
        self._incremental_empty_scope = False
        # When invoked as `/improve -i`, narrow `git_provider.get_diff_files()` to the files
        # changed since the previous suggestions pass. Falls back to full when the provider
        # doesn't support incremental scope or no prior suggestion comment exists.
        self._setup_incremental_scope()
        # If incremental is active but the scope came back empty (no files changed since the
        # previous suggestions pass), short-circuit init now. `run()` checks the same flag and
        # exits without touching the model. This avoids a wasted `mr.changes()` round-trip via
        # `get_files()` — when `unreviewed_files_map` is `{}` it's falsy and `get_files()` falls
        # back to the full MR file list, which is pure waste on the "nothing new" path.
        if (self.incremental.is_incremental
                and hasattr(self.git_provider, "unreviewed_files_map")
                and not self.git_provider.unreviewed_files_map):
            self._incremental_empty_scope = True
            return
        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )

        raw_num_code_suggestions = get_settings().pr_code_suggestions.num_code_suggestions_per_chunk
        try:
            num_code_suggestions = int(raw_num_code_suggestions)
        except (TypeError, ValueError):
            num_code_suggestions = 3
            get_logger().warning(
                f"num_code_suggestions_per_chunk is not a number ({raw_num_code_suggestions!r}), "
                f"using {num_code_suggestions}")

        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        self.cli_mode = cli_mode
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug(f"AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug(f"AI metadata is disabled for this command")

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "diff_no_line_numbers": "",  # empty diff for initial calculation
            "num_code_suggestions": num_code_suggestions,
            "extra_instructions": get_settings().pr_code_suggestions.extra_instructions,
            "skills_context": get_skills_context(),
            "repo_context": build_repo_context(self.git_provider),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "relevant_best_practices": "",
            "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
            "focus_only_on_problems": get_settings().get("pr_code_suggestions.focus_only_on_problems", False),
            "date": datetime.now().strftime('%Y-%m-%d'),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
        }

        if get_settings().pr_code_suggestions.get("decouple_hunks", True):
            self.pr_code_suggestions_prompt_system = get_settings().pr_code_suggestions_prompt.system
            self.pr_code_suggestions_prompt_user = get_settings().pr_code_suggestions_prompt.user
        else:
            self.pr_code_suggestions_prompt_system = get_settings().pr_code_suggestions_prompt_not_decoupled.system
            self.pr_code_suggestions_prompt_user = get_settings().pr_code_suggestions_prompt_not_decoupled.user

        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          self.pr_code_suggestions_prompt_system,
                                          self.pr_code_suggestions_prompt_user)

        self.progress = build_progress_comment()
        self.progress_response = None

    @staticmethod
    def _parse_incremental(args):
        """Parse the `-i` flag for `/improve` exactly like `PRReviewer.parse_incremental`."""
        is_incremental = bool(args and len(args) >= 1 and args[0] == "-i")
        return IncrementalPR(is_incremental)

    def _setup_incremental_scope(self):
        """Configure the provider's suggestions-scoped incremental state for `/improve -i`.

        Falls back to a full run (incremental disabled) when the provider doesn't
        support kind-scoped incremental anchoring.
        """
        if not self.incremental.is_incremental:
            return
        if self.git_provider.supports_incremental_kind("suggestions"):
            self.git_provider.get_incremental_commits(self.incremental, kind="suggestions")
        else:
            get_logger().info(
                "Provider does not support incremental suggestions scope; "
                "running /improve on the full diff"
            )
            self.incremental = IncrementalPR(False)

    async def run(self):
        init_run_details()
        try:
            if getattr(self, "_incremental_empty_scope", False):
                # Set by `__init__` when incremental anchored cleanly but no files changed
                # since the previous suggestions pass. Skip silently — re-running on the
                # full MR diff here would just re-post the same inline suggestions.
                get_logger().info(
                    f"Incremental /improve for {self.pr_url}: no files changed since the previous "
                    f"suggestions pass; skipping"
                )
                return None

            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping code suggestions")
                return None

            get_logger().info('Generating code suggestions for PR...')
            relevant_configs = {'pr_code_suggestions': dict(get_settings().pr_code_suggestions),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # publish "Preparing suggestions..." comments
            if (get_settings().config.publish_output and get_settings().config.publish_output_progress and
                    not get_settings().config.get('is_auto_command', False)):
                if self.git_provider.is_supported("gfm_markdown"):
                    self.progress_response = self.git_provider.publish_comment(self.progress)
                else:
                    self.progress_response = self.git_provider.publish_comment(
                        "Preparing suggestions...", is_temporary=True)

            # # call the model to get the suggestions, and self-reflect on them
            # if not self.is_extended:
            #     data = await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            # else:
            data = await retry_with_fallback_models(self.prepare_prediction_main, model_type=ModelType.REGULAR)
            if not data:
                data = {"code_suggestions": []}
            self.data = data

            # Handle the case where the PR has no suggestions
            if (data is None or 'code_suggestions' not in data or not data['code_suggestions']):
                await self.publish_no_suggestions()
                return

            # publish the suggestions
            if get_settings().config.publish_output:
                # If a temporary comment was published, remove it
                self.git_provider.remove_initial_comment()

                # Publish table summarized suggestions
                if ((not get_settings().pr_code_suggestions.commitable_code_suggestions) and
                        self.git_provider.is_supported("gfm_markdown")):

                    # generate summarized suggestions
                    pr_body = self.generate_summarized_suggestions(data)
                    pr_body += self._get_suggestions_coverage_footer()
                    get_logger().debug(f"PR output", artifact=pr_body)

                    # require self-review
                    if get_settings().pr_code_suggestions.demand_code_suggestions_self_review:
                        pr_body = await self.add_self_review_text(pr_body)

                    # add usage guide
                    if (get_settings().pr_code_suggestions.enable_chat_text and get_settings().config.is_auto_command
                            and isinstance(self.git_provider, GithubProvider)):
                        pr_body += "\n\n>💡 Need additional feedback ? start a [PR chat](https://chromewebstore.google.com/detail/ephlnjeghhogofkifjloamocljapahnl) \n\n"
                    if get_settings().pr_code_suggestions.enable_help_text:
                        pr_body += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
                        pr_body += HelpMessage.get_improve_usage_guide()
                        pr_body += "\n</details>\n"

                    # Output the relevant configurations if enabled
                    if get_settings().get('config', {}).get('output_relevant_configurations', False):
                        pr_body += show_relevant_configurations(relevant_section='pr_code_suggestions')

                    # Output the agent run details (model, tokens, time cost) if enabled
                    if get_settings().get('config', {}).get('output_run_details', False):
                        # This summary-comment branch already requires GFM support, so the argument is always True;
                        # keep the call shaped like the reviewer/describe paths for consistency.
                        pr_body += show_run_details(self.git_provider.is_supported("gfm_markdown"))

                    # publish the PR comment
                    if get_settings().pr_code_suggestions.persistent_comment: # true by default
                        self.publish_persistent_comment_with_history(
                            self.git_provider,
                            pr_body,
                            initial_header=format_pr_code_suggestions_header(),
                            update_header=True,
                            name="suggestions",
                            final_update_message=False,
                            max_previous_comments=get_settings().pr_code_suggestions.max_history_len,
                            progress_response=self.progress_response,
                            identity_marker=PRCodeSuggestionsIdentity.SUMMARY.value,
                            legacy_initial_header=PRCodeSuggestionsHeader.SUMMARY.value,
                        )
                    else:
                        pr_body = add_comment_identity(
                            pr_body,
                            PRCodeSuggestionsIdentity.SUMMARY.value,
                        )
                        if self.progress_response:
                            self.git_provider.edit_comment(self.progress_response, body=pr_body)
                        else:
                            self.git_provider.publish_comment(pr_body)

                    # dual publishing mode
                    if int(get_settings().pr_code_suggestions.dual_publishing_score_threshold) > 0:
                        await self.dual_publishing(data)
                else:
                    await self.push_inline_code_suggestions(data)
                    if self.progress_response:
                        self.git_provider.remove_comment(self.progress_response)
            else:
                get_logger().info('Code suggestions generated for PR, but not published since publish_output is False.')
                pr_body = self.generate_summarized_suggestions(data)
                pr_body += self._get_suggestions_coverage_footer()
                get_settings().data = {"artifact": pr_body}
                return
        except Exception as e:
            get_logger().error(f"Failed to generate code suggestions for PR, error: {e}",
                               artifact={"traceback": traceback.format_exc()})
            if get_settings().config.publish_output:
                if self.progress_response:
                    self.git_provider.remove_comment(self.progress_response)
                else:
                    try:
                        self.git_provider.remove_initial_comment()
                        self.git_provider.publish_comment(f"Failed to generate code suggestions for PR")
                    except Exception as e:
                        get_logger().exception(f"Failed to update persistent review, error: {e}")
            if get_settings().config.get("propagate_tool_errors", False):
                raise

    async def add_self_review_text(self, pr_body):
        text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
        pr_body += f"\n\n- [ ]  {text}"
        approve_pr_on_self_review = get_settings().pr_code_suggestions.approve_pr_on_self_review
        fold_suggestions_on_self_review = get_settings().pr_code_suggestions.fold_suggestions_on_self_review
        if approve_pr_on_self_review and not fold_suggestions_on_self_review:
            pr_body += ' <!-- approve pr self-review -->'
        elif fold_suggestions_on_self_review and not approve_pr_on_self_review:
            pr_body += ' <!-- fold suggestions self-review -->'
        else:
            pr_body += ' <!-- approve and fold suggestions self-review -->'
        return pr_body

    def _get_suggestions_coverage_footer(self, suggestions_present: bool = True) -> str:
        failed_chunk_count = getattr(self, "failed_chunk_count", 0)
        if (not failed_chunk_count or
                not get_settings().pr_code_suggestions.get("enable_suggestions_coverage_footer", True)):
            return ""
        total_chunk_count = getattr(self, "total_chunk_count", failed_chunk_count)
        coverage_detail = ("the suggestions above are based on the successful chunks only."
                           if suggestions_present else
                           "no suggestions were found in the successful chunks; failed chunks could not be analyzed.")
        return (f"\n\n⚠️ **Suggestion coverage:** {failed_chunk_count} of {total_chunk_count} "
                "analysis chunks failed; "
                f"{coverage_detail}")

    async def publish_no_suggestions(self):
        coverage_footer = self._get_suggestions_coverage_footer(suggestions_present=False)
        no_suggestions_message = ("No code suggestions found in the successfully analyzed chunks."
                                  if coverage_footer else "No code suggestions found for the PR.")
        pr_body = f"{format_pr_code_suggestions_header()}\n\n{no_suggestions_message}{coverage_footer}"
        if (get_settings().config.publish_output and
                get_settings().pr_code_suggestions.get('publish_output_no_suggestions', True)):
            get_logger().warning("No code suggestions found for the PR.")
            if self.git_provider.supports_code_suggestions_artifact() is True:
                self.git_provider.publish_code_suggestions_artifact(
                    [], artifact_footer=coverage_footer, no_suggestions_message=no_suggestions_message)
                return
            pr_body = add_comment_identity(
                pr_body,
                PRCodeSuggestionsIdentity.NO_SUGGESTIONS.value,
            )
            # Output the agent run details (model, tokens, time cost) if enabled, so the
            # "no suggestions" result still shows which model produced it.
            if get_settings().get('config', {}).get('output_run_details', False):
                pr_body += show_run_details(self.git_provider.is_supported("gfm_markdown"))
            get_logger().debug(f"PR output", artifact=pr_body)
            if self.progress_response:
                self.git_provider.edit_comment(self.progress_response, body=pr_body)
            else:
                self.git_provider.publish_comment(pr_body)
        else:
            get_settings().data = {"artifact": pr_body if coverage_footer else ""}
            if self.progress_response:
                self.git_provider.remove_comment(self.progress_response)

    async def dual_publishing(self, data):
        data_above_threshold = {'code_suggestions': []}
        try:
            for suggestion in data['code_suggestions']:
                if int(suggestion.get('score', 0)) >= int(
                        get_settings().pr_code_suggestions.dual_publishing_score_threshold):
                    data_above_threshold["code_suggestions"].append(suggestion)
                    if suggestion.get("improved_code") and not data_above_threshold["code_suggestions"][-1][
                            "existing_code"]:
                        get_logger().info(f'Identical existing and improved code for dual publishing found')
                        data_above_threshold['code_suggestions'][-1]['existing_code'] = suggestion[
                            'improved_code']
            if data_above_threshold['code_suggestions']:
                get_logger().info(
                    f"Publishing {len(data_above_threshold['code_suggestions'])} suggestions in dual publishing mode")
                await self.push_inline_code_suggestions(data_above_threshold, include_coverage_footer=False)
        except Exception as e:
            get_logger().error(f"Failed to publish dual publishing suggestions, error: {e}")

    @staticmethod
    def publish_persistent_comment_with_history(git_provider: GitProvider,
                                                pr_comment: str,
                                                initial_header: str,
                                                update_header: bool = True,
                                                name='review',
                                                final_update_message=True,
                                                max_previous_comments=4,
                                                progress_response=None,
                                                only_fold=False,
                                                identity_marker: str | None = None,
                                                legacy_initial_header: str | None = None):
        if hasattr(git_provider, '_publish_check_run') and get_settings().github.publish_as_check_run:
            if git_provider._publish_check_run(pr_comment, name):
                return

        def _extract_link(comment_text: str):
            match = re.search(r"<!--\s*([0-9a-fA-F]{7,40})\s*-->", comment_text)

            up_to_commit_txt = ""
            if match:
                up_to_commit_txt = f" up to commit {match.group(1)}"
            return up_to_commit_txt

        def _comment_body(comment) -> str:
            body = getattr(comment, "body", None)
            if body is None and isinstance(comment, dict):
                body = comment.get("body")
            return body if isinstance(body, str) else ""

        def _is_legacy_suggestions_comment(comment_text: str) -> bool:
            if not legacy_initial_header or not comment_text.startswith(f"{legacy_initial_header}\n"):
                return False
            table_index = comment_text.find("<table>")
            if table_index == -1:
                return False
            return bool(
                re.search(
                    r"<!--\s*[0-9a-fA-F]{7,40}\s*-->",
                    comment_text[len(legacy_initial_header):table_index],
                )
            )

        def _without_heading(comment_text: str) -> str:
            if comment_text.startswith(initial_header):
                comment_text = comment_text[len(initial_header):].lstrip("\n")
            if identity_marker and comment_text.startswith(identity_marker):
                comment_text = comment_text[len(identity_marker):].lstrip("\n")
            return comment_text.strip()

        def _with_identity(comment_text: str) -> str:
            return add_comment_identity(comment_text, identity_marker)

        def _clean_up_progress_note():
            if not progress_response:
                return
            try:
                git_provider.edit_comment(
                    progress_response,
                    "Code suggestions published in the persistent thread above.",
                )
                git_provider.remove_comment(progress_response)
            except Exception as cleanup_error:
                get_logger().warning(
                    "Failed to clean up progress note after persistent update, "
                    f"leaving it in place: {cleanup_error}"
                )

        history_header = f"#### Previous suggestions\n"
        last_commit_num = git_provider.get_latest_commit_url().split('/')[-1][:7]
        if only_fold: # A user clicked on the 'self-review' checkbox
            text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
            latest_suggestion_header = f"\n\n- [x]  {text}"
        else:
            latest_suggestion_header = f"Latest suggestions up to {last_commit_num}"
        latest_commit_html_comment = f"<!-- {last_commit_num} -->"
        new_suggestion_table = _without_heading(pr_comment)

        if max_previous_comments > 0:
            try:
                prev_comments = list(git_provider.get_issue_comments())
                if identity_marker:
                    comment = next(
                        (
                            candidate
                            for candidate in prev_comments
                            if comment_matches_identity(_comment_body(candidate), identity_marker)
                        ),
                        None,
                    )
                    if comment is None:
                        comment = next(
                            (
                                candidate
                                for candidate in prev_comments
                                if _is_legacy_suggestions_comment(_comment_body(candidate))
                            ),
                            None,
                        )
                else:
                    comment = next(
                        (
                            candidate
                            for candidate in prev_comments
                            if comment_matches_identity(_comment_body(candidate), initial_header)
                        ),
                        None,
                    )
                if comment:
                    prev_suggestions = _comment_body(comment)
                    comment_url = git_provider.get_comment_url(comment)

                    if history_header.strip() not in prev_suggestions:
                        # no history section
                        # extract everything between <table> and </table> in comment.body including <table> and </table>
                        table_index = prev_suggestions.find("<table>")
                        if table_index == -1:
                            pr_comment_updated = _with_identity(
                                f"{initial_header}\n\n{latest_commit_html_comment}\n\n"
                                f"{new_suggestion_table}\n\n"
                            )
                            git_provider.edit_comment(comment, pr_comment_updated)
                            _clean_up_progress_note()
                            return comment
                        # find http link from comment.body[:table_index]
                        up_to_commit_txt = _extract_link(prev_suggestions[:table_index])
                        prev_suggestion_table = prev_suggestions[
                                                table_index:prev_suggestions.rfind("</table>") + len("</table>")]

                        tick = "✅ " if "✅" in prev_suggestion_table else ""
                        # surround with details tag
                        prev_suggestion_table = (
                            f"<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n"
                            f"<br>{prev_suggestion_table}\n\n</details>"
                        )

                        pr_comment_updated = _with_identity(
                            f"{initial_header}\n\n{latest_commit_html_comment}\n\n"
                            f"{latest_suggestion_header}\n\n{new_suggestion_table}\n\n___\n\n"
                            f"{history_header}{prev_suggestion_table}\n"
                        )
                    else:
                        # get the text of the previous suggestions until the latest commit
                        sections = prev_suggestions.split(history_header.strip())
                        latest_table = sections[0].strip()
                        prev_suggestion_table = sections[1].replace(history_header, "").strip()

                        # get text after the latest_suggestion_header in comment.body
                        table_ind = latest_table.find("<table>")
                        up_to_commit_txt = _extract_link(latest_table[:table_ind])

                        latest_table = latest_table[table_ind:latest_table.rfind("</table>") + len("</table>")]
                        # enforce max_previous_comments
                        count = prev_suggestions.count(f"\n<details><summary>{name.capitalize()}")
                        count += prev_suggestions.count(f"\n<details><summary>✅ {name.capitalize()}")
                        if count >= max_previous_comments:
                            # remove the oldest suggestion
                            prev_suggestion_table = prev_suggestion_table[:prev_suggestion_table.rfind(
                                f"<details><summary>{name.capitalize()} up to commit")]

                        tick = "✅ " if "✅" in latest_table else ""
                        # Add to the prev_suggestions section
                        last_prev_table = (
                            f"\n<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n"
                            f"<br>{latest_table}\n\n</details>"
                        )
                        prev_suggestion_table = last_prev_table + "\n" + prev_suggestion_table

                        pr_comment_updated = _with_identity(
                            f"{initial_header}\n\n{latest_commit_html_comment}\n\n"
                            f"{latest_suggestion_header}\n\n{new_suggestion_table}\n\n"
                            f"___\n\n{history_header}\n{prev_suggestion_table}\n"
                        )

                    get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
                    git_provider.edit_comment(comment, pr_comment_updated)
                    _clean_up_progress_note()
                    return comment
            except Exception as e:
                get_logger().exception(f"Failed to update persistent review, error: {e}")
                pass

        # if we are here, we did not find a previous comment to update
        pr_comment = _with_identity(
            f"{initial_header}\n\n{latest_commit_html_comment}\n\n"
            f"{new_suggestion_table}\n\n"
        )
        if progress_response:
            git_provider.edit_comment(progress_response, pr_comment)
            new_comment = progress_response
        else:
            new_comment = git_provider.publish_comment(pr_comment)
        return new_comment


    def extract_link(self, s):
        r = re.compile(r"<!--.*?-->")
        match = r.search(s)

        up_to_commit_txt = ""
        if match:
            up_to_commit_txt = f" up to commit {match.group(0)[4:-3].strip()}"
        return up_to_commit_txt

    async def _prepare_prediction(self, model: str) -> dict:
        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False)
        self.patches_diff_list = [self.patches_diff]
        self.patches_diff_no_line_number = self.remove_line_numbers([self.patches_diff])[0]

        if self.patches_diff:
            get_logger().debug(f"PR diff", artifact=self.patches_diff)
            self.prediction = await self._get_prediction(model, self.patches_diff, self.patches_diff_no_line_number)
        else:
            get_logger().warning(f"Empty PR diff")
            self.prediction = None

        data = self.prediction
        return data

    async def _get_prediction(self, model: str, patches_diff: str, patches_diff_no_line_number: str) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = patches_diff  # update diff
        variables["diff_no_line_numbers"] = patches_diff_no_line_number  # update diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(self.pr_code_suggestions_prompt_system).render(variables)
        user_prompt = environment.from_string(self.pr_code_suggestions_prompt_user).render(variables)
        response, finish_reason = await self.ai_handler.chat_completion(
            model=model, temperature=get_settings().config.temperature, system=system_prompt, user=user_prompt)
        if not get_settings().config.publish_output:
            get_settings().system_prompt = system_prompt
            get_settings().user_prompt = user_prompt

        # load suggestions from the AI response
        data = self._prepare_pr_code_suggestions(response)

        # self-reflect on suggestions (mandatory, since line numbers are generated now here)
        response_reflect = await self._self_reflect_with_fallback(data["code_suggestions"], patches_diff, model)
        if response_reflect:
            await self.analyze_self_reflection_response(data, response_reflect)
        else:
            # get_logger().error(f"Could not self-reflect on suggestions. using default score 7")
            for i, suggestion in enumerate(data["code_suggestions"]):
                suggestion["score"] = 7
                suggestion["score_why"] = ""

        return data

    async def _self_reflect_with_fallback(self, suggestion_list: List, patches_diff: str, model: str) -> str:
        """Reflect over the reasoning models, returning the first non-empty response.

        self_reflect_on_suggestions swallows its errors and returns "", so an empty response is
        treated as a failure. This walks the chain itself rather than nesting
        retry_with_fallback_models, which sets the global openai.deployment_id without restoring
        it - nested, that would leak the reflection's deployment into the rest of the run and race
        the other chunk calls, since parallel_calls is on by default.
        """
        if not suggestion_list:
            return ""

        models = _get_all_models(ModelType.REASONING)
        if get_model('model_reasoning') == get_settings().config.model and model in models:
            # No dedicated reasoning model, so this is the regular chain and the outer fallback
            # loop has already burned everything before the model it settled on.
            models = models[models.index(model):]
        if get_settings().get("openai.fallback_deployments", []):
            # Each model is pinned to its own deployment, and openai.deployment_id is global to a
            # run whose chunk calls are already in flight concurrently. Retrying another model here
            # would route it to the deployment this one is pinned to, so stop at the first.
            models = models[:1]

        for reflection_model in models:
            response = await self.self_reflect_on_suggestions(suggestion_list, patches_diff,
                                                              model=reflection_model)
            if response:
                return response
            get_logger().warning(f"Empty self-reflection response from {reflection_model}")
        return ""

    async def analyze_self_reflection_response(self, data, response_reflect):
        response_reflect_yaml = load_yaml(response_reflect)
        code_suggestions_feedback = response_reflect_yaml.get("code_suggestions", [])
        if code_suggestions_feedback and len(code_suggestions_feedback) == len(data["code_suggestions"]):
            for i, suggestion in enumerate(data["code_suggestions"]):
                try:
                    suggestion["score"] = code_suggestions_feedback[i]["suggestion_score"]
                    suggestion["score_why"] = code_suggestions_feedback[i]["why"]

                    if 'relevant_lines_start' not in suggestion:
                        relevant_lines_start = code_suggestions_feedback[i].get('relevant_lines_start', -1)
                        relevant_lines_end = code_suggestions_feedback[i].get('relevant_lines_end', -1)
                        suggestion['relevant_lines_start'] = relevant_lines_start
                        suggestion['relevant_lines_end'] = relevant_lines_end
                        if relevant_lines_start < 0 or relevant_lines_end < 0:
                            suggestion["score"] = 0

                    try:
                        if get_settings().config.publish_output:
                            if not suggestion["score"]:
                                score = -1
                            else:
                                score = int(suggestion["score"])
                            label = suggestion["label"].lower().strip()
                            label = label.replace('<br>', ' ')
                            suggestion_statistics_dict = {'score': score,
                                                          'label': label}
                            get_logger().info(f"PR-Agent suggestions statistics",
                                              statistics=suggestion_statistics_dict, analytics=True)
                    except Exception as e:
                        get_logger().error(f"Failed to log suggestion statistics, error: {e}")
                        pass

                except Exception as e:  #
                    get_logger().error(f"Error processing suggestion score {i}",
                                       artifact={"suggestion": suggestion,
                                                 "code_suggestions_feedback": code_suggestions_feedback[i]})
                    suggestion["score"] = 7
                    suggestion["score_why"] = ""

                suggestion = self.validate_one_liner_suggestion_not_repeating_code(suggestion)

                # if the before and after code is the same, clear one of them
                try:
                    if suggestion['existing_code'] == suggestion['improved_code']:
                        get_logger().debug(
                            f"edited improved suggestion {i + 1}, because equal to existing code: {suggestion['existing_code']}")
                        if get_settings().pr_code_suggestions.commitable_code_suggestions:
                            suggestion['improved_code'] = ""  # we need 'existing_code' to locate the code in the PR
                        else:
                            suggestion['existing_code'] = ""
                except Exception as e:
                    get_logger().error(f"Error processing suggestion {i + 1}, error: {e}")

    @staticmethod
    def _truncate_if_needed(suggestion):
        max_code_suggestion_length = get_settings().get("PR_CODE_SUGGESTIONS.MAX_CODE_SUGGESTION_LENGTH", 0)
        suggestion_truncation_message = get_settings().get("PR_CODE_SUGGESTIONS.SUGGESTION_TRUNCATION_MESSAGE", "")
        if max_code_suggestion_length > 0:
            if len(suggestion['improved_code']) > max_code_suggestion_length:
                get_logger().info(f"Truncated suggestion from {len(suggestion['improved_code'])} "
                                  f"characters to {max_code_suggestion_length} characters")
                suggestion['improved_code'] = suggestion['improved_code'][:max_code_suggestion_length]
                suggestion['improved_code'] += f"\n{suggestion_truncation_message}"
        return suggestion

    def _prepare_pr_code_suggestions(self, predictions: str) -> Dict:
        data = load_yaml(predictions.strip(),
                         keys_fix_yaml=["relevant_file", "suggestion_content", "existing_code", "improved_code"],
                         first_key="code_suggestions", last_key="label")
        if isinstance(data, list):
            data = {'code_suggestions': data}

        # remove or edit invalid suggestions
        suggestion_list = []
        one_sentence_summary_list = []
        for i, suggestion in enumerate(data['code_suggestions']):
            try:
                needed_keys = ['one_sentence_summary', 'label', 'relevant_file']
                is_valid_keys = True
                for key in needed_keys:
                    if key not in suggestion:
                        is_valid_keys = False
                        get_logger().debug(
                            f"Skipping suggestion {i + 1}, because it does not contain '{key}':\n'{suggestion}")
                        break
                if not is_valid_keys:
                    continue

                if get_settings().get("pr_code_suggestions.focus_only_on_problems", False):
                    CRITICAL_LABEL = 'critical'
                    if CRITICAL_LABEL in suggestion['label'].lower(): # we want the published labels to be less declarative
                        suggestion['label'] = 'possible issue'

                if suggestion['one_sentence_summary'] in one_sentence_summary_list:
                    get_logger().debug(f"Skipping suggestion {i + 1}, because it is a duplicate: {suggestion}")
                    continue

                if 'const' in suggestion['suggestion_content'] and 'instead' in suggestion[
                    'suggestion_content'] and 'let' in suggestion['suggestion_content']:
                    get_logger().debug(
                        f"Skipping suggestion {i + 1}, because it uses 'const instead let': {suggestion}")
                    continue

                if ('existing_code' in suggestion) and ('improved_code' in suggestion):
                    suggestion = self._truncate_if_needed(suggestion)
                    one_sentence_summary_list.append(suggestion['one_sentence_summary'])
                    suggestion_list.append(suggestion)
                else:
                    get_logger().info(
                        f"Skipping suggestion {i + 1}, because it does not contain 'existing_code' or 'improved_code': {suggestion}")
            except Exception as e:
                get_logger().error(f"Error processing suggestion {i + 1}: {suggestion}, error: {e}")
        data['code_suggestions'] = suggestion_list

        return data

    async def push_inline_code_suggestions(self, data, include_coverage_footer: bool = True):
        code_suggestions = []
        fallback_comments = []
        coverage_footer = self._get_suggestions_coverage_footer() if include_coverage_footer else ""
        supports_suggestions_artifact = self.git_provider.supports_code_suggestions_artifact() is True

        if not data['code_suggestions']:
            get_logger().info('No suggestions found to improve this PR.')
            empty_coverage_footer = (self._get_suggestions_coverage_footer(suggestions_present=False)
                                     if include_coverage_footer else "")
            no_suggestions_message = ("No suggestions found in the successfully analyzed chunks."
                                      if empty_coverage_footer else "No suggestions found to improve this PR.")
            pr_body = no_suggestions_message + empty_coverage_footer
            if self.progress_response:
                return self.git_provider.edit_comment(self.progress_response,
                                                      body=pr_body)
            else:
                return self.git_provider.publish_comment(pr_body)

        for d in data['code_suggestions']:
            try:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"suggestion: {d}")
                relevant_file = d['relevant_file'].strip()
                relevant_lines_start = int(d['relevant_lines_start'])  # absolute position
                relevant_lines_end = int(d['relevant_lines_end'])
                content = d['suggestion_content'].rstrip()
                new_code_snippet = (d.get("improved_code") or "").rstrip()
                existing_code = d.get("existing_code")
                if not isinstance(existing_code, str):
                    raise TypeError("existing_code must be a string")
                label = d['label'].strip()
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                get_logger().warning(f"Could not parse suggestion: {d}, error: {e}")
                continue

            is_applicable, fallback_reason, has_valid_anchor = self._validate_suggestion(
                relevant_file, relevant_lines_start, relevant_lines_end,
                existing_code if new_code_snippet else None)
            if new_code_snippet and has_valid_anchor:
                new_code_snippet = self.dedent_code(relevant_file, relevant_lines_start, new_code_snippet)

            score = d.get("score")
            header = f"**Suggestion:** {content} [{label}, importance: {score}]" if score \
                else f"**Suggestion:** {content} [{label}]"
            if new_code_snippet and is_applicable:
                body = f"{header}\n```suggestion\n" + new_code_snippet + "\n```"
            else:
                body = header
                if new_code_snippet:
                    body += (f"\n\nProposed code (not offered as a committable change because {fallback_reason}):\n"
                             f"```\n{new_code_snippet}\n```")

            if not has_valid_anchor:
                fallback_comments.append(f"{body}\n\nLocation: `{relevant_file}:"
                                         f"{relevant_lines_start}-{relevant_lines_end}`")
            else:
                code_suggestions.append({'body': body, 'relevant_file': relevant_file,
                                         'relevant_lines_start': relevant_lines_start,
                                         'relevant_lines_end': relevant_lines_end,
                                         'original_suggestion': d})

        if code_suggestions:
            if supports_suggestions_artifact:
                is_successful = self.git_provider.publish_code_suggestions_artifact(
                    code_suggestions, artifact_footer=coverage_footer)
            else:
                is_successful = self.git_provider.publish_code_suggestions(code_suggestions)
            if not is_successful:
                get_logger().info("Failed to publish code suggestions, trying to publish each suggestion separately")
                for code_suggestion in code_suggestions:
                    self.git_provider.publish_code_suggestions([code_suggestion])
        if coverage_footer and not supports_suggestions_artifact:
            fallback_comments.append(coverage_footer.strip())
        if fallback_comments:
            self.git_provider.publish_comment("\n\n---\n\n".join(fallback_comments))

    def _get_diff_file(self, relevant_file):
        diff_files = getattr(self.git_provider, "diff_files", None)
        if diff_files is None:
            diff_files = self.git_provider.get_diff_files()
        for file in diff_files or []:
            if file.filename and file.filename.strip() == relevant_file:
                return file
        return None

    @staticmethod
    def _get_patch_range_lines(patch, relevant_lines_start, relevant_lines_end) -> Optional[List[str]]:
        target_lines = {}
        target_line = None
        target_remaining = 0
        for line in (patch or "").splitlines():
            hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if hunk_match:
                target_line = int(hunk_match.group(1))
                target_remaining = int(hunk_match.group(2) or 1)
                continue
            if target_line is None or target_remaining == 0 or line.startswith(("-", "\\")):
                continue
            if line.startswith((" ", "+")):
                if relevant_lines_start <= target_line <= relevant_lines_end:
                    target_lines[target_line] = line[1:]
                target_line += 1
                target_remaining -= 1

        if all(line_number in target_lines
               for line_number in range(relevant_lines_start, relevant_lines_end + 1)):
            return [target_lines[line_number]
                    for line_number in range(relevant_lines_start, relevant_lines_end + 1)]
        return None

    def _validate_suggestion(self, relevant_file, relevant_lines_start, relevant_lines_end,
                             existing_code) -> tuple[bool, str, bool]:
        if relevant_lines_start < 1 or relevant_lines_end < relevant_lines_start:
            return False, "the anchored range is outside the file", False

        diff_file = self._get_diff_file(relevant_file)
        if diff_file is None:
            return False, "the file content is unavailable", False
        if diff_file.head_file and getattr(diff_file, "head_file_is_complete", True):
            file_lines = diff_file.head_file.splitlines()
            if relevant_lines_end > len(file_lines):
                return False, "the anchored range is outside the file", False
            anchored_lines = file_lines[relevant_lines_start - 1:relevant_lines_end]
        else:
            anchored_lines = self._get_patch_range_lines(
                diff_file.patch, relevant_lines_start, relevant_lines_end)
            if anchored_lines is None:
                return False, "the file content is unavailable", False

        if not existing_code:
            return False, "the existing code is unavailable", True
        anchored_lines = [line.rstrip() for line in textwrap.dedent("\n".join(anchored_lines)).split("\n")]
        existing_lines = [line.rstrip() for line in textwrap.dedent(existing_code).splitlines()]
        if existing_lines != anchored_lines:
            return False, "the existing code does not match the anchored range", True
        return True, "", True

    def _suggestion_applyability(self, relevant_file, relevant_lines_start, relevant_lines_end,
                                 existing_code) -> tuple[bool, str]:
        is_applicable, fallback_reason, _ = self._validate_suggestion(
            relevant_file, relevant_lines_start, relevant_lines_end, existing_code)
        return is_applicable, fallback_reason

    def is_applicable_suggestion(self, relevant_file, relevant_lines_start, relevant_lines_end,
                                 existing_code) -> bool:
        return self._suggestion_applyability(relevant_file, relevant_lines_start,
                                             relevant_lines_end, existing_code)[0]

    @staticmethod
    def _shift_code_indentation(code_snippet: str, delta_spaces: int) -> str:
        shifted_lines = []
        for line in code_snippet.splitlines():
            if not line.strip():
                shifted_lines.append("")
                continue
            if delta_spaces > 0:
                shifted_lines.append(" " * delta_spaces + line)
            elif delta_spaces < 0:
                shift = -delta_spaces
                leading_whitespace = len(line) - len(line.lstrip())
                shifted_lines.append(line[min(shift, leading_whitespace):])
            else:
                shifted_lines.append(line)
        return "\n".join(shifted_lines)

    @staticmethod
    def _infer_space_indentation_unit(space_deltas: list[int]) -> int:
        if not space_deltas:
            return 1
        unique_deltas = sorted(set(space_deltas))
        for delta in unique_deltas:
            if delta * 2 in unique_deltas:
                return delta
        return unique_deltas[0]

    @staticmethod
    def _continuation_space_adjustments(
        lines: list[str],
        leading_whitespace: list[str],
    ) -> list[int]:
        openers = []
        adjustments = [0] * len(lines)
        closer_for = {"(": ")", "[": "]"}
        for index, (line, prefix) in enumerate(
            zip(lines, leading_whitespace, strict=True)
        ):
            stripped = line.strip()
            if not stripped:
                continue
            for opener_position in range(len(openers) - 1, -1, -1):
                opener_index, opener_prefix, closer = openers[opener_position]
                if prefix == opener_prefix and stripped.startswith(closer):
                    interior_indexes = [
                        line_index
                        for line_index in range(opener_index + 1, index)
                        if lines[line_index].strip()
                    ]
                    opener_spaces = opener_prefix.count(" ")
                    positive_offsets = [
                        leading_whitespace[line_index].count(" ") - opener_spaces
                        for line_index in interior_indexes
                        if leading_whitespace[line_index].count(" ") > opener_spaces
                    ]
                    if positive_offsets:
                        continuation_offset = min(positive_offsets)
                        for line_index in interior_indexes:
                            if leading_whitespace[line_index].count(" ") > opener_spaces:
                                adjustments[line_index] += continuation_offset
                    del openers[opener_position:]
                    break
            if stripped[-1] in closer_for:
                openers.append((index, prefix, closer_for[stripped[-1]]))
        return adjustments

    @staticmethod
    def _align_code_with_tabs(code_snippet: str, anchor_prefix: str) -> str:
        lines = code_snippet.splitlines()
        if not lines:
            return code_snippet
        leading_whitespace = [line[:len(line) - len(line.lstrip())] for line in lines]
        anchor_depth = len(anchor_prefix) - len(anchor_prefix.lstrip("\t"))
        anchor_alignment = anchor_prefix[anchor_depth:]
        initial_index, initial_prefix = next(
            (
                (index, prefix)
                for index, (line, prefix) in enumerate(
                    zip(lines, leading_whitespace, strict=True)
                )
                if line.strip()
            ),
            (0, ""),
        )
        initial_spaces = initial_prefix.count(" ")
        initial_tabs = initial_prefix.count("\t")
        continuation_adjustments = PRCodeSuggestions._continuation_space_adjustments(
            lines,
            leading_whitespace,
        )
        initial_continuation_adjustment = continuation_adjustments[initial_index]
        adjusted_initial_spaces = initial_spaces - initial_continuation_adjustment
        space_deltas = [
            abs(
                prefix.count(" ")
                - continuation_adjustments[index]
                - adjusted_initial_spaces
            )
            for index, (line, prefix) in enumerate(
                zip(lines, leading_whitespace, strict=True)
            )
            if (
                line.strip()
                and prefix.count(" ") - continuation_adjustments[index]
                != adjusted_initial_spaces
            )
        ]
        space_unit = PRCodeSuggestions._infer_space_indentation_unit(space_deltas)
        aligned_lines = []
        for index, (line, prefix) in enumerate(
            zip(lines, leading_whitespace, strict=True)
        ):
            if not line.strip():
                aligned_lines.append("")
                continue
            continuation_alignment = (
                continuation_adjustments[index] - initial_continuation_adjustment
            )
            relative_space_depth, alignment_spaces = divmod(
                prefix.count(" ")
                - initial_spaces
                - continuation_alignment,
                space_unit,
            )
            relative_depth = (
                prefix.count("\t") - initial_tabs
                + relative_space_depth
            )
            aligned_lines.append(
                "\t" * max(0, anchor_depth + relative_depth)
                + anchor_alignment
                + " " * (alignment_spaces + continuation_alignment)
                + line[len(prefix):]
            )
        return "\n".join(aligned_lines).rstrip("\n")

    def dedent_code(self, relevant_file, relevant_lines_start, new_code_snippet):
        try:  # dedent code snippet
            self.diff_files = getattr(self.git_provider, "diff_files", None)
            if self.diff_files is None:
                self.diff_files = self.git_provider.get_diff_files()
            original_initial_line = None
            for file in self.diff_files:
                if file.filename.strip() == relevant_file:
                    if file.head_file and getattr(file, "head_file_is_complete", True):
                        file_lines = file.head_file.splitlines()
                        if relevant_lines_start > len(file_lines):
                            get_logger().warning(
                                "Could not dedent code snippet, because relevant_lines_start is out of range",
                                artifact={'filename': file.filename,
                                          'file_content': file.head_file,
                                          'relevant_lines_start': relevant_lines_start,
                                          'new_code_snippet': new_code_snippet})
                            return new_code_snippet
                        else:
                            original_initial_line = file_lines[relevant_lines_start - 1]
                    else:
                        patch_lines = self._get_patch_range_lines(
                            file.patch, relevant_lines_start, relevant_lines_start)
                        if patch_lines is None:
                            get_logger().warning(
                                "Could not dedent code snippet, because file content is unavailable",
                                artifact={'filename': file.filename,
                                          'relevant_lines_start': relevant_lines_start,
                                          'new_code_snippet': new_code_snippet})
                            return new_code_snippet
                        original_initial_line = patch_lines[0]
                    break
            if original_initial_line:
                suggested_initial_line = next(
                    (line for line in new_code_snippet.splitlines() if line.strip()),
                    "",
                )
                original_initial_spaces = len(original_initial_line) - len(original_initial_line.lstrip())
                suggested_initial_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
                if original_initial_line.startswith("\t"):
                    original_prefix = original_initial_line[:original_initial_spaces]
                    new_code_snippet = self._align_code_with_tabs(new_code_snippet, original_prefix)
                else:
                    delta_spaces = original_initial_spaces - suggested_initial_spaces
                    new_code_snippet = self._shift_code_indentation(new_code_snippet, delta_spaces)
        except Exception as e:
            get_logger().error(f"Error when dedenting code snippet for file {relevant_file}, error: {e}")

        return new_code_snippet

    def validate_one_liner_suggestion_not_repeating_code(self, suggestion):
        try:
            existing_code = suggestion.get('existing_code', '').strip()
            if '...' in existing_code:
                return suggestion
            new_code = suggestion.get('improved_code', '').strip()

            relevant_file = suggestion.get('relevant_file', '').strip()
            diff_files = self.git_provider.get_diff_files()
            for file in diff_files:
                if file.filename.strip() == relevant_file:
                    # protections
                    if not file.head_file:
                        get_logger().info(f"head_file is empty")
                        return suggestion
                    head_file = file.head_file
                    base_file = file.base_file
                    if existing_code in base_file and existing_code not in head_file and new_code in head_file:
                        suggestion["score"] = 0
                        get_logger().warning(
                            f"existing_code is in the base file but not in the head file, setting score to 0",
                            artifact={"suggestion": suggestion})
        except Exception as e:
            get_logger().exception(f"Error validating one-liner suggestion", artifact={"error": e})

        return suggestion

    def remove_line_numbers(self, patches_diff_list: List[str]) -> List[str]:
        # create a copy of the patches_diff_list, without line numbers for '__new hunk__' sections
        try:
            self.patches_diff_list_no_line_numbers = []
            for patches_diff in self.patches_diff_list:
                patches_diff_lines = patches_diff.splitlines()
                for i, line in enumerate(patches_diff_lines):
                    if line.strip():
                        if line.isnumeric():
                            patches_diff_lines[i] = ''
                        elif line[0].isdigit():
                            # find the first letter in the line that starts with a valid letter
                            for j, char in enumerate(line):
                                if not char.isdigit():
                                    patches_diff_lines[i] = line[j + 1:]
                                    break
                self.patches_diff_list_no_line_numbers.append('\n'.join(patches_diff_lines))
            return self.patches_diff_list_no_line_numbers
        except Exception as e:
            get_logger().error(f"Error removing line numbers from patches_diff_list, error: {e}")
            return patches_diff_list

    async def prepare_prediction_main(self, model: str) -> dict:
        self.failed_chunk_count = 0
        self.total_chunk_count = 0
        # get PR diff
        if get_settings().pr_code_suggestions.decouple_hunks:
            self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                        self.token_handler,
                                                        model,
                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                        add_line_numbers=True)  # decouple hunk with line numbers
            self.patches_diff_list_no_line_numbers = self.remove_line_numbers(self.patches_diff_list)  # decouple hunk

        else:
            # non-decoupled hunks
            self.patches_diff_list_no_line_numbers = get_pr_multi_diffs(self.git_provider,
                                                                        self.token_handler,
                                                                        model,
                                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                                        add_line_numbers=False)
            self.patches_diff_list = await self.convert_to_decoupled_with_line_numbers(
                self.patches_diff_list_no_line_numbers, model)
            if not self.patches_diff_list:
                # fallback to decoupled hunks
                self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                            self.token_handler,
                                                            model,
                                                            max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                            add_line_numbers=True)  # decouple hunk with line numbers

        if self.patches_diff_list:
            get_logger().info(f"Number of PR chunk calls: {len(self.patches_diff_list)}")
            get_logger().debug(f"PR diff:", artifact=self.patches_diff_list)

            prediction_list = []
            chunk_errors = []
            chunk_pairs = list(zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers))
            self.total_chunk_count = len(chunk_pairs)

            # parallelize calls to AI:
            if get_settings().pr_code_suggestions.parallel_calls:
                prediction_results = await asyncio.gather(
                    *[self._get_prediction(model, patches_diff, patches_diff_no_line_numbers) for
                      patches_diff, patches_diff_no_line_numbers in chunk_pairs],
                    return_exceptions=True)
                for chunk_index, prediction in enumerate(prediction_results):
                    if isinstance(prediction, Exception):
                        chunk_errors.append(prediction)
                        get_logger().warning(
                            f"Failed to generate code suggestions for chunk {chunk_index + 1}; "
                            "retaining successful chunks",
                            artifact={"error": prediction},
                        )
                    elif isinstance(prediction, BaseException):
                        raise prediction
                    else:
                        prediction_list.append(prediction)
            else:
                for chunk_index, (patches_diff, patches_diff_no_line_numbers) in enumerate(
                        chunk_pairs):
                    try:
                        prediction = await self._get_prediction(model, patches_diff, patches_diff_no_line_numbers)
                    except Exception as e:
                        chunk_errors.append(e)
                        get_logger().warning(
                            f"Failed to generate code suggestions for chunk {chunk_index + 1}; "
                            "retaining successful chunks",
                            artifact={"error": e},
                        )
                    else:
                        prediction_list.append(prediction)

            self.failed_chunk_count = len(chunk_errors)
            if chunk_errors and not prediction_list:
                raise chunk_errors[0]
            self.prediction_list = prediction_list

            data = {"code_suggestions": []}
            for j, predictions in enumerate(prediction_list):  # each call adds an element to the list
                if "code_suggestions" in predictions:
                    score_threshold = max(1, int(get_settings().pr_code_suggestions.suggestions_score_threshold))
                    for i, prediction in enumerate(predictions["code_suggestions"]):
                        try:
                            score = int(prediction.get("score", 1))
                            if score >= score_threshold:
                                data["code_suggestions"].append(prediction)
                            else:
                                get_logger().info(
                                    f"Removing suggestions {i} from call {j}, because score is {score}, and score_threshold is {score_threshold}",
                                    artifact=prediction)
                        except Exception as e:
                            get_logger().error(f"Error getting PR diff for suggestion {i} in call {j}, error: {e}",
                                               artifact={"prediction": prediction})
            self.data = data
        else:
            get_logger().warning(f"Empty PR diff list")
            self.data = data = None
        return data

    async def convert_to_decoupled_with_line_numbers(self, patches_diff_list_no_line_numbers, model) -> List[str]:
        with get_logger().contextualize(sub_feature='convert_to_decoupled_with_line_numbers'):
            try:
                patches_diff_list = []
                for patch_prompt in patches_diff_list_no_line_numbers:
                    file_prefix = "## File: "
                    patches = patch_prompt.strip().split(f"\n{file_prefix}")
                    patches_new = copy.deepcopy(patches)
                    for i in range(len(patches_new)):
                        if i == 0:
                            prefix = patches_new[i].split("\n@@")[0].strip()
                        else:
                            prefix = file_prefix + patches_new[i].split("\n@@")[0][1:]
                            prefix = prefix.strip()
                        patches_new[i] = prefix + '\n\n' + decouple_and_convert_to_hunks_with_lines_numbers(patches_new[i],
                                                                                                          file=None).strip()
                        patches_new[i] = patches_new[i].strip()
                    patch_final = "\n\n\n".join(patches_new)
                    if model in MAX_TOKENS:
                        max_tokens_full = MAX_TOKENS[
                            model]  # note - here we take the actual max tokens, without any reductions. we do aim to get the full documentation website in the prompt
                    else:
                        max_tokens_full = get_max_tokens(model)
                    delta_output = 2000
                    token_count = self.token_handler.count_tokens(patch_final)
                    if token_count > max_tokens_full - delta_output:
                        get_logger().warning(
                            f"Token count {token_count} exceeds the limit {max_tokens_full - delta_output}. clipping the tokens")
                        patch_final = clip_tokens(patch_final, max_tokens_full - delta_output)
                    patches_diff_list.append(patch_final)
                return patches_diff_list
            except Exception as e:
                get_logger().exception(f"Error converting to decoupled with line numbers",
                                       artifact={'patches_diff_list_no_line_numbers': patches_diff_list_no_line_numbers})
                return []

    def generate_summarized_suggestions(self, data: Dict) -> str:
        try:
            pr_body = f"{format_pr_code_suggestions_header()}\n\n"

            if len(data.get('code_suggestions', [])) == 0:
                pr_body += "No suggestions found to improve this PR."
                return pr_body

            if get_settings().config.is_auto_command:
                pr_body += "Explore these optional code suggestions:\n\n"

            language_extension_map_org = get_settings().language_extension_map_org
            extension_to_language = {}
            for language, extensions in language_extension_map_org.items():
                for ext in extensions:
                    extension_to_language[ext] = language

            pr_body += "<table>"
            header = f"Suggestion"
            delta = 66
            header += "&nbsp; " * delta
            pr_body += f"""<thead><tr><td><strong>Category</strong></td><td align=left><strong>{header}</strong></td><td align=center><strong>Impact</strong></td></tr>"""
            pr_body += """<tbody>"""
            suggestions_labels = dict()
            # add all suggestions related to each label
            for suggestion in data['code_suggestions']:
                label = suggestion['label'].strip().strip("'").strip('"')
                if label not in suggestions_labels:
                    suggestions_labels[label] = []
                suggestions_labels[label].append(suggestion)

            # sort suggestions_labels by the suggestion with the highest score
            suggestions_labels = dict(
                sorted(suggestions_labels.items(), key=lambda x: max([s['score'] for s in x[1]]), reverse=True))
            # sort the suggestions inside each label group by score
            for label, suggestions in suggestions_labels.items():
                suggestions_labels[label] = sorted(suggestions, key=lambda x: x['score'], reverse=True)

            counter_suggestions = 0
            for label, suggestions in suggestions_labels.items():
                num_suggestions = len(suggestions)
                pr_body += f"""<tr><td rowspan={num_suggestions}>{label.capitalize()}</td>\n"""
                for i, suggestion in enumerate(suggestions):

                    relevant_file = suggestion['relevant_file'].strip()
                    relevant_lines_start = int(suggestion['relevant_lines_start'])
                    relevant_lines_end = int(suggestion['relevant_lines_end'])
                    range_str = ""
                    if relevant_lines_start == relevant_lines_end:
                        range_str = f"[{relevant_lines_start}]"
                    else:
                        range_str = f"[{relevant_lines_start}-{relevant_lines_end}]"

                    try:
                        code_snippet_link = self.git_provider.get_line_link(relevant_file, relevant_lines_start,
                                                                            relevant_lines_end)
                    except:
                        code_snippet_link = ""
                    # add html table for each suggestion

                    suggestion_content = suggestion['suggestion_content'].rstrip()
                    CHAR_LIMIT_PER_LINE = 84
                    suggestion_content = insert_br_after_x_chars(suggestion_content, CHAR_LIMIT_PER_LINE)
                    # pr_body += f"<tr><td><details><summary>{suggestion_content}</summary>"
                    existing_code = suggestion["existing_code"].rstrip()
                    if existing_code:
                        existing_code = self.dedent_code(relevant_file, relevant_lines_start, existing_code)
                    existing_code += "\n"
                    improved_code = suggestion["improved_code"].rstrip()
                    if improved_code:
                        improved_code = self.dedent_code(relevant_file, relevant_lines_start, improved_code)
                    improved_code += "\n"

                    diff = difflib.unified_diff(existing_code.split('\n'),
                                                improved_code.split('\n'), n=999)
                    patch_orig = "\n".join(diff)
                    patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')

                    example_code = ""
                    example_code += f"```diff\n{patch.rstrip()}\n```\n"
                    if i == 0:
                        pr_body += f"""<td>\n\n"""
                    else:
                        pr_body += f"""<tr><td>\n\n"""
                    suggestion_summary = suggestion['one_sentence_summary'].strip().rstrip('.')
                    if "'<" in suggestion_summary and ">'" in suggestion_summary:
                        # escape the '<' and '>' characters, otherwise they are interpreted as html tags
                        get_logger().info(f"Escaped suggestion summary: {suggestion_summary}")
                        suggestion_summary = suggestion_summary.replace("'<", "`<")
                        suggestion_summary = suggestion_summary.replace(">'", ">`")
                    if '`' in suggestion_summary:
                        suggestion_summary = replace_code_tags(suggestion_summary)

                    pr_body += f"""\n\n<details><summary>{suggestion_summary}</summary>\n\n___\n\n"""
                    pr_body += f"""
**{suggestion_content}**

[{relevant_file} {range_str}]({code_snippet_link})

{example_code.rstrip()}
"""
                    if suggestion.get('score_why'):
                        pr_body += f"<details><summary>Suggestion importance[1-10]: {suggestion['score']}</summary>\n\n"
                        pr_body += f"__\n\nWhy: {suggestion['score_why']}\n\n"
                        pr_body += f"</details>"

                    pr_body += f"</details>"

                    # # add another column for 'score'
                    score_int = int(suggestion.get('score', 0))
                    score_str = f"{score_int}"
                    if get_settings().pr_code_suggestions.new_score_mechanism:
                        score_str = self.get_score_str(score_int)
                    pr_body += f"</td><td align=center>{score_str}\n\n"

                    pr_body += f"</td></tr>"
                    counter_suggestions += 1

                # pr_body += "</details>"
                # pr_body += """</td></tr>"""
            pr_body += """</tr></tbody></table>"""
            return pr_body
        except Exception as e:
            get_logger().info(f"Failed to publish summarized code suggestions, error: {e}")
            return ""

    def get_score_str(self, score: int) -> str:
        th_high = get_settings().pr_code_suggestions.get('new_score_mechanism_th_high', 9)
        th_medium = get_settings().pr_code_suggestions.get('new_score_mechanism_th_medium', 7)
        if score >= th_high:
            return "High"
        elif score >= th_medium:
            return "Medium"
        else:  # score < 7
            return "Low"

    async def self_reflect_on_suggestions(self,
                                          suggestion_list: List,
                                          patches_diff: str,
                                          model: str,
                                          prev_suggestions_str: str = "",
                                          dedicated_prompt: str = "") -> str:
        if not suggestion_list:
            return ""

        try:
            suggestion_str = ""
            for i, suggestion in enumerate(suggestion_list):
                suggestion_str += f"suggestion {i + 1}: " + str(suggestion) + '\n\n'

            variables = {'suggestion_list': suggestion_list,
                         'suggestion_str': suggestion_str,
                         "diff": patches_diff,
                         'num_code_suggestions': len(suggestion_list),
                         'prev_suggestions_str': prev_suggestions_str,
                         "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
                         'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False)}
            environment = Environment(undefined=StrictUndefined)

            if dedicated_prompt:
                system_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).user).render(variables)
            else:
                system_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.user).render(variables)

            with get_logger().contextualize(command="self_reflect_on_suggestions"):
                response_reflect, finish_reason_reflect = await self.ai_handler.chat_completion(model=model,
                                                                                                system=system_prompt_reflect,
                                                                                                temperature=get_settings().config.temperature,
                                                                                                user=user_prompt_reflect)
        except Exception as e:
            get_logger().info(f"Could not reflect on suggestions, error: {e}")
            return ""
        return response_reflect
