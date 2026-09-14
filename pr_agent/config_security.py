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
    # repo_context_sibling_repos lists the sibling repositories whose files a consuming repo
    # (or a comment command) may select into model context. A repo's .pr_agent.toml alone must
    # not be able to name an arbitrary same-owner private sibling: the actor check bounds who
    # triggers the read, not who chose the target or where the output lands, so a sibling
    # collaborator running /review on a public repo would otherwise print the sibling's private
    # content into that public thread. The list stays host-only (default empty).
    # repo_context_max_sibling_files bounds sibling-repository fetches per repo-context build.
    # Letting a repo's .pr_agent.toml or a comment command raise it would defeat the safety
    # bound and let a commenter force unbounded cross-repository API calls; it stays host-only.
    "config": frozenset({"repo_context_max_sibling_files", "repo_context_sibling_repos"}),
}

# Keys that repositories may still configure from their own default-branch settings but that
# comment/CLI *arguments* must never override. repo_context_files selects which repository and
# sibling files are fetched and rendered as the model's instruction context, so an untrusted
# commenter must not be able to point the bot at arbitrary sibling repo content. Values curated
# by the repo's maintainers in .pr_agent.toml stay accepted (apply_repo_settings does not consult
# this map); only CliArgs.validate_user_args enforces it, so repo settings and comment args do
# not drift.
CLI_HOST_ONLY_KEYS_BY_SECTION = {
    "config": frozenset({"repo_context_files"}),
}
