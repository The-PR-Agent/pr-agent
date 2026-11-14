"""Ledger of the configuration keys a PR comment can override.

The host-only lists in pr_agent/config_security.py only say what is blocked, so a new
key in pr_agent/settings/configuration.toml is open to `/command --section.key=value`
unless someone remembers to block it. This ledger records every key the validator
still accepts, so that a new key cannot stay open without a visible choice in the diff.

Adding a key to configuration.toml fails this test until the key is either made
host-only in pr_agent/config_security.py or added to the ledger below. The ledger
starts as a snapshot of existing behaviour, not a statement that each key should stay
open. Nothing at runtime reads it.
"""

import tomllib
from pathlib import Path

from pr_agent.algo.cli_args import CliArgs

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"

COMMENT_OVERRIDABLE_KEYS = {
    "artifacts": frozenset({
        "artifact_instructions", "artifact_label", "artifact_path", "enable", "max_artifact_size",
        "target_commands", "truncate_from",
    }),
    "asana": frozenset({
        "request_timeout",
    }),
    "azure_devops": frozenset({
        "default_comment_status",
    }),
    "bitbucket": frozenset({
        "identity_request_timeout",
    }),
    "bitbucket_app": frozenset({
        "avoid_full_files", "handle_push_trigger", "push_commands", "request_timeout",
    }),
    "bitbucket_server": frozenset({
        "handle_push_trigger", "push_commands",
    }),
    "config": frozenset({
        "add_user_to_requests", "additional_reasoning_effort_models", "allow_dynamic_context",
        "bot_user_indicators", "claude_adaptive_thinking_models_override",
        "claude_extended_thinking_models_override", "cli_mode", "custom_model_max_tokens",
        "custom_reasoning_model", "disable_auto_feedback", "duplicate_prompt_examples", "enable_ai_metadata",
        "enable_claude_adaptive_thinking", "enable_claude_extended_thinking", "enable_per_directory_settings",
        "extended_thinking_budget_tokens", "extended_thinking_max_output_tokens", "extract_issue_from_branch",
        "ignore_language_framework", "ignore_pr_authors", "ignore_pr_labels", "ignore_pr_source_branches",
        "ignore_pr_target_branches", "ignore_pr_title", "ignore_repositories", "image_input_token_allowance",
        "is_auto_command", "large_patch_policy", "log_level", "max_commits_tokens", "max_description_tokens",
        "max_extra_lines_before_dynamic_context", "max_model_tokens", "max_output_tokens",
        "max_webhook_request_body_bytes", "model", "model_token_count_estimate_factor",
        "no_temperature_models", "output_run_cost", "output_run_details", "patch_extension_skip_types",
        "patch_extra_lines_after", "patch_extra_lines_before", "per_directory_settings_max_files",
        "per_directory_settings_max_tree_pages", "persistent_inline_comments", "progress_gif_url",
        "progress_gif_width", "propagate_command_errors", "publish_output", "publish_output_progress",
        "reaction_on_failure", "reaction_on_start", "reaction_on_success", "reasoning_effort",
        "repo_context_from_default_branch", "repo_context_max_lines", "response_language", "restricted_mode",
        "retry_same_model_on_length", "retry_same_model_on_timeout", "seed", "temperature",
        "use_extra_bad_extensions", "use_global_settings_file", "use_repo_settings_file", "verbosity_level",
    }),
    "gitea": frozenset({
        "handle_push_trigger", "push_commands",
    }),
    "github": frozenset({
        "ignore_bot_pr", "polling_request_timeout", "publish_as_check_run", "try_fix_invalid_inline_comments",
    }),
    "github_app": frozenset({
        "feedback_on_draft_pr", "handle_pr_actions", "handle_push_trigger", "push_commands",
        "push_trigger_ignore_bot_commits", "push_trigger_ignore_merge_commits",
        "push_trigger_pending_tasks_backlog", "push_trigger_pending_tasks_ttl", "review_author_types",
        "review_commands", "review_states", "webhook_delivery_deduplication",
    }),
    "gitlab": frozenset({
        "auto_resolve_fixed_inline_threads", "expand_submodule_diffs", "feedback_on_draft_pr",
        "handle_push_trigger", "handle_reviewer_assignment", "ignore_user_ids",
        "publish_code_suggestions_as_review", "publish_improve_as_thread", "publish_review_as_thread",
        "push_commands", "reply_to_trigger_comment", "resolve_outdated_inline_threads", "reviewer_commands",
    }),
    "jira": frozenset({
        "jira_requirements_field",
    }),
    "linear": frozenset({
        "request_timeout",
    }),
    "litellm": frozenset({
        "add_session_id_to_requests", "base_models", "cache_control_injection_points",
        "callback_timeout_seconds", "custom_llm_provider", "enable_callbacks", "failure_callback",
        "force_streaming_api_base_substrings", "force_streaming_custom_llm_provider", "service_callback",
        "success_callback", "turn_off_message_logging",
    }),
    "model_routing": frozenset({
        "enable", "rules",
    }),
    "mosaico": frozenset({
        "context_history_max_tasks", "health_timeout_seconds", "routing_scan_max_chars",
        "task_store_max_tasks_per_owner",
    }),
    "openrouter": frozenset({
        "allow_fallbacks", "max_tokens", "provider_only", "provider_order", "reasoning_effort",
        "reasoning_max_tokens",
    }),
    "otel": frozenset({
        "environment", "exporter_type", "include_error_details", "is_enabled", "otlp_protocol",
        "otlp_timeout", "prometheus_multiproc_dir", "service_name",
    }),
    "pinecone": frozenset({
        "cloud", "region",
    }),
    "pr_add_docs": frozenset({
        "docs_style", "extra_instructions", "use_repo_context",
    }),
    "pr_code_suggestions": frozenset({
        "code_suggestions_self_review_text", "committable_code_suggestions", "decouple_hunks",
        "demand_code_suggestions_self_review", "dual_publishing_score_threshold", "enable_chat_text",
        "enable_help_text", "enable_suggestions_coverage_footer", "extra_instructions",
        "focus_only_on_problems", "max_discussion_context_chars", "max_history_len",
        "max_suggestions_per_file", "new_score_mechanism", "new_score_mechanism_th_high",
        "new_score_mechanism_th_medium", "num_code_suggestions_per_chunk", "persistent_comment",
        "publish_failure_comment", "publish_output_no_suggestions", "score_on_reflection_failure",
        "suggestions_heading", "suggestions_score_threshold",
    }),
    "pr_description": frozenset({
        "add_original_user_description", "async_ai_calls", "collapsible_file_list",
        "collapsible_file_list_threshold", "enable_help_comment", "enable_help_text",
        "enable_large_pr_handling", "enable_pr_description", "enable_pr_diagram", "enable_pr_type",
        "enable_semantic_files_types", "extra_instructions", "file_table_collapsible_open_by_default",
        "final_update_message", "generate_ai_title", "include_generated_by_header", "max_ai_calls",
        "pr_diagram_direction", "pr_diagram_direction_threshold", "publish_description_as_comment",
        "publish_description_as_comment_persistent", "publish_labels", "use_description_markers",
    }),
    "pr_help_docs": frozenset({
        "docs_path", "enable_help_text", "exclude_root_readme", "repo_default_branch", "supported_doc_exts",
    }),
    "pr_questions": frozenset({
        "ask_heading", "enable_help_text", "extra_instructions", "use_conversation_history",
    }),
    "pr_reviewer": frozenset({
        "enable_help_text", "enable_intro_text", "enable_large_pr_chunking", "enable_review_coverage_footer",
        "enable_review_labels_effort", "enable_review_labels_security", "extra_instructions",
        "final_update_message", "inline_key_issues", "max_previous_findings_chars", "min_finding_confidence",
        "minimal_commits_for_incremental_review", "minimal_minutes_for_incremental_review",
        "num_max_findings", "persistent_comment", "persistent_finding_state", "publish_output_no_suggestions",
        "publish_review_failure_comment", "require_all_thresholds_for_incremental_review",
        "require_can_be_split_review", "require_estimate_contribution_time_cost",
        "require_estimate_effort_to_review", "require_failure_modes", "require_merge_recommendation",
        "require_priority_files", "require_risk_assessment", "require_score_review",
        "require_security_review", "require_tests_review", "require_ticket_analysis_review",
        "require_todo_scan", "review_heading", "show_previous_findings_summary",
    }),
    "pr_similar_issue": frozenset({
        "max_issues_to_scan", "skip_comments", "vectordb",
    }),
    "pr_update_changelog": frozenset({
        "add_pr_link", "extra_instructions", "skip_ci_on_push",
    }),
    "skills": frozenset({
        "enabled", "max_skills_tokens",
    }),
}


def _keys_accepted_from_comments() -> set[tuple[str, str]]:
    with open(CONFIG_TOML, "rb") as f:
        settings = tomllib.load(f)

    return {
        (section, key)
        for section, entries in settings.items()
        if not section.startswith("_")
        for key in entries
        if CliArgs.validate_user_args([f"--{section}.{key}=1"])[0]
    }


def _ledger() -> set[tuple[str, str]]:
    return {(section, key) for section, keys in COMMENT_OVERRIDABLE_KEYS.items() for key in keys}


def test_every_key_accepted_from_comments_is_in_the_ledger():
    unclassified = _keys_accepted_from_comments() - _ledger()

    assert not unclassified, (
        f"Keys a PR comment can override but the ledger does not list: {sorted(unclassified)}. "
        "Make each one host-only in pr_agent/config_security.py, or add it to "
        "COMMENT_OVERRIDABLE_KEYS if a commenter should be able to set it."
    )


def test_ledger_lists_only_keys_accepted_from_comments():
    stale = _ledger() - _keys_accepted_from_comments()

    assert not stale, (
        f"Keys in the ledger that a PR comment can no longer override: {sorted(stale)}. "
        "Remove them from COMMENT_OVERRIDABLE_KEYS."
    )
