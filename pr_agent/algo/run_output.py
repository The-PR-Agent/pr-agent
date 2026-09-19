from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlparse

import requests
import yaml

from pr_agent.algo.run_details import get_run_details
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def github_action_output(output_data: dict, key_name: str):
    try:
        enable_output = get_settings().get("github_action_config.enable_output", False)
        if isinstance(enable_output, str):
            enable_output = enable_output.lower().strip() not in ("false", "0", "no", "")
        if not enable_output:
            return

        key_data = output_data.get(key_name, {})
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            print(f"{key_name}={json.dumps(key_data, indent=None, ensure_ascii=False)}", file=fh)
    except Exception as e:
        get_logger().error(f"Failed to write to GitHub Action output: {e}")
    return


def _push_outputs_sink_url(cfg: dict, key: str) -> str:
    """Return cfg[key] if it is an absolute https URL with a host, else "" (with a warning).

    Requiring https keeps the review text, which can quote private code, off plaintext
    transports. The host is not restricted: self-hosted collectors and Slack-compatible
    endpoints (Mattermost, Rocket.Chat) are legitimate targets.
    """
    url = cfg.get(key) or ""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        # Log the key, never the value: a webhook URL is itself the credential.
        get_logger().warning(f"push_outputs: ignoring {key}, expected an absolute https:// URL")
        return ""
    return url


def push_outputs(message_type: str, payload: dict | None = None, markdown: str | None = None) -> None:
    """Emit a tool's output to external sinks, without calling any git-provider API.

    Controlled by the [push_outputs] config section (disabled by default). Supported channels:
    "stdout" (one JSON line), "file" (append JSONL), "webhook" (POST the generic record),
    "slack" (POST {"text": ...} to a Slack Incoming Webhook). Non-fatal: never raises.
    """
    try:
        cfg = get_settings().get("push_outputs", {}) or {}
        enable = cfg.get("enable", False)
        # Treat environment-variable strings "false", "0", "no", and "" as disabled.
        if isinstance(enable, str):
            enable = enable.lower().strip() not in ("false", "0", "no", "")
        if not enable:
            return

        channels = cfg.get("channels", []) or []
        record = {
            "type": message_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        if markdown is not None:
            record["markdown"] = markdown

        if "stdout" in channels:
            try:
                print(json.dumps(record, ensure_ascii=False))
            except Exception as e:
                get_logger().warning(f"push_outputs: stdout failed: {type(e).__name__}")

        if "file" in channels:
            try:
                file_path = cfg.get("file_path", "pr-agent-outputs/reviews.jsonl")
                folder = os.path.dirname(file_path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with open(file_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                get_logger().warning(f"push_outputs: file failed: {type(e).__name__}")

        # Write local channels first and network last so a failed POST cannot drop a file write.
        # Never follow a redirect from a configured sink to another host (allow_redirects=False).
        if "webhook" in channels:
            try:
                webhook_url = _push_outputs_sink_url(cfg, "webhook_url")
                if webhook_url:
                    response = requests.post(webhook_url, json=record, timeout=5, allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        get_logger().warning(f"push_outputs: webhook failed with status {response.status_code}")
            except Exception as e:
                get_logger().warning(f"push_outputs: webhook failed: {type(e).__name__}")

        # Post {"text": ...} directly to Slack Incoming Webhooks without a relay service.
        if "slack" in channels:
            try:
                slack_webhook_url = _push_outputs_sink_url(cfg, "slack_webhook_url")
                if slack_webhook_url:
                    text = markdown if markdown is not None else json.dumps(payload or {}, ensure_ascii=False)
                    response = requests.post(slack_webhook_url, json={"text": text}, timeout=5,
                                             allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        get_logger().warning(f"push_outputs: slack failed with status {response.status_code}")
            except Exception as e:
                get_logger().warning(f"push_outputs: slack failed: {type(e).__name__}")
    except Exception as e:
        # Log only the exception type: requests errors embed the (secret-bearing) URL in their text.
        get_logger().warning(f"push_outputs failed: {type(e).__name__}")


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
    extra_skip_keys = get_settings().config.get("skip_keys", [])
    if extra_skip_keys:
        skip_keys.extend(extra_skip_keys)
    skip_keys_lower = [str(key).lower() for key in skip_keys]

    markdown_text = ""
    markdown_text += "\n<hr>\n<details> <summary><strong>🛠️ Relevant configurations:</strong></summary> \n\n"
    markdown_text += (
        "<br>These are the relevant [configurations]"
        "(https://github.com/Codium-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml)"
        " for this tool:\n\n"
    )
    markdown_text += "**[config**]\n```yaml\n\n"
    for key, value in get_settings().config.items():
        if key.lower() in skip_keys_lower:
            continue
        markdown_text += f"{key}: {_render_setting_value(value)}\n"
    markdown_text += "\n```\n"
    markdown_text += f"\n**[{relevant_section}]**\n```yaml\n\n"
    for key, value in get_settings().get(relevant_section, {}).items():
        if key.lower() in skip_keys_lower:
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

    title = "⚙️ Agent run details"
    if len(details.models_used) > 1:
        lines = [f"- Models: {', '.join(details.models_used)}{' (includes fallback)' if details.fallback_used else ''}"]
    else:
        lines = [f"- Model: {details.model_used}{' (fallback)' if details.fallback_used else ''}"]
    if details.has_token_usage:
        # Drop zero-valued components because providers may omit their usage counts.
        counts = [(details.prompt_tokens, "in"), (details.completion_tokens, "out"),
                  (details.total_tokens, "total")]
        reported = [f"{value:,} {label}" for value, label in counts if value]
        lines.append(f"- Tokens: {' / '.join(reported)}")
    lines.append(f"- Time cost: {details.duration_seconds:.1f}s")
    if details.num_ai_calls:
        lines.append(f"- AI calls: {details.num_ai_calls}")
    if get_settings().get("config.output_run_cost", False) and details.num_ai_calls:
        if details.cost_status == "unavailable":
            # Report pricing as unavailable without asserting a cause: usage may be
            # missing (streaming without a final usage chunk) or uncollected by the handler.
            lines.append("- Estimated API cost: unavailable (no calls could be priced)")
        else:
            partial = ""
            if details.cost_status == "partial":
                partial = (f" (partial: {details.known_cost_call_count} of "
                           f"{details.num_ai_calls} successful calls priced)")
            lines.append(f"- Estimated API cost: {_format_usd(details.total_cost_usd)} USD{partial}")
            if len(details.model_costs_usd) > 1:
                for model, cost in details.model_costs_usd.items():
                    lines.append(f"  - {model}: {_format_usd(cost)} USD")
    body = "\n".join(lines)

    if gfm_supported:
        return (f"\n<hr>\n<details> <summary><strong>{title}</strong></summary>\n\n"
                f"{body}\n\n</details>\n")
    return f"\n___\n\n**{title}**\n\n{body}\n"


def get_version() -> str:
    # First check pyproject.toml if running directly out of the pr-agent repository
    if os.path.exists("pyproject.toml"):
        if sys.version_info >= (3, 11):
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
        else:
            get_logger().warning("Unable to determine local version from pyproject.toml")

    # Otherwise get the installed pip package version
    try:
        return version('pr-agent')
    except PackageNotFoundError:
        get_logger().warning("Unable to find package named 'pr-agent'")
        return "unknown"
