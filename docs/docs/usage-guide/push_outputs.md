---
title: "Push outputs to external sinks"
sidebar_position: 7
---

The `[push_outputs]` feature routes finished tool output to external sinks — stdout, a JSONL file, a
generic webhook, Slack, or Telegram — without calling git-provider APIs. It is disabled by default, and is
additive to normal publishing: when a tool finishes, the same result that is posted as a PR comment
is also emitted to the configured sinks.

## What gets pushed

Each finished tool run emits one record. The tools that currently emit are:

| Tool | `type` in the record |
|---|---|
| `/review` | `review` |
| `/describe` | `describe` |
| `/improve` | `improve` |

A record is a JSON object:

```json
{
  "type": "review",
  "timestamp": "2026-09-08T14:03:21+00:00",
  "payload": {},
  "markdown": "# PR Reviewer Guide ..."
}
```

- `type` — which tool produced the output
- `timestamp` — the run completion time, UTC, ISO-8601
- `payload` — the structured tool result
- `markdown` — the rendered comment text; present when the tool produces one

## Configuration

The defaults are defined at the end of the
[configuration file](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml):

```toml
[push_outputs]
enable = false
channels = []                          # any of: "stdout", "file", "webhook", "slack", "telegram"
file_path = "pr-agent-outputs/reviews.jsonl"
webhook_url = ""                       # must be an absolute https:// URL
slack_webhook_url = ""                 # Slack Incoming Webhook; must be an absolute https:// URL
telegram_bot_token = ""                # Telegram bot token; host-only secret
telegram_chat_id = ""                  # destination chat for the Telegram bot
```

- `enable` — master switch (default `false`). When `false`, nothing is emitted.
- `channels` — which sinks to use. Nothing is emitted until at least one channel is listed here.
- `file_path` — the file the `file` channel appends to.
- `webhook_url` — the endpoint the `webhook` channel POSTs the generic record to.
- `slack_webhook_url` — a Slack Incoming Webhook URL that the `slack` channel posts a `{"text": ...}` payload to.
- `telegram_bot_token` — the bot token used by the `telegram` channel. Keep it in host secrets, not a repository file.
- `telegram_chat_id` — the chat that receives Telegram messages.

:::danger[Host-only configuration]
The whole `[push_outputs]` section is **host-only**. A repository cannot set these keys:
keys supplied through a repo's local `.pr_agent.toml` are dropped, and CLI arguments
(`--push_outputs.webhook_url=...`, `--push_outputs={...}`) are blocked. This prevents a
pull request from redirecting review output to an attacker-controlled host, reaching
internal endpoints, or appending to arbitrary host files. Configure these values in the
PR-Agent host's own settings.
:::

### URL requirements

`webhook_url` and `slack_webhook_url` must be absolute `https://` URLs with a host. Any other
value (for example a plain `http://` URL or a bare path) is ignored with a warning. Requiring
HTTPS keeps review text, which can quote private code, off plaintext transports. The host is
intentionally not restricted, so self-hosted collectors and Slack-compatible endpoints
(Mattermost, Rocket.Chat) are legitimate targets.

Warnings log the setting name — never the URL value — because a webhook or Slack URL is itself a
credential.

## Channels

| Channel | Behaviour |
|---|---|
| `stdout` | Prints one JSON line (the record) to stdout. |
| `file` | Appends one JSON line per run (JSONL) to `file_path`, creating parent directories as needed. |
| `webhook` | POSTs the generic record as JSON to `webhook_url` (5-second timeout, redirects not followed). |
| `slack` | POSTs `{"text": ...}` to a Slack Incoming Webhook; the text is the markdown, or the payload JSON when the tool produces no markdown. |
| `telegram` | Sends the markdown, or the payload JSON when no markdown is present, as plain text to `telegram_chat_id`. Text is truncated to 4096 characters. |

Local channels (`stdout`, `file`) run before network channels (`webhook`, `slack`, `telegram`), and network
posts never follow redirects. Each configured destination is attempted independently, so one failure
does not prevent later destinations from receiving the output.

### Telegram

Enable the channel in the host's settings and supply the bot token through the host environment:

```toml
[push_outputs]
enable = true
channels = ["telegram"]
telegram_chat_id = "<destination-chat-id>"
```

Set `PUSH_OUTPUTS__TELEGRAM_BOT_TOKEN` to your bot token in the host's secret environment.
The bot must be able to send messages to the destination chat. Missing credentials skip delivery
with a warning that names only the missing setting.

Requests use the fixed `https://api.telegram.org` host, a 5-second timeout, and no redirects.
The token is URL-encoded in the request path and is never included in PR-Agent's warning messages.
No Telegram parse mode is set: Markdown syntax is sent as plain text. Longer output is truncated,
not split across messages; other configured channels still receive the complete output.

## Error handling

Failures are non-fatal: `push_outputs` never raises, so a sink outage does not break the review
flow. Exceptions and non-2xx HTTP responses are logged with the destination and only the exception
type or status code, since request error messages can embed the (secret-bearing) URL.

## Extending delivery

`push_outputs()` in `pr_agent/algo/run_output.py` builds the record once and isolates failures
for each selected destination. Delivery strategies live in `pr_agent/algo/output_sinks.py`:
each implements `OutputSink.send(record, cfg)`, and `create_output_sink()` selects the strategy
from `OUTPUT_SINK_TYPES`. Registry order determines delivery order, with local writes first;
duplicate channel entries still result in a single delivery.

To add a destination, implement its strategy and register it, then add any required host-only
settings, documentation, and provider-specific tests. HTTP strategies must validate destinations
and preserve the shared HTTPS, timeout, redirect, and secret-safe logging policy. Provider-specific
payload formatting belongs in the strategy, so the generic webhook record remains unchanged.

This interface organizes provider implementations; it does not remove the work of maintaining
their APIs. Retries, rate limiting, idempotency, and background delivery are separate policy
decisions and are not introduced by this structure.
