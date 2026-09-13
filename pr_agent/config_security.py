"""Shared configuration boundaries for repository-provided settings."""

# Sections that touch host-level capabilities cannot be fully configured from
# a repository's settings file. The same allowlist is used by repo settings
# application and CLI argument validation so the two entry points cannot drift.
# For each section listed here, only the keys in its allowlist may be set from a
# repository; every other key is dropped with a warning.
#
# skills: `enabled` and `max_skills_tokens` are safe per-repo preferences (a repo can opt in to, or
# size, the host's admin-curated skill library). `paths` is NOT overridable: it points at the
# PR-Agent host's filesystem, so letting a repo set it would allow a malicious repo to read
# sensitive host files (e.g. ~/.ssh/*) into the LLM prompt. `paths` therefore stays host-only.
#
# push_outputs: routes review data to operator-controlled sinks (webhook/slack/file). Letting a
# repo set any of these would let a malicious repo exfiltrate review data to an arbitrary host,
# reach internal endpoints (SSRF), or append to arbitrary host files. The whole section is
# therefore host-only (empty allowlist -> every key dropped).
#
# prompt_fragments: contains Jinja source rendered by the host before it is inserted into tool
# prompts. Keep the whole section host-only so repository settings and comment arguments cannot
# supply executable template expressions.
REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION = {
    "skills": frozenset({"enabled", "max_skills_tokens"}),
    "push_outputs": frozenset(),
    "prompt_fragments": frozenset(),
}

# Individual settings in otherwise repository-configurable sections may also be
# host-only. publish_error_details controls what service-side failure state is
# disclosed in a PR comment, so the PR author must not be able to enable it.
REPO_HOST_ONLY_KEYS_BY_SECTION = {
    "pr_reviewer": frozenset({"publish_error_details"}),
}

# Keys a per-directory `.pr_agent.toml` can never override, even when their section is
# otherwise open (None) in REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS. Nested files live in
# the working repository where any contributor can edit them, so keys that perform
# host-side writes (label mutation, resolving human review threads) or consume unbounded
# external resources (forcing a full issue-index refresh, scanning arbitrary issue counts,
# or repointing the vector backend) stay root-config- or host-controlled.
# Similarly, budget/call-count controls (max_number_of_calls, max_ai_calls, parallel_calls,
# enable_large_pr_chunking, enable_large_pr_handling, async_ai_calls) are restricted so
# a nested file cannot multiply AI calls independently of the host-trusted defaults.
PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION = {
    "pr_reviewer": frozenset({"enable_large_pr_chunking", "max_number_of_calls"}),
    "pr_description": frozenset({
        "publish_labels", "enable_large_pr_handling", "max_ai_calls", "async_ai_calls",
    }),
    "pr_questions": frozenset({"resolve_threads"}),
    "pr_code_suggestions": frozenset({"max_number_of_calls", "parallel_calls"}),
    "pr_similar_issue": frozenset({"force_update_dataset", "max_issues_to_scan", "vectordb"}),
}

# Sections a *per-directory* `.pr_agent.toml` may override at all. Nested config
# files live in the working repository where any contributor can edit them, so this
# layer is deliberately narrower than the root/global repo settings: tool
# instructions, suggestion limits, ignore lists and model routing only. A frozenset
# value restricts the section to those keys; None allows every key in the section.
# Secrets, identity and deployment-critical settings (provider tokens, git_provider,
# push_outputs, skills, prompt_fragments, ...) can only be influenced from the root
# config or host environment, never from a nested file.
#
# Tool sections that can trigger bot-side writes (commits, changelog pushes) or
# read/connect from arbitrary URLs are likewise restricted to drop-only keys:
# `pr_update_changelog` cannot be given `push_changelog_changes` (a nested config
# must not cause the bot to commit), and `pr_help_docs` cannot be given `repo_url`
# (that field can be resolved into a token-embedded clone URL, so a nested file
# must not point it anywhere).
#
# The `ignore` section is open to `glob` only: fnmatch translates glob patterns
# into bounded regexes, whereas `ignore.regex` accepts arbitrary expressions that
# filter_ignored() compiles and matches against every changed filename on every
# review. A catastrophic-backtracking pattern committed in a nested file could
# stall a worker, so nested files keep the bounded glob form only.
REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS = {
    "config": frozenset({
        "model", "fallback_models", "model_weak", "model_reasoning",
        "custom_model_max_tokens", "max_model_tokens", "max_output_tokens",
        "model_token_count_estimate_factor", "temperature", "response_language",
        "repo_context_files", "repo_context_from_default_branch", "repo_context_max_lines",
    }),
    "ignore": frozenset({"glob"}),
    "pr_reviewer": None,
    "pr_description": None,
    "pr_questions": None,
    "pr_code_suggestions": None,
    "pr_custom_prompt": None,
    "pr_add_docs": None,
    "pr_update_changelog": frozenset({"extra_instructions", "add_pr_link"}),
    "pr_analyze": None,
    "pr_test": None,
    "pr_improve_component": None,
    "pr_help": None,
    "pr_help_docs": frozenset({"docs_path", "exclude_root_readme", "supported_doc_exts", "enable_help_text"}),
    "pr_similar_issue": None,
    "pr_find_similar_component": None,
}
