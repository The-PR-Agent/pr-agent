# Configuration Reference

<!-- This page is generated from pr_agent/settings/configuration.toml by
     scripts/generate_config_reference.py. Do not edit it by hand: change the
     TOML (keys and comments) and re-run the script. -->

All PR-Agent options with their default values, generated from
[`configuration.toml`](https://github.com/qodo-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml)
— the single source of truth for configuration. Override any of these in your
repo's `.pr_agent.toml`, via CLI arguments (`--section.key=value`), or with
environment variables. See the
[usage guide](./configuration_options.md) for how overrides are merged.


## `[config]`

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `"gpt-5.5-2026-04-23"` |  |
| `fallback_models` | `["gpt-5.4-mini"]` |  |
| `git_provider` | `"github"` |  |
| `publish_output` | `true` |  |
| `publish_output_progress` | `true` |  |
| `progress_gif_url` | `""` | optional, override for the progress loading gif url (example: 'https://.../spinner.gif'). |
| `progress_gif_width` | `48` | optional, width (in px) of the progress loading gif. |
| `verbosity_level` | `0` | 0,1,2 |
| `use_extra_bad_extensions` | `false` |  |
| `log_level` | `"DEBUG"` |  |
| `use_wiki_settings_file` | `true` |  |
| `use_repo_settings_file` | `true` |  |
| `use_global_settings_file` | `true` |  |
| `extra_config_url` | `""` | optional URL or path to an additional .pr_agent.toml merged before the repo-local config; also settable via --extra_config_url or PR_AGENT_EXTRA_CONFIG_URL. See docs/docs/usage-guide/configuration_options.md#external-configuration-url. |
| `disable_auto_feedback` | `false` |  |
| `ai_timeout` | `120` | 2 minutes |
| `skip_keys` | `[]` |  |
| `custom_reasoning_model` | `false` | when true, disables system messages and temperature controls for models that don't support chat-style inputs |
| `response_language` | `"en-US"` | Language locales code for PR responses in ISO 3166 and ISO 639 format (e.g., "en-US", "it-IT", "zh-CN", ...) |
| `max_description_tokens` | `500` |  |
| `max_commits_tokens` | `500` |  |
| `max_model_tokens` | `32000` | Limits the maximum number of tokens that can be used by any model, regardless of the model's default capabilities. |
| `custom_model_max_tokens` | `-1` | for models not in the default list; when unset, litellm model metadata is used as a final fallback |
| `model_token_count_estimate_factor` | `0.3` | factor to increase the token count estimate, in order to reduce likelihood of model failure due to too many tokens - applicable only when requesting an accurate estimate. |
| `patch_extension_skip_types` | `[".md",".txt"]` |  |
| `allow_dynamic_context` | `true` |  |
| `max_extra_lines_before_dynamic_context` | `10` | will try to include up to 10 extra lines before the hunk in the patch, until we reach an enclosing function or class |
| `patch_extra_lines_before` | `5` | Number of extra lines (+3 default ones) to include before each hunk in the patch |
| `patch_extra_lines_after` | `1` | Number of extra lines (+3 default ones) to include after each hunk in the patch |
| `secret_provider` | `""` | "" (disabled), "google_cloud_storage", or "aws_secrets_manager" for secure secret management |
| `cli_mode` | `false` |  |
| `output_relevant_configurations` | `false` |  |
| `large_patch_policy` | `"clip"` | "clip", "skip" |
| `duplicate_prompt_examples` | `false` |  |
| `seed` | `-1` | set positive value to fix the seed (and ensure temperature=0) |
| `temperature` | `0.2` |  |
| `ignore_pr_title` | `["^\\[Auto\\]", "^Auto"]` | a list of regular expressions to match against the PR title to ignore the PR agent |
| `ignore_pr_target_branches` | `[]` | a list of regular expressions of target branches to ignore from PR agent when an PR is created |
| `ignore_pr_source_branches` | `[]` | a list of regular expressions of source branches to ignore from PR agent when an PR is created |
| `ignore_pr_labels` | `[]` | labels to ignore from PR agent when an PR is created |
| `ignore_pr_authors` | `[]` | authors to ignore from PR agent when an PR is created |
| `ignore_repositories` | `[]` | a list of regular expressions of repository full names (e.g. "org/repo") to ignore from PR agent processing |
| `ignore_language_framework` | `[]` | a list of code-generation languages or frameworks (e.g. 'protobuf', 'go_gen') whose auto-generated source files will be excluded from analysis |
| `restricted_mode` | `false` | when true, skip operations that require elevated permissions (e.g. pushing code to the repository) |
| `enable_diff_files_cache` | `false` | cache fetched diff files across requests, keyed by PR head commit; saves provider API calls when several commands run against the same PR head |
| `diff_files_cache_ttl` | `300` | seconds a cached diff stays valid |
| `diff_files_cache_max_entries` | `50` | max PRs kept in the diff cache (FIFO eviction) |
| `is_auto_command` | `false` | will be auto-set to true if the command is triggered by an automation |
| `enable_ai_metadata` | `false` | will enable adding ai metadata |
| `reasoning_effort` | `"medium"` | "none", "minimal", "low", "medium", "high", "xhigh" |
| `enable_claude_extended_thinking` | `false` | Set to true to enable extended thinking feature |
| `extended_thinking_budget_tokens` | `2048` |  |
| `extended_thinking_max_output_tokens` | `4096` |  |
| `extract_issue_from_branch` | `true` | Extract issue number from PR source branch name (e.g. feature/1-auth-google -> issue #1). When true, branch-derived issue URLs are merged with tickets from the PR description for compliance. Set to false to restore description-only behaviour. Note: Branch-name extraction is GitHub-only for now; other providers planned for later. |
| `branch_issue_regex` | `""` | Optional: custom regex with exactly one capturing group for the issue number (validated at runtime; falls back to default if missing). If empty, uses default pattern: first 1-6 digits at start of branch or after a slash, followed by hyphen or end (e.g. feature/1-test, 123-fix). GitHub only; other providers planned for later. |

## `[pr_reviewer]`

| Key | Default | Description |
|-----|---------|-------------|
| `require_score_review` | `false` |  |
| `require_tests_review` | `true` |  |
| `require_estimate_effort_to_review` | `true` |  |
| `require_can_be_split_review` | `false` |  |
| `require_security_review` | `true` |  |
| `require_estimate_contribution_time_cost` | `false` |  |
| `require_todo_scan` | `false` |  |
| `require_ticket_analysis_review` | `true` |  |
| `publish_output_no_suggestions` | `true` | Set to "false" if you only need the reviewer's remarks (not labels, not "security audit", etc.) and want to avoid noisy "No major issues detected" comments. |
| `persistent_comment` | `true` |  |
| `extra_instructions` | `""` |  |
| `num_max_findings` | `3` |  |
| `final_update_message` | `true` |  |
| `enable_review_labels_security` | `true` |  |
| `enable_review_labels_effort` | `true` |  |
| `require_all_thresholds_for_incremental_review` | `false` | specific configurations for incremental review (/review -i) |
| `minimal_commits_for_incremental_review` | `0` |  |
| `minimal_minutes_for_incremental_review` | `0` |  |
| `enable_intro_text` | `true` |  |
| `enable_help_text` | `false` | Determines whether to include help text in the PR review. Enabled by default. |

## `[pr_description]`

| Key | Default | Description |
|-----|---------|-------------|
| `publish_labels` | `false` |  |
| `add_original_user_description` | `true` |  |
| `generate_ai_title` | `false` |  |
| `use_bullet_points` | `true` |  |
| `extra_instructions` | `""` |  |
| `enable_pr_type` | `true` |  |
| `final_update_message` | `true` |  |
| `enable_help_text` | `false` |  |
| `enable_help_comment` | `false` |  |
| `enable_pr_diagram` | `true` | adds a section with a diagram of the PR changes |
| `publish_description_as_comment` | `false` |  |
| `publish_description_as_comment_persistent` | `true` |  |
| `enable_semantic_files_types` | `true` |  |
| `collapsible_file_list` | `'adaptive'` | true, false, 'adaptive' |
| `collapsible_file_list_threshold` | `6` |  |
| `inline_file_summary` | `false` | false, true, 'table' |
| `use_description_markers` | `false` |  |
| `enable_large_pr_handling` | `true` |  |
| `include_generated_by_header` | `true` |  |
| `max_ai_calls` | `4` |  |
| `async_ai_calls` | `true` |  |

## `[pr_questions]`

| Key | Default | Description |
|-----|---------|-------------|
| `enable_help_text` | `false` |  |
| `use_conversation_history` | `true` |  |
| `extra_instructions` | `""` |  |

## `[pr_code_suggestions]`

| Key | Default | Description |
|-----|---------|-------------|
| `commitable_code_suggestions` | `false` |  |
| `dual_publishing_score_threshold` | `-1` | -1 to disable, [0-10] to set the threshold (>=) for publishing a code suggestion both in a table and as committable |
| `focus_only_on_problems` | `true` |  |
| `extra_instructions` | `""` |  |
| `enable_help_text` | `false` |  |
| `enable_chat_text` | `false` |  |
| `persistent_comment` | `true` |  |
| `max_history_len` | `4` |  |
| `publish_output_no_suggestions` | `true` |  |
| `suggestions_score_threshold` | `0` | [0-10]\| recommend not to set this value above 8, since above it may clip highly relevant suggestions |
| `new_score_mechanism` | `true` |  |
| `new_score_mechanism_th_high` | `9` |  |
| `new_score_mechanism_th_medium` | `7` |  |
| `auto_extended_mode` | `true` | params for '/improve --extended' mode |
| `num_code_suggestions_per_chunk` | `3` |  |
| `max_number_of_calls` | `3` |  |
| `parallel_calls` | `true` |  |
| `final_clip_factor` | `0.8` |  |
| `decouple_hunks` | `false` |  |
| `demand_code_suggestions_self_review` | `false` | add a checkbox for the author to self-review the code suggestions |
| `code_suggestions_self_review_text` | `"**Author self-review**: I have reviewed the PR code suggestions, and addressed the relevant ones."` |  |
| `approve_pr_on_self_review` | `false` | if true, the PR will be auto-approved after the author clicks on the self-review checkbox |
| `fold_suggestions_on_self_review` | `true` | if true, the code suggestions will be folded after the author clicks on the self-review checkbox |

## `[pr_custom_prompt]`

| Key | Default | Description |
|-----|---------|-------------|
| `prompt` | `"""\` |  |
| `suggestions_score_threshold` | `0` |  |
| `num_code_suggestions_per_chunk` | `3` |  |
| `self_reflect_on_custom_suggestions` | `true` |  |
| `enable_help_text` | `false` |  |

## `[pr_add_docs]`

| Key | Default | Description |
|-----|---------|-------------|
| `extra_instructions` | `""` |  |
| `docs_style` | `"Sphinx"` | "Google Style with Args, Returns, Attributes...etc", "Numpy Style", "Sphinx Style", "PEP257", "reStructuredText" |
| `file` | `""` | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | `""` | in case there are several methods with the same name in the same file, you can specify the relevant class name |

## `[pr_update_changelog]`

| Key | Default | Description |
|-----|---------|-------------|
| `push_changelog_changes` | `false` |  |
| `extra_instructions` | `""` |  |
| `add_pr_link` | `true` |  |
| `skip_ci_on_push` | `true` |  |

## `[pr_analyze]`

| Key | Default | Description |
|-----|---------|-------------|
| `enable_help_text` | `true` |  |

## `[pr_test]`

| Key | Default | Description |
|-----|---------|-------------|
| `extra_instructions` | `""` |  |
| `testing_framework` | `""` | specify the testing framework you want to use |
| `num_tests` | `3` | number of tests to generate. max 5. |
| `avoid_mocks` | `true` | if true, the generated tests will prefer to use real objects instead of mocks |
| `file` | `""` | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | `""` | in case there are several methods with the same name in the same file, you can specify the relevant class name |
| `enable_help_text` | `false` |  |

## `[pr_improve_component]`

| Key | Default | Description |
|-----|---------|-------------|
| `num_code_suggestions` | `4` |  |
| `extra_instructions` | `""` |  |
| `file` | `""` | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | `""` | in case there are several methods with the same name in the same file, you can specify the relevant class name |

## `[pr_help]`

| Key | Default | Description |
|-----|---------|-------------|
| `force_local_db` | `false` |  |
| `num_retrieved_snippets` | `5` |  |

## `[pr_help_docs]`

| Key | Default | Description |
|-----|---------|-------------|
| `repo_url` | `""` | If not overwritten, will use the repo from where the context came from (issue or PR) |
| `repo_default_branch` | `"main"` |  |
| `docs_path` | `"docs"` |  |
| `exclude_root_readme` | `false` |  |
| `supported_doc_exts` | `[".md", ".mdx", ".rst"]` |  |
| `enable_help_text` | `false` |  |

## `[github]`

| Key | Default | Description |
|-----|---------|-------------|
| `deployment_type` | `"user"` | The type of deployment to create. Valid values are 'app' or 'user'. |
| `ratelimit_retries` | `5` |  |
| `base_url` | `"https://api.github.com"` |  |
| `publish_inline_comments_fallback_with_verification` | `true` |  |
| `try_fix_invalid_inline_comments` | `true` |  |
| `app_name` | `"pr-agent"` |  |
| `ignore_bot_pr` | `true` |  |
| `publish_as_check_run` | `false` | when true, publish review/description/improve output as GitHub Checks instead of PR comments |

## `[github_app]`

| Key | Default | Description |
|-----|---------|-------------|
| `bot_user` | `"github-actions[bot]"` | these toggles allows running the github app from custom deployments |
| `override_deployment_type` | `true` |  |
| `handle_pr_actions` | `['opened', 'reopened', 'ready_for_review']` | settings for "pull_request" event |
| `pr_commands` | `[` |  |
| `handle_push_trigger` | `false` | settings for "pull_request" event with "synchronize" action - used to detect and handle push triggers for new commits |
| `push_trigger_ignore_bot_commits` | `true` |  |
| `push_trigger_ignore_merge_commits` | `true` |  |
| `push_trigger_wait_for_initial_review` | `true` |  |
| `push_trigger_pending_tasks_backlog` | `true` |  |
| `push_trigger_pending_tasks_ttl` | `300` |  |
| `push_commands` | `[` |  |

## `[gitlab]`

| Key | Default | Description |
|-----|---------|-------------|
| `url` | `"https://gitlab.com"` |  |
| `expand_submodule_diffs` | `false` |  |
| `pr_commands` | `[` |  |
| `handle_push_trigger` | `false` |  |
| `push_commands` | `[` |  |

## `[gitea]`

| Key | Default | Description |
|-----|---------|-------------|
| `url` | `"https://gitea.com"` |  |
| `handle_push_trigger` | `false` |  |
| `pr_commands` | `[` |  |
| `push_commands` | `[` |  |

## `[bitbucket_app]`

| Key | Default | Description |
|-----|---------|-------------|
| `pr_commands` | `[` |  |
| `avoid_full_files` | `false` |  |

## `[bitbucket_server]`

| Key | Default | Description |
|-----|---------|-------------|
| `url` | `""` | URL to the BitBucket Server instance |
| `pr_commands` | `[` |  |

## `[litellm]`

| Key | Default | Description |
|-----|---------|-------------|
| `enable_callbacks` | `false` |  |
| `success_callback` | `[]` |  |
| `failure_callback` | `[]` |  |
| `service_callback` | `[]` |  |

## `[pr_similar_issue]`

| Key | Default | Description |
|-----|---------|-------------|
| `skip_comments` | `false` |  |
| `force_update_dataset` | `false` |  |
| `max_issues_to_scan` | `500` |  |
| `vectordb` | `"pinecone"` | options: "pinecone", "lancedb", "qdrant" |

## `[pr_find_similar_component]`

| Key | Default | Description |
|-----|---------|-------------|
| `class_name` | `""` |  |
| `file` | `""` |  |
| `search_from_org` | `false` |  |
| `allow_fallback_less_words` | `true` |  |
| `number_of_keywords` | `5` |  |
| `number_of_results` | `5` |  |

## `[lancedb]`

| Key | Default | Description |
|-----|---------|-------------|
| `uri` | `"./lancedb"` |  |

## `[skills]`

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Agent skills (SKILL.md) support: discovers SKILL.md files from the configured filesystem paths and injects their content into review/improve/describe prompts. Sibling *.md files in the skill directory tree (e.g. references/guide.md) are inlined alongside SKILL.md. PR-Agent supports text-only skills: scripts/ and assets/ subdirectories are skipped because PR-Agent uses a single-shot model call (no tool-use loop) and cannot execute scripts or load binary assets on demand. Skills that depend on script execution will not work here. |
| `paths` | `[]` | directories to scan recursively for "*/SKILL.md"; supports ~ and $VAR |
| `max_skills_tokens` | `8000` | token budget for the combined skills_context block |

## `[best_practices]`

| Key | Default | Description |
|-----|---------|-------------|
| `content` | `""` |  |
| `organization_name` | `""` |  |
| `max_lines_allowed` | `800` |  |
| `enable_global_best_practices` | `false` |  |

## `[auto_best_practices]`

| Key | Default | Description |
|-----|---------|-------------|
| `enable_auto_best_practices` | `true` | public - general flag to disable all auto best practices usage |
| `utilize_auto_best_practices` | `true` | public - disable usage of auto best practices in the 'improve' tool |
| `extra_instructions` | `""` | public - extra instructions to the auto best practices generation prompt |
| `content` | `""` |  |
| `max_patterns` | `5` | max number of patterns to be detected |

## `[azure_devops]`

| Key | Default | Description |
|-----|---------|-------------|
| `default_comment_status` | `"closed"` |  |

## `[azure_devops_server]`

| Key | Default | Description |
|-----|---------|-------------|
| `pr_commands` | `[` |  |
