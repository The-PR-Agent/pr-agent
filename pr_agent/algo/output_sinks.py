"""Delivery strategies for host-configured tool output destinations."""

from __future__ import annotations

import json
import os
from typing import Protocol
from urllib.parse import quote, urlparse

import requests

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


class StdoutSink:
    def send(self, record: dict, cfg: dict) -> None:
        print(json.dumps(record, ensure_ascii=False))


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
        _post_json("slack", url, {"text": text})


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
        _post_json("telegram", url, {"chat_id": chat_id, "text": text[:4096]})


# Keep local channels before network channels, regardless of configuration order.
# Attempt each selected channel once.
OUTPUT_SINK_TYPES: dict[str, type[OutputSink]] = {
    "stdout": StdoutSink,
    "file": FileSink,
    "webhook": WebhookSink,
    "slack": SlackSink,
    "telegram": TelegramSink,
}


def create_output_sink(channel: str) -> OutputSink:
    """Instantiate a registered strategy without doing I/O or reading global settings."""
    return OUTPUT_SINK_TYPES[channel]()
