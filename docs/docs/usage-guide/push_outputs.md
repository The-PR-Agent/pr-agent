# Push outputs to external sinks

The `[push_outputs]` feature routes finished tool output to external sinks — stdout, a JSONL file, a
generic webhook, or Slack — without calling git-provider APIs. It is disabled by default, and is
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
channels = []                          # any of: "stdout", "file", "webhook", "slack"
file_path = "pr-agent-outputs/reviews.jsonl"
webhook_url = ""                       # must be an absolute https:// URL
slack_webhook_url = ""                 # Slack Incoming Webhook; must be an absolute https:// URL
```

- `enable` — master switch (default `false`). When `false`, nothing is emitted.
- `channels` — which sinks to use. Nothing is emitted until at least one channel is listed here.
- `file_path` — the file the `file` channel appends to.
- `webhook_url` — the endpoint the `webhook` channel POSTs the generic record to.
- `slack_webhook_url` — a Slack Incoming Webhook URL that the `slack` channel posts a `{"text": ...}` payload to.

!!! danger "Host-only configuration"
    The whole `[push_outputs]` section is **host-only**. A repository cannot set these keys:
    keys supplied through a repo's local `.pr_agent.toml` are dropped, and CLI arguments
    (`--push_outputs.webhook_url=...`, `--push_outputs={...}`) are blocked. This prevents a
    pull request from redirecting review output to an attacker-controlled host, reaching
    internal endpoints, or appending to arbitrary host files. Configure these values in the
    PR-Agent host's own settings.

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

Local channels (`stdout`, `file`) run before network channels (`webhook`, `slack`), and network
posts never follow redirects, so a failed or redirecting POST cannot lose an already-written file
line or be forwarded to a different host.

## Error handling

Failures are non-fatal: `push_outputs` never raises, so a sink outage does not break the review
flow. Errors are logged with the exception type only, since request error messages can embed the
(secret-bearing) URL.
