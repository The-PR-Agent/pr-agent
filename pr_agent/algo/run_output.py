from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import PackageNotFoundError, version

import yaml

from pr_agent.algo.i18n import t
from pr_agent.algo.output_sinks import OUTPUT_SINK_TYPES, _selected_output_commands, create_output_sink
from pr_agent.algo.run_details import get_run_details
from pr_agent.algo.setting_utils import _as_bool_setting, normalize_setting_list
from pr_agent.config_loader import get_settings
from pr_agent.config_security import should_skip_config_key
from pr_agent.git_providers.git_provider import redact_credentials
from pr_agent.log import get_logger


def github_action_output(output_data: dict, key_name: str):
    try:
        enable_output = get_settings().get("github_action_config.enable_output", False)
        if not _as_bool_setting(enable_output):
            return

        key_data = output_data.get(key_name, {})
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            print(f"{key_name}={json.dumps(key_data, indent=None, ensure_ascii=False)}", file=fh)
    except Exception as e:
        get_logger().error(f"Failed to write to GitHub Action output: {e}")
    return


def _pr_identity(git_provider) -> dict | None:
    if git_provider is None:
        return None
    try:
        identity = git_provider.get_pr_identity()
    except Exception:
        return None
    if not isinstance(identity, dict):
        return None
    identity = dict(identity)
    identity["url"] = redact_credentials(identity.get("url", ""))
    return identity


def push_outputs(
    message_type: str,
    payload: dict | None = None,
    markdown: str | None = None,
    git_provider=None,
) -> None:
    """Emit a command's output to external sinks, including its PR identity when available.

    Controlled by the [push_outputs] config section (disabled by default). Supported channels:
    "stdout" (one JSON line), "file" (append JSONL), "webhook" (POST the generic record),
    "slack" (POST {"text": ...} to a Slack Incoming Webhook), and "telegram"
    (sendMessage to a configured chat). Non-fatal: never raises.
    """
    try:
        cfg = get_settings().get("push_outputs", {}) or {}
        enable = cfg.get("enable", False)
        if isinstance(enable, str):
            enable = enable.strip()
        if not _as_bool_setting(enable, default=bool(enable)):
            return

        commands = _selected_output_commands(cfg)
        if commands is None:
            get_logger().warning("push_outputs: invalid push_outputs.commands setting")
            return
        if message_type not in commands:
            return

        channels = cfg.get("channels", []) or []
        if isinstance(channels, str):
            channels = normalize_setting_list(channels, split_commas=True)
        record = {
            "type": message_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": payload or {},
        }
        identity = _pr_identity(git_provider)
        if identity is not None:
            record["pr"] = identity
        if markdown is not None:
            record["markdown"] = markdown

        for channel in OUTPUT_SINK_TYPES:
            if channel not in channels:
                continue
            try:
                create_output_sink(channel).send(record, cfg)
            except Exception as e:
                get_logger().warning(f"push_outputs: {channel} failed: {type(e).__name__}")
    except Exception as e:
        # Log only the exception type: requests errors embed the (secret-bearing) URL in their text.
        get_logger().warning(f"push_outputs failed: {type(e).__name__}")


async def push_outputs_async(
    message_type: str,
    payload: dict | None = None,
    markdown: str | None = None,
    git_provider=None,
) -> None:
    """Emit a command's output to external sinks without blocking the event loop.

    The sinks do blocking HTTP and file I/O, so the delivery runs in a worker thread.
    Awaiting it keeps the event loop free for other work and still delivers the output
    before the command returns.
    """
    await asyncio.to_thread(
        push_outputs, message_type, payload=payload, markdown=markdown,
        git_provider=git_provider,
    )


def _render_setting_value(value) -> str:
    """Render a settings value as YAML, so nested values do not become Python reprs."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    try:
        return yaml.safe_dump(_to_plain(value), default_flow_style=True).strip()
    except Exception:
        return str(value)


def _to_plain(value):
    """Convert Dynaconf boxes to plain dict/list so yaml can represent them."""
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def show_relevant_configurations(relevant_section: str) -> str:
    skip_keys = [
        "ai_disclaimer", "ai_disclaimer_title", "ANALYTICS_FOLDER", "secret_provider",
        "skip_keys", "app_id", "redirect", "trial_prefix_message", "no_eligible_message",
        "identity_provider", "ALLOWED_REPOS", "APP_NAME",
    ]
    skip_keys.extend(normalize_setting_list(get_settings().config.get("skip_keys", [])))
    markdown_text = ""
    markdown_text += "\n<hr>\n<details> <summary><strong>🛠️ Relevant configurations:</strong></summary> \n\n"
    markdown_text += (
        "<br>These are the relevant [configurations]"
        "(https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml)"
        " for this command:\n\n"
    )
    markdown_text += "**[config**]\n```yaml\n\n"
    for key, value in get_settings().config.items():
        if should_skip_config_key("config", key, skip_keys):
            continue
        markdown_text += f"{key}: {_render_setting_value(value)}\n"
    markdown_text += "\n```\n"
    markdown_text += f"\n**[{relevant_section}]**\n```yaml\n\n"
    for key, value in get_settings().get(relevant_section, {}).items():
        if should_skip_config_key(relevant_section, key, skip_keys):
            continue
        markdown_text += f"{key}: {_render_setting_value(value)}\n"
    markdown_text += "\n```"
    markdown_text += "\n</details>\n"
    return markdown_text


def _format_usd(cost: Decimal) -> str:
    """Format cost at two decimals without turning a positive amount into false zero."""
    rounded = cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if cost > 0 and rounded == 0:
        return "<$0.01"
    return f"${rounded:.2f}"


def show_run_details(gfm_supported: bool) -> str:
    """Render the opt-in run-details section (model, tokens, time cost, AI calls).

    Falls back to a plain, non-collapsible section when the provider does not
    support GitHub-flavored markdown, so the information stays visible.
    """
    details = get_run_details()
    if details is None or not details.model_used:
        return ""

    title = f"⚙️ {t('run_details.title')}"
    if len(details.models_used) > 1:
        fallback = f" ({t('run_details.includes_fallback')})" if details.fallback_used else ""
        lines = [f"- {t('run_details.models')}: {', '.join(details.models_used)}{fallback}"]
    else:
        fallback = f" ({t('run_details.fallback')})" if details.fallback_used else ""
        lines = [f"- {t('run_details.model')}: {details.model_used}{fallback}"]
    if details.has_token_usage:
        # Drop zero-valued components because providers may omit their usage counts.
        counts = [(details.prompt_tokens, "tokens_in"), (details.completion_tokens, "tokens_out"),
                  (details.total_tokens, "tokens_total")]
        reported = [f"{value:,} {t(f'run_details.{label}')}" for value, label in counts if value]
        lines.append(f"- {t('run_details.tokens')}: {' / '.join(reported)}")
    if details.has_cache_usage:
        # Prompt-cache activity proves whether the cache config is earning anything.
        cache_counts = [(details.cache_read_tokens, "cache_read"), (details.cache_creation_tokens, "cache_written")]
        cache_reported = [f"{value:,} {t(f'run_details.{label}')}" for value, label in cache_counts if value]
        lines.append(f"- {t('run_details.prompt_cache')}: {' / '.join(cache_reported)}")
    lines.append(f"- {t('run_details.time_cost')}: {details.duration_seconds:.1f}s")
    if details.num_ai_calls:
        lines.append(f"- {t('run_details.ai_calls')}: {details.num_ai_calls}")
    if get_settings().get("config.output_run_cost", False) and details.num_ai_calls:
        if details.cost_status == "unavailable":
            # Report pricing as unavailable without asserting a cause: usage may be
            # missing (streaming without a final usage chunk) or uncollected by the handler.
            lines.append(f"- {t('run_details.estimated_cost')}: {t('run_details.cost_unavailable')}")
        else:
            partial = ""
            if details.cost_status == "partial":
                cost_partial = t("run_details.cost_partial", priced=details.known_cost_call_count,
                                 calls=details.num_ai_calls)
                partial = f" ({cost_partial})"
            lines.append(f"- {t('run_details.estimated_cost')}: {_format_usd(details.total_cost_usd)} USD{partial}")
            if len(details.model_costs_usd) > 1:
                for model, cost in details.model_costs_usd.items():
                    lines.append(f"  - {model}: {_format_usd(cost)} USD")
    body = "\n".join(lines)

    if gfm_supported:
        return (f"\n<hr>\n<details> <summary><strong>{title}</strong></summary>\n\n"
                f"{body}\n\n</details>\n")
    return f"\n___\n\n**{title}**\n\n{body}\n"


def show_fallback_note() -> str:
    """Render a visible one-line note naming the fallback model that produced the output.

    Shown regardless of ``config.output_run_details``: that footer is off by default and
    collapsed, yet a fallback answer can differ in quality from the configured model.
    """
    details = get_run_details()
    if details is None or not details.fallback_used or not details.model_used:
        return ""
    if len(details.models_used) > 1:
        models = ", ".join(f"`{model}`" for model in details.models_used)
        note = t("run_details.fallback_note_partial", models=models)
    else:
        note = t("run_details.fallback_note", model=f"`{details.model_used}`")
    return f"\n\n> {note}\n"


def append_run_details(body: str, git_provider) -> str:
    """Append the fallback note and the opt-in run-details section to a comment body.

    The fallback note is added whenever a fallback model produced the output. The
    run-details section is skipped when ``config.output_run_details`` is off, or when the
    current run recorded no model (so a message that needed no AI call stays clean).
    A provider without capability checks gets the plain-text section.
    """
    body += show_fallback_note()
    if not get_settings().get("config.output_run_details", False):
        return body
    is_supported = getattr(git_provider, "is_supported", None)
    gfm_supported = bool(callable(is_supported) and is_supported("gfm_markdown"))
    return body + show_run_details(gfm_supported)


def get_version() -> str:
    # First check pyproject.toml if running directly out of the pr-agent repository
    if os.path.exists("pyproject.toml"):
        import tomllib
        try:
            with open("pyproject.toml", "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError) as e:  # tomllib raises TOMLDecodeError, or UnicodeDecodeError on non-UTF-8
            get_logger().warning(f"Unable to read pyproject.toml, falling back to package metadata: {e}")
        else:
            # only trust this file when it is pr-agent's own pyproject.toml, otherwise an
            # unrelated project in the current working directory would dictate our version
            project = data.get("project", {})
            if project.get("name") == "pr-agent":
                if "version" in project:
                    return project["version"]
                get_logger().warning("Version not found in pyproject.toml")

    # Otherwise get the installed pip package version
    try:
        return version('pr-agent')
    except PackageNotFoundError:
        get_logger().warning("Unable to find package named 'pr-agent'")
        return "unknown"
