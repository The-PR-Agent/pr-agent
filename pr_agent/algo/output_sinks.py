"""Delivery strategies for host-configured command output destinations."""

from __future__ import annotations

import json
import os
import sys
from typing import Protocol
from urllib.parse import quote, urlparse

import requests

from pr_agent.algo.setting_utils import _as_bool_setting, normalize_setting_list
from pr_agent.log import get_logger


class OutputSink(Protocol):
    def send(self, record: dict, cfg: dict) -> None:
        """Deliver one record using host-controlled settings; let the caller isolate failures."""
        pass


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


def _post_json(channel: str, url: str, body: dict) -> None:
    """Post to a validated sink URL with the shared transport policy."""
    # Never follow a redirect from a configured sink to another host.
    response = requests.post(url, json=body, timeout=5, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        get_logger().warning(f"push_outputs: {channel} failed with status {response.status_code}")


def _pr_url(record: dict) -> str:
    pr = record.get("pr")
    if not isinstance(pr, dict):
        return ""
    url = pr.get("url")
    return url if isinstance(url, str) else ""


def _append_pr_url(text: str, record: dict) -> str:
    url = _pr_url(record)
    if not url:
        return text
    return f"{text}\n\n{url}" if text else url


def _truncate_utf16(text: str, max_units: int = 4096) -> str:
    """Limit text to `max_units` UTF-16 code units without splitting a surrogate pair."""
    return text.encode("utf-16-le", "surrogatepass")[:max_units * 2].decode("utf-16-le", "ignore")


class StdoutSink:
    def send(self, record: dict, cfg: dict) -> None:
        try:
            print(json.dumps(record, ensure_ascii=False))
        except UnicodeEncodeError:
            sys.stdout.write(json.dumps(record, ensure_ascii=True) + "\n")


class FileSink:
    def send(self, record: dict, cfg: dict) -> None:
        file_path = cfg.get("file_path", "pr-agent-outputs/reviews.jsonl")
        folder = os.path.dirname(file_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class WebhookSink:
    def send(self, record: dict, cfg: dict) -> None:
        url = _push_outputs_sink_url(cfg, "webhook_url")
        if url:
            _post_json("webhook", url, record)


class SlackSink:
    def send(self, record: dict, cfg: dict) -> None:
        url = _push_outputs_sink_url(cfg, "slack_webhook_url")
        if not url:
            return
        text = record.get("markdown")
        if text is None:
            text = json.dumps(record["payload"], ensure_ascii=False)
        # Records carry PR-controlled text and model output; escape Slack control syntax
        # (mentions, <url|label> links) as Slack's formatting guide asks.
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        _post_json("slack", url, {"text": _append_pr_url(text, record)})


class TelegramSink:
    def send(self, record: dict, cfg: dict) -> None:
        bot_token = str(cfg.get("telegram_bot_token") or "").strip()
        chat_id = str(cfg.get("telegram_chat_id") or "").strip()
        missing_keys = [
            key for key, value in (("telegram_bot_token", bot_token), ("telegram_chat_id", chat_id))
            if not value
        ]
        if missing_keys:
            get_logger().warning(f"push_outputs: telegram channel missing {', '.join(missing_keys)}")
            return

        text = record.get("markdown")
        if text is None:
            text = json.dumps(record["payload"], ensure_ascii=False)
        # Keep the host fixed and encode the token as a path component, not a URL.
        url = f"https://api.telegram.org/bot{quote(bot_token, safe=':')}/sendMessage"
        # Reserve room for the PR link so truncating a long message never drops it.
        pr_url = _pr_url(record)
        if pr_url:
            suffix = f"\n\n{pr_url}" if text else pr_url
            suffix_units = len(suffix.encode("utf-16-le", "surrogatepass")) // 2
            text = _truncate_utf16(text, max(0, 4096 - suffix_units)) + suffix
        text = _truncate_utf16(text)
        _post_json("telegram", url, {"chat_id": chat_id, "text": text})


class TeamsSink:
    message_size_limit = 28 * 1024
    truncation_marker = "\n\n[Output truncated]"

    @staticmethod
    def _card(record: dict, markdown: str) -> dict:
        title = str(record.get("type") or "output").replace("_", " ").title()
        card = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": f"PR-Agent {title}", "weight": "Bolder", "size": "Medium"},
                        {"type": "TextBlock", "text": markdown, "wrap": True},
                    ],
                },
            }],
        }
        pr = record.get("pr")
        pr_url = pr.get("url") if isinstance(pr, dict) else ""
        if not isinstance(pr_url, str):
            pr_url = ""
        if pr_url:
            card["attachments"][0]["content"]["actions"] = [{
                "type": "Action.OpenUrl", "title": "Open pull request", "url": pr_url,
            }]
        return card

    @staticmethod
    def _serialized_size(card: dict) -> int:
        # Measure the default JSON encoding used by requests.post(json=card).
        return len(json.dumps(card).encode("utf-8"))

    def send(self, record: dict, cfg: dict) -> None:
        url = _push_outputs_sink_url(cfg, "teams_webhook_url")
        if not url:
            return
        markdown = record.get("markdown")
        if markdown is None:
            markdown = json.dumps(record.get("payload", {}), ensure_ascii=False)

        card = self._card(record, markdown)
        if self._serialized_size(card) > self.message_size_limit:
            low, high = 0, len(markdown)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = self._card(record, markdown[:middle] + self.truncation_marker)
                if self._serialized_size(candidate) <= self.message_size_limit:
                    low = middle
                else:
                    high = middle - 1
            markdown = markdown[:low] + self.truncation_marker
            card = self._card(record, markdown)
        _post_json("teams", url, card)


# Keep local channels before network channels, regardless of configuration order.
# Attempt each selected channel once.
OUTPUT_SINK_TYPES: dict[str, type[OutputSink]] = {
    "stdout": StdoutSink,
    "file": FileSink,
    "webhook": WebhookSink,
    "slack": SlackSink,
    "telegram": TelegramSink,
    "teams": TeamsSink,
}


def _selected_output_commands(cfg: dict) -> list[str] | None:
    """Return selected output types, or None for an invalid configuration."""
    commands = cfg.get("commands", ["review", "describe"])
    if isinstance(commands, str):
        commands = normalize_setting_list(commands, split_commas=True)
    if (not isinstance(commands, (list, tuple)) or not commands
            or any(not isinstance(name, str) or name not in ("review", "describe", "improve", "ask", "update_changelog")
                   for name in commands)):
        return None
    return list(commands)


def _check_output_sinks(cfg: dict) -> tuple[bool, str] | None:
    """Check basic enabled destination settings without constructing sinks or probing resources."""
    enable = cfg.get("enable", False)
    if isinstance(enable, str):
        enable = enable.strip()
    if not _as_bool_setting(enable, default=bool(enable)):
        return None

    if _selected_output_commands(cfg) is None:
        return False, "push_outputs.commands must select supported command names"

    channels = cfg.get("channels", [])
    if isinstance(channels, str):
        channels = normalize_setting_list(channels, split_commas=True)
    if not isinstance(channels, (list, tuple)):
        return False, "push_outputs.channels must select registered output sinks"
    if any(not isinstance(name, str) or name not in OUTPUT_SINK_TYPES for name in channels):
        return False, "push_outputs.channels must select registered output sinks"
    selected = [name for name in OUTPUT_SINK_TYPES if name in channels]
    if not selected:
        return False, "push_outputs.channels must select registered output sinks"

    if "file" in selected:
        path = cfg.get("file_path", "pr-agent-outputs/reviews.jsonl")
        if not isinstance(path, str) or not path:
            return False, "push_outputs.file_path must be a nonempty string"
    for channel, key in (
        ("webhook", "webhook_url"), ("slack", "slack_webhook_url"), ("teams", "teams_webhook_url"),
    ):
        if channel not in selected:
            continue
        url = cfg.get(key)
        valid = False
        if isinstance(url, str):
            try:
                parsed = urlparse(url)
                valid = parsed.scheme == "https" and bool(parsed.hostname)
            except ValueError:
                # Malformed URLs use the same value-free failure message below.
                pass
        if not valid:
            return False, f"push_outputs.{key} must be an absolute https:// URL with a host"
    if "telegram" in selected:
        for key in ("telegram_bot_token", "telegram_chat_id"):
            value = cfg.get(key)
            if key == "telegram_chat_id" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value or "")
            if not isinstance(value, str) or not value.strip():
                if key == "telegram_chat_id":
                    return False, "push_outputs.telegram_chat_id must be a nonempty string or nonzero integer"
                return False, f"push_outputs.{key} must be a nonempty string"
    # Distinct custom names are recognized only; their private configuration is not inspected.
    return True, "push_outputs basic destination settings checked (custom sinks: recognition only)"


def create_output_sink(channel: str) -> OutputSink:
    """Instantiate a registered strategy without doing I/O or reading global settings."""
    return OUTPUT_SINK_TYPES[channel]()
