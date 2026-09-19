"""Regression guard for dead configuration keys.

Every key in pr_agent/settings/configuration.toml must be read somewhere in
pr_agent/ source code. Static detection is deliberately conservative: keys are
matched through the common read patterns below, and keys that are genuinely
used through variable indirection (e.g. a settings section assigned to a local
variable and then indexed) are recorded in the allowlist with a note pointing
at the reading site.

A whole-section read (settings.get("SECTION", ...)) does not count as reading
its keys: each key must be matched through an attribute chain, a quoted
dotted path, or a section-level .get("KEY") / getattr() call, or it must be
allowlisted.

Adding a key without a reader - or leaving a config-only key in the TOML -
fails this test, so the "dead key ledger" cannot silently regrow.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"
PR_AGENT_SOURCE = ROOT / "pr_agent"

# Keys read through a settings section stored in a local variable, so the
# static dotted-path scan cannot see them. Each entry documents the reader.
_ALLOWLIST = {
    # [artifacts] read as artifacts_settings = get_settings().get("ARTIFACTS", {})
    (
        "artifacts",
        "max_artifact_size",
    ): "artifacts_settings.get('max_artifact_size', 50000) in pr_agent/algo/artifacts.py",
    (
        "artifacts",
        "artifact_label",
    ): "artifacts_settings.get('artifact_label', '') in pr_agent/algo/artifacts.py",
    # [config] helpers that re-index the section from a variable or expression
    (
        "config",
        "reaction_on_failure",
    ): "get_reaction_setting('reaction_on_success' if succeeded else 'reaction_on_failure') "
    "reads get_settings().config.get(name) in pr_agent/git_providers/git_provider.py",
    (
        "config",
        "output_relevant_configurations",
    ): "get_settings().get('config', {}).get('output_relevant_configurations', False) in "
    "pr_agent/tools/pr_description.py",
    (
        "config",
        "enable_claude_adaptive_thinking",
    ): "settings.config.get(key, default) in the thinking-controls loop in "
    "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "config",
        "enable_claude_extended_thinking",
    ): "settings.config.get(key, default) in the thinking-controls loop in "
    "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "config",
        "extended_thinking_budget_tokens",
    ): "settings.config.get(key, default) in the thinking-controls loop in "
    "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "config",
        "extended_thinking_max_output_tokens",
    ): "settings.config.get(key, default) in the thinking-controls loop in "
    "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    # [gerrit] read as gerrit_settings = get_settings().get("gerrit", {})
    (
        "gerrit",
        "webhook_username",
    ): "gerrit_settings.get('webhook_username', None) in pr_agent/servers/gerrit_server.py",
    (
        "gerrit",
        "webhook_password",
    ): "gerrit_settings.get('webhook_password', None) in pr_agent/servers/gerrit_server.py",
    (
        "github",
        "api_retries",
    ): "github_config = get_settings().github then github_config.get('api_retries') in "
    "pr_agent/git_providers/github_provider.py",
    (
        "github",
        "seconds_between_requests",
    ): "github_config.get('seconds_between_requests') in pr_agent/git_providers/github_provider.py",
    (
        "github",
        "seconds_between_writes",
    ): "github_config.get('seconds_between_writes') in pr_agent/git_providers/github_provider.py",
    # [openrouter] read as openrouter_settings = settings.get("openrouter", {}) or {}
    (
        "openrouter",
        "provider_only",
    ): "openrouter_settings.get('provider_only', []) in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "openrouter",
        "provider_order",
    ): "openrouter_settings.get('provider_order', []) in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "openrouter",
        "allow_fallbacks",
    ): "openrouter_settings.get('allow_fallbacks', True) in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "openrouter",
        "reasoning_effort",
    ): "openrouter_settings.get('reasoning_effort', '') in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "openrouter",
        "reasoning_max_tokens",
    ): "openrouter_settings.get('reasoning_max_tokens', 0) in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "openrouter",
        "max_tokens",
    ): "openrouter_settings.get('max_tokens', 0) in pr_agent/algo/ai_handlers/litellm_ai_handler.py",
    (
        "pr_description",
        "pr_diagram_direction",
    ): "description_settings = get_settings().pr_description then "
    "description_settings.pr_diagram_direction in pr_agent/tools/pr_description.py",
    (
        "pr_description",
        "pr_diagram_direction_threshold",
    ): "description_settings.pr_diagram_direction_threshold in pr_agent/tools/pr_description.py",
    # [push_outputs] read as cfg = get_settings().get('push_outputs', {}) or {}
    (
        "push_outputs",
        "enable",
    ): "cfg.get('enable', False) in pr_agent/algo/utils.py",
    (
        "push_outputs",
        "channels",
    ): "cfg.get('channels', []) in pr_agent/algo/utils.py",
    (
        "push_outputs",
        "file_path",
    ): "cfg.get('file_path', 'pr-agent-outputs/reviews.jsonl') in pr_agent/algo/utils.py",
    (
        "push_outputs",
        "webhook_url",
    ): "_push_outputs_sink_url(cfg, 'webhook_url') in pr_agent/algo/utils.py",
    (
        "push_outputs",
        "slack_webhook_url",
    ): "_push_outputs_sink_url(cfg, 'slack_webhook_url') in pr_agent/algo/utils.py",
}


def _read_sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(PR_AGENT_SOURCE.rglob("*.py"))
    ).lower()


def _key_is_read(section: str, key: str, sources: str) -> bool:
    section_name = re.escape(section)
    key_name = re.escape(key)
    settings_prefix = (
        r"(?:get_settings\s*\(\s*\)|global_settings|self\s*\.\s*settings|settings)"
    )
    # 1. attribute chain: settings.<section>.<key>
    if re.search(
        settings_prefix + r"\s*\.\s*" + section_name + r"\s*\.\s*" + key_name + r"\b",
        sources,
    ):
        return True
    # 2. quoted dotted paths: "SECTION.KEY" or "section.key"
    for dotted in (f"{section.upper()}.{key.upper()}", f"{section}.{key}"):
        if re.search(re.escape(f"\"{dotted}\"") + r"|" + re.escape(f"'{dotted}'"), sources):
            return True
    # 3. settings.<section>.get("KEY")
    if re.search(
        settings_prefix + r"\s*\.\s*" + section_name + r"\s*\.\s*get\s*\(\s*['\"]"
        + key_name + r"['\"]",
        sources,
    ):
        return True
    # 4. getattr(settings.<section>, "KEY", ...)
    if re.search(
        r"getattr\s*\(\s*" + settings_prefix + r"\s*\.\s*" + section_name
        + r"\s*,\s*['\"]" + key_name + r"['\"]",
        sources,
    ):
        return True
    # 5. helper indirection for the [config] section: _read_bool_setting("key")
    #    and get_reaction_setting("key") both read get_settings().config.get(key).
    if section == "config":
        for helper in ("_read_bool_setting", "get_reaction_setting"):
            if re.search(
                r"\b" + re.escape(helper) + r"\s*\(\s*['\"]" + key_name + r"['\"]",
                sources,
            ):
                return True
    return False


def test_every_config_key_has_a_reader_or_is_allowlisted():
    sources = _read_sources()
    with open(CONFIG_TOML, "rb") as f:
        settings = tomllib.load(f)

    unread = {
        (section, key)
        for section, entries in settings.items()
        if not section.startswith("_")
        for key in entries
        if not _key_is_read(section, key, sources)
    }
    allowlisted = set(_ALLOWLIST)

    assert unread <= allowlisted, (
        "configuration.toml keys have no detected reader; remove them or allowlist them: "
        f"{sorted(unread - allowlisted)}"
    )
    assert (
        allowlisted - unread == set()
    ), "allowlist has stale entries (those keys are now detected as read or no longer present): " \
       f"{sorted(allowlisted - unread)}"
