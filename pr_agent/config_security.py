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

_CONNECTION_KEY_SUFFIXES = ("url", "base_url", "endpoint", "org", "key", "token", "secret")
_REPO_OVERRIDABLE_SUFFIX_KEYS_BY_SECTION = {
    "config": frozenset({"progress_gif_url"}),
    "gitea": frozenset({"web_url"}),
}
_REPO_OVERRIDABLE_SUFFIX_SECTIONS = frozenset({"language_extension_map_org"})


# Keep individual settings in otherwise repository-configurable sections host-only.
# Protect generic provider connection locations and credentials in every section,
# using the exceptions above only for documented repository preferences.
REPO_HOST_ONLY_KEYS_BY_SECTION = {
    "aws": frozenset({"aws_access_key_id", "aws_region_name"}),
    # Keep api_base, api_type and api_version host-controlled, matching the comment-argument filter.
    "azure_ad": frozenset({"api_base"}),
    "azure_devops": frozenset({"pat"}),
    "databricks": frozenset({"api_base"}),
    "gerrit": frozenset({"webhook_password", "webhook_username"}),
    "huggingface": frozenset({"api_base"}),
    "moonshot": frozenset({"api_base"}),
    "ollama": frozenset({"api_base"}),
    "openai": frozenset({"api_base", "api_type", "api_version"}),
    "openrouter": frozenset({"api_base"}),
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
    # description_issue_regex is compiled and run with finditer over the whole
    # pull-request description, which is attacker-supplied and not length-capped. A pattern
    # whose matching is ambiguous backtracks exponentially, so a caller who can choose it can
    # burn a worker for an unbounded time on a short body. The setting is only meaningful as
    # an operator choice, and this is what makes it one: without it, [config] is otherwise
    # repo-configurable, so a reviewed repo's .pr_agent.toml or a comment argument could
    # supply the pattern. The operator still sets it through host configuration.
    # extra_config_url is host-only: the next apply_repo_settings() call fetches it over
    # HTTP(S) (attaching PR_AGENT_EXTRA_CONFIG_AUTH_HEADER) and merges *every* section of the
    # response into runtime settings without host-key filtering. A malicious reviewed repo
    # would otherwise repoint that fetch to an arbitrary internal URL (SSRF), exfiltrate the
    # auth header, and override secrets/model routing/output sinks wholesale. CLI arguments
    # for it are already blocked; the repo-settings entry point now matches.
    "config": frozenset({
        "extra_config_url",
        "description_issue_regex",
        "repo_context_max_sibling_files",
        "repo_context_sibling_repos",
    }),
}


def is_repo_host_only_key(section: str, key: str) -> bool:
    """Return whether a repository must not override a setting."""
    section = section.lower()
    key = key.lower()
    if key in REPO_HOST_ONLY_KEYS_BY_SECTION.get(section, frozenset()):
        return True
    return (
        section not in _REPO_OVERRIDABLE_SUFFIX_SECTIONS
        and key not in _REPO_OVERRIDABLE_SUFFIX_KEYS_BY_SECTION.get(section, frozenset())
        and key.endswith(_CONNECTION_KEY_SUFFIXES)
    )


# Keys that repositories may still configure from their own default-branch settings but that
# comment/CLI *arguments* must never override. repo_context_files selects which repository and
# sibling files are fetched and rendered as the model's instruction context, so an untrusted
# commenter must not be able to point the bot at arbitrary sibling repo content. Values curated
# by the repo's maintainers in .pr_agent.toml stay accepted (apply_repo_settings does not consult
# this map); only CliArgs.validate_user_args enforces it, so repo settings and comment args do
# not drift.
# github_action_config.fail_on_tool_errors decides whether a tool's recorded failure fails the
# GitHub Action. The runner reads it after the command has applied its arguments, so a comment
# such as `/review --github_action_config.fail_on_tool_errors=false` could turn a failed review
# into a green workflow; the workflow's operator sets it instead.
CLI_HOST_ONLY_KEYS_BY_SECTION = {
    "config": frozenset({"repo_context_files"}),
    "github_action_config": frozenset({"fail_on_tool_errors"}),
}

# Keys a per-directory `.pr_agent.toml` can never override, even when their section is
# otherwise open (None) in REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS. Nested files live in
# the working repository where any contributor can edit them, so keys that perform
# host-side writes (label mutation, resolving human review threads, publishing inline
# review findings, replacing pull-request labels with reviewer effort/security labels) or
# consume unbounded external resources (forcing a full issue-index refresh, scanning
# arbitrary issue counts, or repointing the vector backend) stay root-config- or host-
# controlled. Likewise, keys that rewrite pull-request metadata (AI title generation,
# comment-only description publication) stay root-controlled so a nested file cannot
# bypass the operator's choice of which PR fields the bot edits. Ticket extraction
# (require_ticket_analysis_review) stays root-controlled too: enabling it from a nested
# file would make the bot run authenticated Jira lookups the root config already disabled.
# Self-review workflow controls (demand_code_suggestions_self_review and
# approve_pr_on_self_review) stay root-controlled as well: a nested file must not be able
# to make /improve demand a checklist and then auto-approve the pull request when the
# author ticks it. Thread-history collection (pr_questions.use_conversation_history) is
# also root-controlled so a nested file cannot re-enable sending private review-thread
# discussion bodies to the model after the operator opted out.
# Similarly, budget/call-count controls (max_number_of_calls, max_ai_calls, parallel_calls,
# enable_large_pr_chunking, enable_large_pr_handling, async_ai_calls) are restricted so
# a nested file cannot multiply AI calls independently of the host-trusted defaults.
PER_DIRECTORY_HOST_ONLY_KEYS_BY_SECTION = {
    "pr_reviewer": frozenset({
        "enable_large_pr_chunking", "max_number_of_calls",
        "inline_key_issues", "enable_review_labels_security",
        "enable_review_labels_effort", "require_estimate_effort_to_review",
        "require_security_review", "require_ticket_analysis_review", "max_previous_findings_chars",
    }),
    "pr_description": frozenset({
        "publish_labels", "enable_large_pr_handling", "max_ai_calls", "async_ai_calls",
        "generate_ai_title", "publish_description_as_comment",
        "publish_description_as_comment_persistent",
    }),
    "pr_questions": frozenset({"resolve_threads", "use_conversation_history"}),
    "pr_code_suggestions": frozenset({
        "committable_code_suggestions",
        # The deprecated pre-1.0 spelling stays host-only for the same reason: a nested file
        # must not be able to flip /improve to inline publishing through the alias that
        # get_committable_code_suggestions() still accepts. This layer works by subtraction
        # (the whole section is overridable in REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS), and
        # an unrecognized key survives the merge, so leaving the alias out of this set would
        # hand a nested file the very control the canonical name is denied.
        "commitable_code_suggestions",
        "max_number_of_calls", "parallel_calls", "max_discussion_context_chars",
        "approve_pr_on_self_review", "demand_code_suggestions_self_review",
    }),
    "pr_similar_issue": frozenset({"force_update_dataset", "max_issues_to_scan", "vectordb", "skip_comments"}),
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
# read/connect from arbitrary URLs or paths are likewise restricted to drop-only keys:
# `pr_update_changelog` cannot be given `push_changelog_changes` (a nested config
# must not cause the bot to commit), and `pr_help_docs` cannot be given `repo_url`,
# `docs_path`, or `supported_doc_exts` (`repo_url` can be resolved into a token-embedded
# clone URL, while `docs_path` plus `supported_doc_exts` can point collection at any
# repository path or extension, so a nested file could read arbitrary source files into
# the model prompt).
#
# The `ignore` section is open to `glob` only: fnmatch translates glob patterns
# into bounded regexes, whereas `ignore.regex` accepts arbitrary expressions that
# filter_ignored() compiles and matches against every changed filename on every
# review. A catastrophic-backtracking pattern committed in a nested file could
# stall a worker, so nested files keep the bounded glob form only.
#
# The `config` section lists model-routing and output knobs but deliberately
# excludes the repo-context builders: `repo_context_files` fetches every listed
# file in full and `repo_context_max_lines` sizes the trimmed output, so without
# a host-trusted upper bound a nested file could balloon repository calls and
# token budget whenever a tool builds context for a directory it crosses. Those
# knobs stay root-/host-controlled. `fallback_models` is excluded too: the retry
# helper treats each entry as one routing attempt per failing model, so an
# arbitrarily long nested list could multiply AI calls; fallback routing stays
# root-/host-controlled. The token-budget keys (`max_model_tokens`,
# `custom_model_max_tokens`, `max_output_tokens`) are excluded as well because
# they directly size request context and completion limits, so without a trusted
# ceiling a nested file could inflate the size and cost of every model request.
REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS = {
    "config": frozenset({
        "model", "model_weak", "model_reasoning",
        "temperature", "response_language",
        "repo_context_from_default_branch",
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
    "pr_help_docs": frozenset({"exclude_root_readme", "enable_help_text"}),
    "pr_similar_issue": None,
    "pr_find_similar_component": None,
}
