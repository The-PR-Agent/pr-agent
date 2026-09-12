# Configuration Reference

> This page is **auto-generated** and should not be edited by hand.
> Regenerate it from the [TOML source](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) with:
>
> ```bash
> python scripts/generate_config_reference.py
> ```

Every configuration option PR-Agent supports, grouped by section. The [configuration.toml](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml)
file is the single source of truth for defaults and inline comments; this page renders the same
list for easy searching and linking.

Rows with an empty **Description** are keys whose TOML entry carries no explanatory comment yet.
They are listed deliberately rather than hidden, so the gaps double as the documentation
to-do list.

## `[config]`

**models**

| Key | Default | Description |
| --- | --- | --- |
| `model` | "gpt-5.6" |  |
| `fallback_models` | ["gpt-5.6-terra"] |  |
**CLI**

| Key | Default | Description |
| --- | --- | --- |
| `git_provider` | "github" |  |
| `publish_output` | true |  |
| `publish_output_progress` | true |  |
| `progress_gif_url` | "" | optional, override for the progress loading gif url (example: 'https://.../spinner.gif'). |
| `progress_gif_width` | 48 | optional, width (in px) of the progress loading gif. |
| `verbosity_level` | 0 | 0,1,2 |
| `use_extra_bad_extensions` | false |  |
**Log**

| Key | Default | Description |
| --- | --- | --- |
| `log_level` | "DEBUG" |  |
**Configurations**

| Key | Default | Description |
| --- | --- | --- |
| `use_repo_settings_file` | true |  |
| `use_global_settings_file` | true |  |
| `extra_config_url` | "" | optional URL or path to an additional .pr_agent.toml merged before the repo-local config; also settable via --extra_config_url or PR_AGENT_EXTRA_CONFIG_URL. See docs/docs/usage-guide/configuration_options.md#external-configuration-url. |
| `disable_auto_feedback` | false |  |
| `enable_auto_approval` | false | when true, /review may auto-approve a PR via auto_approve_logic(); that caller is currently commented out |
| `ai_timeout` | 120 | 2 minutes |
| `retry_same_model_on_timeout` | true | when false, a timed-out call is not retried on the same model and moves on to fallback_models |
| `skip_keys` | [] |  |
| `custom_reasoning_model` | false | when true, disables system messages and temperature controls for models that don't support chat-style inputs |
| `response_language` | "en-US" | Language locales code for PR responses in ISO 3166 and ISO 639 format (e.g., "en-US", "it-IT", "zh-CN", ...) |
| `repo_context_files` | ["AGENTS.md"] | Repository-relative files (e.g. AGENTS.md, CLAUDE.md) to include as AI prompt context; set to [] to disable |
| `repo_context_from_default_branch` | true | Read repo context files from the repository default branch (trusts only default-branch content). Set to false to read from the PR target branch instead. |
| `repo_context_max_lines` | 500 | Maximum total rendered lines for repo context, including wrapper tags |
**token limits**

| Key | Default | Description |
| --- | --- | --- |
| `max_description_tokens` | 500 |  |
| `max_commits_tokens` | 500 |  |
| `max_model_tokens` | 32000 | Limits the maximum number of tokens that can be used by any model, regardless of the model's default capabilities. |
| `custom_model_max_tokens` | -1 | for models not in the default list |
| `max_output_tokens` | 0 | 0 = unset (the provider's own default applies) |
| `model_token_count_estimate_factor` | 0.3 | factor to increase the token count estimate, in order to reduce likelihood of model failure due to too many tokens - applicable only when requesting an accurate estimate. |
**patch extension logic**

| Key | Default | Description |
| --- | --- | --- |
| `patch_extension_skip_types` | [".md", ".txt"] |  |
| `allow_dynamic_context` | true |  |
| `max_extra_lines_before_dynamic_context` | 10 | will try to include up to 10 extra lines before the hunk in the patch, until we reach an enclosing function or class |
| `patch_extra_lines_before` | 5 | Number of extra lines (+3 default ones) to include before each hunk in the patch |
| `patch_extra_lines_after` | 1 | Number of extra lines (+3 default ones) to include after each hunk in the patch |
| `secret_provider` | "" | "" (disabled), "google_cloud_storage", or "aws_secrets_manager" for secure secret management |
| `cli_mode` | false |  |
| `output_relevant_configurations` | false |  |
| `output_run_details` | false | if true, append an agent run details section (model, tokens, time cost, AI calls) to generated PR comments |
| `output_run_cost` | false | if true, collect estimated LiteLLM API cost and include it inside the enabled run details section |
| `large_patch_policy` | "clip" | "clip", "skip" |
| `duplicate_prompt_examples` | false |  |
| `persistent_inline_comments` | false | Persistent inline comments (issue #2037): when true, the GitHub, GitLab, and Azure DevOps providers fingerprint each inline comment, embed the fingerprint as an HTML marker, and skip re-posting suggestions already present on the PR/MR across runs. |
**seed**

| Key | Default | Description |
| --- | --- | --- |
| `seed` | -1 | set positive value to fix the seed (and ensure temperature=0) |
| `temperature` | 0.2 |  |
**ignore logic**

| Key | Default | Description |
| --- | --- | --- |
| `ignore_pr_title` | ["^\\[Auto\\]", "^Auto"] | a list of regular expressions to match against the PR title to ignore the PR agent |
| `ignore_pr_target_branches` | [] | a list of regular expressions of target branches to ignore from PR agent when an PR is created |
| `ignore_pr_source_branches` | [] | a list of regular expressions of source branches to ignore from PR agent when an PR is created |
| `ignore_pr_labels` | [] | labels to ignore from PR agent when an PR is created |
| `ignore_pr_authors` | [] | authors to ignore from PR agent when an PR is created |
| `reaction_on_start` | "eyes" | added before the command runs |
| `reaction_on_success` | "" | added when the command finished successfully |
| `reaction_on_failure` | "" | added when the command failed |
| `ignore_repositories` | [] | a list of regular expressions of repository full names (e.g. "org/repo") to ignore from PR agent processing |
| `ignore_language_framework` | [] | a list of code-generation languages or frameworks (e.g. 'protobuf', 'go_gen') whose auto-generated source files will be excluded from analysis |
| `bot_user_indicators` | ["codium", "bot_", "bot-", "_bot", "-bot"] | Substring indicators used to skip bot users on webhook events. Currently consumed by the GitLab webhook (`is_bot_user`); other providers may adopt this list in future. The match is case-insensitive against the sender's display name. Overriding this setting REPLACES the default list — include the entries below in your override if you want to keep them (e.g. `["codium", "bot_", "bot-", "_bot", "-bot", "renovate"]`). |
| `restricted_mode` | false | when true, skip operations that require elevated permissions (e.g. pushing code to the repository) |
| `is_auto_command` | false | will be auto-set to true if the command is triggered by an automation |
| `propagate_tool_errors` | false | when true, a tool re-raises instead of swallowing an internal error, so a caller can tell a failed run from an empty one |
| `enable_ai_metadata` | false | will enable adding ai metadata |
| `add_user_to_requests` | false | send the current command and PR URL in the OpenAI-compatible "user" request field, for provider-side attribution of requests (e.g. OpenRouter "external_user") |
| `reasoning_effort` | "medium" | "none", "minimal", "low", "medium", "high", "xhigh", "max" |
**extended thinking for Claude reasoning models**

| Key | Default | Description |
| --- | --- | --- |
| `enable_claude_extended_thinking` | false | Set to true to enable extended thinking feature |
| `extended_thinking_budget_tokens` | 2048 |  |
| `extended_thinking_max_output_tokens` | 4096 |  |
| `enable_claude_adaptive_thinking` | false | Adaptive thinking for Claude Opus 4.7/4.8 and Claude 5 models. When enabled, these models receive thinking={"type": "adaptive"} and an output_config effort. Do not add adaptive-only models to claude_extended_thinking_models_override; when both features are enabled, adaptive thinking takes precedence. |
| `claude_extended_thinking_models_override` | [] | Optional: override the built-in list of Claude models that receive the extended-thinking payload. When non-empty, this list fully replaces the built-in defaults (see CLAUDE_EXTENDED_THINKING_MODELS in pr_agent/algo/__init__.py). Leave empty to use the defaults. |
| `extract_issue_from_branch` | true | Extract issue number from PR source branch name (e.g. feature/1-auth-google -> issue #1). When true, branch-derived issue URLs are merged with tickets from the PR description for compliance. Set to false to restore description-only behaviour. Note: Branch-name extraction is GitHub-only for now; other providers planned for later. |
| `branch_issue_regex` | "" | Optional: custom regex with exactly one capturing group for the issue number (validated at runtime; falls back to default if missing). If empty, uses default pattern: first 1-6 digits at start of branch or after a slash, followed by hyphen or end (e.g. feature/1-test, 123-fix). GitHub only; other providers planned for later. |


## `[pr_reviewer]` — /review

**enable/disable features**

| Key | Default | Description |
| --- | --- | --- |
| `require_score_review` | false |  |
| `require_tests_review` | true |  |
| `require_estimate_effort_to_review` | true |  |
| `require_can_be_split_review` | false |  |
| `require_security_review` | true |  |
| `require_estimate_contribution_time_cost` | false |  |
| `require_todo_scan` | false |  |
| `require_ticket_analysis_review` | true |  |
| `require_risk_assessment` | false | ask the model for an overall risk level (low/medium/high) |
| `require_merge_recommendation` | false | ask the model for a merge recommendation (safe_to_merge/merge_with_caution/changes_required) |
| `require_priority_files` | false | ask the model which files a human should inspect first |
**general options**

| Key | Default | Description |
| --- | --- | --- |
| `publish_output_no_suggestions` | true | Set to "false" if you only need the reviewer's remarks (not labels, not "security audit", etc.) and want to avoid noisy "No major issues detected" comments. |
| `publish_error_details` | false | Publish a deterministic, sanitized failure reason in manual review comments. No AI call is used. |
| `persistent_comment` | true |  |
| `review_heading` | "PR Reviewer Guide" | Visible base heading for full and incremental review comments. Identity is tracked separately. |
| `persistent_finding_state` | true | Persist review finding state across complete review runs. |
| `inline_key_issues` | false |  |
| `extra_instructions` | "" |  |
| `num_max_findings` | 3 |  |
| `final_update_message` | true |  |
**review labels**

| Key | Default | Description |
| --- | --- | --- |
| `enable_review_labels_security` | true |  |
| `enable_review_labels_effort` | true |  |
**specific configurations for incremental review (/review -i)**

| Key | Default | Description |
| --- | --- | --- |
| `require_all_thresholds_for_incremental_review` | false |  |
| `minimal_commits_for_incremental_review` | 0 |  |
| `minimal_minutes_for_incremental_review` | 0 |  |
| `enable_intro_text` | true |  |
| `enable_help_text` | false | Determines whether to include help text in the PR review. |
| `enable_review_coverage_footer` | true |  |
| `enable_large_pr_chunking` | false | large-diff chunking (opt-in). When the token budget leaves files out of the review, split the diff into chunks, review each chunk, and merge the per-chunk results into one review. |
| `max_number_of_calls` | 3 | maximum number of chunk review calls, used only when enable_large_pr_chunking is true |


## `[pr_description]` — /describe

| Key | Default | Description |
| --- | --- | --- |
| `publish_labels` | false |  |
| `add_original_user_description` | true |  |
| `generate_ai_title` | false |  |
| `use_bullet_points` | true |  |
| `extra_instructions` | "" |  |
| `enable_pr_type` | true |  |
| `enable_pr_description` | true | adds a section with an AI-generated summary of the PR changes |
| `final_update_message` | true |  |
| `enable_help_text` | false |  |
| `enable_help_comment` | false |  |
| `enable_pr_diagram` | true | adds a section with a diagram of the PR changes |
| `pr_diagram_direction` | "adaptive" | 'adaptive', 'LR', 'TD'. 'adaptive' picks the direction from the shape of the diagram |
| `pr_diagram_direction_threshold` | 5 | with 'adaptive', a chain longer than this many nodes is drawn top-down |
**describe as comment**

| Key | Default | Description |
| --- | --- | --- |
| `publish_description_as_comment` | false |  |
| `publish_description_as_comment_persistent` | true |  |
**changes walkthrough section**

| Key | Default | Description |
| --- | --- | --- |
| `enable_semantic_files_types` | true |  |
| `collapsible_file_list` | "adaptive" | true, false, 'adaptive' |
| `collapsible_file_list_threshold` | 6 |  |
| `file_table_collapsible_open_by_default` | false |  |
**markers**

| Key | Default | Description |
| --- | --- | --- |
| `use_description_markers` | false |  |
| `enable_large_pr_handling` | true |  |
| `include_generated_by_header` | true |  |
| `max_ai_calls` | 4 |  |
| `async_ai_calls` | true |  |


## `[pr_questions]` — /ask

| Key | Default | Description |
| --- | --- | --- |
| `enable_help_text` | false |  |
| `use_conversation_history` | true |  |
| `ask_heading` | "Ask" | Set the plain-text heading for top-level /ask answers; provider-specific presentation is added automatically. |
| `resolve_threads` | false | Enable to let /ask_line resolve the review thread when the LLM judges the issue addressed. Note: also resolves threads started by human reviewers. |
| `extra_instructions` | "" |  |


## `[pr_code_suggestions]` — /improve

| Key | Default | Description |
| --- | --- | --- |
| `commitable_code_suggestions` | false |  |
| `dual_publishing_score_threshold` | -1 | -1 to disable, [0-10] to set the threshold (>=) for publishing a code suggestion both in a table and as committable |
| `focus_only_on_problems` | true |  |
| `extra_instructions` | "" |  |
| `suggestions_heading` | "PR Code Suggestions" | Visible base heading for summary-table /improve comments. Identity is tracked separately. |
| `enable_help_text` | false |  |
| `enable_chat_text` | false |  |
| `persistent_comment` | true |  |
| `max_history_len` | 4 |  |
| `publish_output_no_suggestions` | true |  |
| `enable_suggestions_coverage_footer` | true | show when failed analysis chunks make the suggestions incomplete |
**suggestions scoring**

| Key | Default | Description |
| --- | --- | --- |
| `suggestions_score_threshold` | 0 | [0-10]\| recommend not to set this value above 8, since above it may clip highly relevant suggestions |
| `new_score_mechanism` | true |  |
| `new_score_mechanism_th_high` | 9 |  |
| `new_score_mechanism_th_medium` | 7 |  |
**params for '/improve --extended' mode**

| Key | Default | Description |
| --- | --- | --- |
| `auto_extended_mode` | true |  |
| `num_code_suggestions_per_chunk` | 3 |  |
| `max_suggestions_per_file` | 0 | Maximum suggestions retained per file after all chunks are merged; 0 disables the cap. |
| `max_number_of_calls` | 3 |  |
| `parallel_calls` | true |  |
| `final_clip_factor` | 0.8 |  |
| `decouple_hunks` | false |  |
**self-review checkbox**

| Key | Default | Description |
| --- | --- | --- |
| `demand_code_suggestions_self_review` | false | add a checkbox for the author to self-review the code suggestions |
| `code_suggestions_self_review_text` | "**Author self-review**: I have reviewed the PR code suggestions, and addressed the relevant ones." |  |
| `approve_pr_on_self_review` | false | if true, the PR will be auto-approved after the author clicks on the self-review checkbox |
| `fold_suggestions_on_self_review` | true | if true, the code suggestions will be folded after the author clicks on the self-review checkbox |


## `[pr_custom_prompt]` — /custom_prompt

| Key | Default | Description |
| --- | --- | --- |
| `prompt` | """The code suggestions should focus only on the following: ...""" |  |
| `suggestions_score_threshold` | 0 |  |
| `num_code_suggestions_per_chunk` | 3 |  |
| `self_reflect_on_custom_suggestions` | true |  |
| `enable_help_text` | false |  |


## `[pr_add_docs]` — /add_docs

| Key | Default | Description |
| --- | --- | --- |
| `extra_instructions` | "" |  |
| `docs_style` | "Sphinx" | "Google Style with Args, Returns, Attributes...etc", "Numpy Style", "Sphinx Style", "PEP257", "reStructuredText" |
| `file` | "" | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | "" | in case there are several methods with the same name in the same file, you can specify the relevant class name |


## `[pr_update_changelog]` — /update_changelog

| Key | Default | Description |
| --- | --- | --- |
| `push_changelog_changes` | false |  |
| `extra_instructions` | "" |  |
| `add_pr_link` | true |  |
| `skip_ci_on_push` | true |  |


## `[pr_analyze]` — /analyze

| Key | Default | Description |
| --- | --- | --- |
| `enable_help_text` | true |  |


## `[pr_test]` — /test

| Key | Default | Description |
| --- | --- | --- |
| `extra_instructions` | "" |  |
| `testing_framework` | "" | specify the testing framework you want to use |
| `num_tests` | 3 | number of tests to generate. max 5. |
| `avoid_mocks` | true | if true, the generated tests will prefer to use real objects instead of mocks |
| `file` | "" | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | "" | in case there are several methods with the same name in the same file, you can specify the relevant class name |
| `enable_help_text` | false |  |


## `[pr_improve_component]` — /improve_component

| Key | Default | Description |
| --- | --- | --- |
| `num_code_suggestions` | 4 |  |
| `extra_instructions` | "" |  |
| `file` | "" | in case there are several components with the same name, you can specify the relevant file |
| `class_name` | "" | in case there are several methods with the same name in the same file, you can specify the relevant class name |


## `[pr_help]` — /help

| Key | Default | Description |
| --- | --- | --- |
| `force_local_db` | false |  |
| `num_retrieved_snippets` | 5 |  |


## `[pr_config]` — /config

_This section only documents commented-out examples; see the [TOML source](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) for details._

## `[pr_help_docs]`

| Key | Default | Description |
| --- | --- | --- |
| `repo_url` | "" | If not overwritten, will use the repo from where the context came from (issue or PR) |
| `repo_default_branch` | "main" |  |
| `docs_path` | "docs" |  |
| `exclude_root_readme` | false |  |
| `supported_doc_exts` | [".md", ".mdx", ".rst"] |  |
| `enable_help_text` | false |  |


## `[github]`

| Key | Default | Description |
| --- | --- | --- |
| `deployment_type` | "user" | The type of deployment to create. Valid values are 'app' or 'user'. |
| `ratelimit_retries` | 5 |  |
| `seconds_between_requests` | 0 | seconds between API requests; 0 = no pacing (1.59 behaviour) |
| `seconds_between_writes` | 0 | seconds between write calls; 0 = no pacing (1.59 behaviour) |
| `api_retries` | 0 | max retries per request with backoff; 0 = no retries (1.59 behaviour) |
| `polling_request_timeout` | 10 | total seconds for comment-history fallback; positive values capped at 60 |
| `base_url` | "https://api.github.com" |  |
| `publish_inline_comments_fallback_with_verification` | true |  |
| `try_fix_invalid_inline_comments` | true |  |
| `app_name` | "pr-agent" |  |
| `ignore_bot_pr` | true |  |
| `publish_as_check_run` | false | when true, publish review/description/improve output as GitHub Checks instead of PR comments |


## `[github_action_config]`

_This section only documents commented-out examples; see the [TOML source](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) for details._

## `[github_app]`

| Key | Default | Description |
| --- | --- | --- |
| `bot_user` | "github-actions[bot]" | these toggles allows running the github app from custom deployments |
| `override_deployment_type` | true |  |
**settings for "pull_request" event**

| Key | Default | Description |
| --- | --- | --- |
| `handle_pr_actions` | ["opened", "reopened", "ready_for_review"] |  |
| `feedback_on_draft_pr` | false |  |
| `review_states` | ["changes_requested"] | A submitted GitHub review can optionally trigger these commands. The empty default preserves current behavior. |
| `review_author_types` | ["User"] |  |
| `review_commands` | [] |  |
| `handle_push_trigger` | false | settings for "pull_request" event with "synchronize" action - used to detect and handle push triggers for new commits |
| `push_trigger_ignore_bot_commits` | true |  |
| `push_trigger_ignore_merge_commits` | true |  |
| `push_trigger_wait_for_initial_review` | true |  |
| `push_trigger_pending_tasks_backlog` | true |  |
| `push_trigger_pending_tasks_ttl` | 300 |  |
| `push_commands` | ["/describe", "/review"] |  |


## `[gitlab]`

| Key | Default | Description |
| --- | --- | --- |
| `url` | "https://gitlab.com" |  |
| `expand_submodule_diffs` | false |  |
| `feedback_on_draft_pr` | false |  |
| `publish_review_as_thread` | false | Post the /review summary as a resolvable thread (discussion) instead of a plain note. |
| `publish_improve_as_thread` | false | Post the /improve suggestions comment as a resolvable thread (discussion) instead of a plain note. |
| `publish_code_suggestions_as_review` | false | When pr_code_suggestions.commitable_code_suggestions is true, queue each suggestion as a GitLab draft note and publish them all together in one batch (like GitLab's own "start a review" flow) instead of posting each as its own live discussion - and its own notification - as soon as it's created. |
| `resolve_outdated_inline_threads` | false | Resolve the bot's own inline threads that a later push left on an outdated diff version. |
| `handle_push_trigger` | false |  |
| `push_commands` | ["/describe", "/review"] |  |
| `handle_reviewer_assignment` | false | Auto-trigger commands when the bot is assigned as a reviewer on an MR |
| `reviewer_commands` | ["/review"] |  |


## `[gitea]`

| Key | Default | Description |
| --- | --- | --- |
| `url` | "https://gitea.com" |  |
| `handle_push_trigger` | false |  |
| `push_commands` | ["/describe", "/review"] |  |


## `[bitbucket_app]`

| Key | Default | Description |
| --- | --- | --- |
| `avoid_full_files` | false |  |
| `request_timeout` | 30 | positive seconds for connection and response-read inactivity timeouts |


## `[local]`

_This section only documents commented-out examples; see the [TOML source](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) for details._

## `[gerrit]`

| Key | Default | Description |
| --- | --- | --- |
| `webhook_username` | "" | both are required: the gerrit webhook endpoint rejects every call until they are set |
| `webhook_password` | "" |  |


## `[bitbucket_server]`

| Key | Default | Description |
| --- | --- | --- |
| `url` | "" | URL to the BitBucket Server instance |


## `[jira]`

| Key | Default | Description |
| --- | --- | --- |
| `jira_requirements_field` | "" | Custom field id holding acceptance criteria / requirements, mapped to the ticket "requirements" section. Instance-specific (e.g. "customfield_10127"); empty disables it. |


## `[litellm]`

| Key | Default | Description |
| --- | --- | --- |
| `enable_callbacks` | false |  |
| `success_callback` | [] |  |
| `failure_callback` | [] |  |
| `service_callback` | [] |  |
| `turn_off_message_logging` | false | set true to keep prompt/response content (the whole PR diff) out of callback payloads; strongly recommended with the "otel" callback |
| `custom_llm_provider` | "" | Optional: forward a fixed custom_llm_provider to LiteLLM, so a raw hosted model id (e.g. "claude-sonnet-4-5") reaches the provider unchanged instead of being rewritten by LiteLLM's prefix inference. Empty = let LiteLLM infer the provider from the model name. |
| `force_streaming_custom_llm_provider` | "" | Force streaming when the request matches this provider AND its api_base contains one of the substrings below. Some OpenAI-compatible endpoints return a response that LiteLLM cannot normalize in non-streaming mode. Both must be set for the workaround to apply. |
| `force_streaming_api_base_substrings` | [] |  |
| `callback_timeout_seconds` | 30 | max seconds to wait for pending litellm callbacks to flush before exiting |
| `cache_control_injection_points` | [] | Optional: enable Anthropic prompt caching via LiteLLM, e.g. [{location = "message", role = "system"}] (https://docs.litellm.ai/docs/tutorials/prompt_caching) |


## `[openrouter]`

| Key | Default | Description |
| --- | --- | --- |
| `provider_only` | [] | restrict routing to these upstream providers only, a hard allowlist (e.g. ["z-ai"]); empty = OpenRouter default routing |
| `provider_order` | [] | preferred provider order; ignored when provider_only is set; empty = unset |
| `allow_fallbacks` | true | when provider_order is set, allow routing beyond the listed providers |
| `reasoning_effort` | "" | Invalid reasoning_effort values are warned about and treated as unset. Empty inherits config.reasoning_effort for models in SUPPORT_REASONING_EFFORT_MODELS. Valid values: "none", "minimal", "low", "medium", "high", "xhigh", "max". OpenRouter normalizes "max" to "xhigh" for LiteLLM/OpenRouter compatibility. Model-specific support varies; mandatory reasoning models reject "none". |
| `reasoning_max_tokens` | 0 | A positive value overrides global effort and non-none OpenRouter-specific efforts. Explicit openrouter.reasoning_effort = "none" keeps reasoning disabled, except on Grok 4.5/4.6: there "none" is clamped to the lowest supported effort first, so a positive budget wins over it. Some providers require max_tokens to be greater than the reasoning budget. |
| `max_tokens` | 0 | hard cap on completion tokens for the request; 0 = unset |


## `[model_routing]` — send a small pull request to a cheaper primary model (disabled by default)

| Key | Default | Description |
| --- | --- | --- |
| `enable` | false | Rules are checked in order; the first one whose limits the pull request fits selects the primary model for the call, and config.fallback_models still apply after it. A pull request that fits no rule uses config.model. Size is measured after the [ignore] rules by diff hunks (max_hunks) and changed files (max_files), which every git provider reports and which do not depend on any model's tokenizer. Only calls that ask for the regular model are routed (/review, /improve, /generate_labels, /add_docs); tools that use model_weak are left alone. With Azure (openai.deployment_id set) a rule also needs its own deployment_id, or it is skipped. |
| `rules` | [] | e.g. [{ max_hunks = 3, model = "gpt-5.6-luna" }, { max_hunks = 15, max_files = 6, model = "gpt-5.6-terra" }] |


## `[otel]`

| Key | Default | Description |
| --- | --- | --- |
| `is_enabled` | false | disabled by default; set to true to enable telemetry |
| `exporter_type` | "console" | "console", "otlp", "prometheus", or "none" |
| `service_name` | "pr-agent" |  |
| `environment` | "development" | "development", "staging", "production", etc. |
| `otlp_timeout` | 3 | seconds; hard deadline per OTLP export call (incl. retries). Bounds CLI-exit/request stalls when the collector is unreachable; batches slower than this are dropped. |
| `otlp_protocol` | "http" | "http" (default; exporter ships with pr-agent) or "grpc" (requires the otel-grpc extra: pip install pr-agent[otel-grpc]) |
| `prometheus_multiproc_dir` | "/tmp/pr-agent-prometheus" | shared dir for the prometheus exporter's per-worker state files; required when exporter_type = "prometheus" (multiprocess gunicorn deployments); created 0700 and must stay owned by the run user (symlink or foreign-owner paths are refused) |
| `include_pr_url` | false | set to true to attach PR URLs to spans (may expose private repo names) |
| `include_error_details` | false | set to true to attach exception messages and rejected-command text to spans (may expose PR URLs, repo names, or other request content) |


## `[pr_similar_issue]`

| Key | Default | Description |
| --- | --- | --- |
| `skip_comments` | false |  |
| `force_update_dataset` | false |  |
| `max_issues_to_scan` | 500 |  |
| `vectordb` | "lancedb" | options: "pinecone", "lancedb", "qdrant" |


## `[pr_find_similar_component]`

| Key | Default | Description |
| --- | --- | --- |
| `class_name` | "" |  |
| `file` | "" |  |
| `search_from_org` | false |  |
| `allow_fallback_less_words` | true |  |
| `number_of_keywords` | 5 |  |
| `number_of_results` | 5 |  |


## `[pinecone]`

| Key | Default | Description |
| --- | --- | --- |
| `cloud` | "aws" | Serverless index deployment for the modern SDK. `cloud` is one of "aws", "gcp" or "azure"; pick a `region` offered by that cloud. |
| `region` | "us-east-1" |  |


## `[lancedb]`

| Key | Default | Description |
| --- | --- | --- |
| `uri` | "./lancedb" |  |


## `[qdrant]`

_This section only documents commented-out examples; see the [TOML source](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) for details._

## `[skills]`

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | false | Agent skills (SKILL.md) support: discovers SKILL.md files from the configured filesystem paths and injects their content into review/improve/describe and top-level /ask prompts. Sibling *.md files in the skill directory tree (e.g. references/guide.md) are inlined alongside SKILL.md. PR-Agent supports text-only skills: scripts/ and assets/ subdirectories are skipped because PR-Agent uses a single-shot model call (no tool-use loop) and cannot execute scripts or load binary assets on demand. Skills that depend on script execution will not work here. See https://github.com/The-PR-Agent/pr-agent/issues/2384 |
| `paths` | [] | directories to scan recursively for "*/SKILL.md"; supports ~ and $VAR |
| `max_skills_tokens` | 8000 | token budget for the combined skills_context block |


## `[best_practices]`

| Key | Default | Description |
| --- | --- | --- |
| `content` | "" |  |
| `organization_name` | "" |  |
| `max_lines_allowed` | 800 |  |
| `enable_global_best_practices` | false |  |


## `[auto_best_practices]`

| Key | Default | Description |
| --- | --- | --- |
| `enable_auto_best_practices` | true | public - general flag to disable all auto best practices usage |
| `utilize_auto_best_practices` | true | public - disable usage of auto best practices in the 'improve' tool |
| `extra_instructions` | "" | public - extra instructions to the auto best practices generation prompt |
| `content` | "" |  |
| `max_patterns` | 5 | max number of patterns to be detected |


## `[artifacts]`

| Key | Default | Description |
| --- | --- | --- |
| `enable` | false | Enable artifact injection into tool prompts (off by default; auto-enabled when artifact_path input is set) |
| `artifact_path` | "" | File path to the artifact (relative to GITHUB_WORKSPACE, or absolute) |
| `artifact_instructions` | "" | Custom instructions appended after the artifact content (leave empty for a sensible default) |
| `artifact_label` | "" | Label shown to the AI — defaults to the filename when empty. |
| `target_tools` | ["pr_reviewer", "pr_description", "pr_code_suggestions"] | Which tools receive artifact context. |
| `max_artifact_size` | 50000 | Max artifact size in characters (content is truncated if exceeded) |


## `[asana]`

| Key | Default | Description |
| --- | --- | --- |
| `request_timeout` | 10 | seconds allowed for each Asana task API request |


## `[azure_devops]`

| Key | Default | Description |
| --- | --- | --- |
| `default_comment_status` | "closed" |  |


## `[azure_devops_server]`

| Key | Default | Description |
| --- | --- | --- |
| `agent_identity` | "" | empty: discover the identity from earlier agent comments on the PR |


## `[push_outputs]` — push tool outputs to external sinks without calling git-provider APIs (disabled by default)

| Key | Default | Description |
| --- | --- | --- |
| `enable` | false |  |
| `channels` | [] | any of: "stdout", "file", "webhook", "slack". Nothing is emitted until a channel is listed here |
| `file_path` | "pr-agent-outputs/reviews.jsonl" | used by the "file" channel |
| `webhook_url` | "" | used by the "webhook" channel: generic JSON POST target. Must be an absolute https:// URL |
| `slack_webhook_url` | "" | used by the "slack" channel: a Slack Incoming Webhook URL. Must be an absolute https:// URL |
