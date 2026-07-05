"""Warn-only validation of settings overrides and of the loaded configuration.

Dynaconf accepts any section/key, so a typo in a repo's .pr_agent.toml (or an
extra config file) is silently ignored — a recurring source of "why isn't my
setting working" reports. validate_settings_overrides() compares a to-be-merged
settings dict against the currently loaded settings (configuration.toml is the
authoritative listing of options) and logs a warning for anything unknown. It
never blocks the merge: keys read via get_settings().get(..., default) may
legitimately be missing from the defaults.

validate_current_config() is a doctor-style coherence check used by
`pr-agent --validate_config`.
"""

from pr_agent.log import get_logger

# Sections whose keys are user-defined or credential-shaped, so key-level
# comparison against the defaults would only produce noise.
FREE_FORM_SECTIONS = frozenset({
    "custom_labels",
    "best_practices",
    "pr_custom_prompt",
})

# Credential/provider sections that live in .secrets.toml or env vars rather
# than configuration.toml, and so may be absent from the loaded defaults.
CREDENTIAL_SECTIONS = frozenset({
    "anthropic", "aws", "azure", "azure_ad", "azure_devops", "bitbucket",
    "bitbucket_server", "codestral", "cohere", "databricks", "deepinfra",
    "deepseek", "gerrit", "gitea", "github", "gitlab", "google",
    "google_ai_studio", "groq", "huggingface", "langfuse", "litellm",
    "mistral", "mosaico", "ollama", "openai", "openrouter", "pinecone",
    "qdrant", "replicate", "sambanova", "vertexai", "xai",
})


def validate_settings_overrides(overrides: dict, current_settings, source: str) -> list:
    """
    Compare `overrides` (a parsed settings dict about to be merged) against the
    currently loaded settings and log a warning for unknown sections/keys.
    Returns the list of warning strings; never raises, never blocks the merge.
    """
    warnings = []
    try:
        current = {str(section).lower(): value for section, value in current_settings.as_dict().items()}
        for section, contents in (overrides or {}).items():
            section_lower = str(section).lower()
            if not isinstance(contents, dict) or section_lower in FREE_FORM_SECTIONS:
                continue
            if section_lower not in current:
                if section_lower not in CREDENTIAL_SECTIONS:
                    warnings.append(
                        f"{source}: section [{section}] is not present in the default configuration "
                        f"(see pr_agent/settings/configuration.toml) - possible typo")
                continue
            known_section = current[section_lower]
            if not isinstance(known_section, dict) or section_lower in CREDENTIAL_SECTIONS:
                continue
            known_keys = {str(k).lower() for k in known_section}
            for key in contents:
                if str(key).lower() not in known_keys:
                    warnings.append(
                        f"{source}: key '{key}' in section [{section}] is not present in the default "
                        f"configuration (see pr_agent/settings/configuration.toml) - possible typo")
        for warning in warnings:
            get_logger().warning(warning)
    except Exception as e:
        get_logger().debug(f"Settings override validation skipped due to error: {e}")
    return warnings


def validate_current_config(settings) -> dict:
    """
    Doctor-style coherence check of the loaded configuration.
    Returns {"errors": [...], "warnings": [...]}; never raises.
    """
    errors = []
    warnings = []

    # git provider
    from pr_agent.git_providers import _GIT_PROVIDERS
    git_provider = settings.get("config.git_provider", None)
    if not git_provider:
        errors.append("config.git_provider is not set")
    elif git_provider not in _GIT_PROVIDERS:
        errors.append(f"config.git_provider '{git_provider}' is unknown; "
                      f"expected one of {sorted(_GIT_PROVIDERS)}")

    # models resolve to a max-token budget
    from pr_agent.algo.utils import get_max_tokens
    for key in ("config.model", "config.fallback_models"):
        value = settings.get(key, None)
        if value is None or (key == "config.model" and not value):
            errors.append(f"{key} is not set")
            continue
        if isinstance(value, str):
            models = [model.strip() for model in value.split(",") if model.strip()]
        else:
            models = value if isinstance(value, list) else [value]
        for model in models:
            try:
                get_max_tokens(model)
            except Exception:
                errors.append(f"{key} '{model}' has no known max-token budget: not in MAX_TOKENS, "
                              f"no litellm metadata, and config.custom_model_max_tokens is not set")

    # provider credentials (soft check - many setups inject them via env at runtime)
    credential_keys_by_provider = {
        "github": ["github.user_token", "github.private_key", "github.app_id"],
        "gitlab": ["gitlab.personal_access_token"],
        "bitbucket": ["bitbucket.bearer_token", "bitbucket.auth_type"],
        "bitbucket_server": ["bitbucket_server.bearer_token"],
        "azure": ["azure_devops.pat"],
        "gitea": ["gitea.token", "gitea.personal_access_token"],
    }
    for key_candidates in [credential_keys_by_provider.get(git_provider, [])]:
        if key_candidates and not any(settings.get(k, None) for k in key_candidates):
            warnings.append(f"no credentials found for git provider '{git_provider}' "
                            f"(checked: {key_candidates}); fine if injected via environment at runtime")

    return {"errors": errors, "warnings": warnings}
