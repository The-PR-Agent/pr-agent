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

# Sections a *per-directory* `.pr_agent.toml` may override at all. Nested config
# files live in the working repository where any contributor can edit them, so this
# layer is deliberately narrower than the root/global repo settings: tool
# instructions, suggestion limits, ignore lists and model routing only. A frozenset
# value restricts the section to those keys; None allows every key in the section.
# Secrets, identity and deployment-critical settings (provider tokens, git_provider,
# push_outputs, skills, prompt_fragments, ...) can only be influenced from the root
# config or host environment, never from a nested file.
REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS = {
    "config": frozenset({
        "model", "fallback_models", "model_weak", "model_reasoning",
        "custom_model_max_tokens", "max_model_tokens", "max_output_tokens",
        "model_token_count_estimate_factor", "temperature", "response_language",
        "repo_context_files", "repo_context_from_default_branch", "repo_context_max_lines",
    }),
    "ignore": None,
    "pr_reviewer": None,
    "pr_description": None,
    "pr_questions": None,
    "pr_code_suggestions": None,
    "pr_custom_prompt": None,
    "pr_add_docs": None,
    "pr_update_changelog": None,
    "pr_analyze": None,
    "pr_test": None,
    "pr_improve_component": None,
    "pr_help": None,
    "pr_help_docs": None,
    "pr_similar_issue": None,
    "pr_find_similar_component": None,
}
