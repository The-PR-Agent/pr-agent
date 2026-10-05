---
title: "MOSAICO A2A Server"
sidebar_position: 9
---

PR-Agent can run as an [A2A](https://a2a-protocol.org/) 1.0 *solution agent* for the
[MOSAICO](https://mosaico-project.eu/) ecosystem: a small Starlette server that exposes the
standard A2A surface (agent card + JSON-RPC) plus a health probe. It is **not** a fork or a
separate project — the server is PR-Agent code under [`pr_agent/mosaico/`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/mosaico/server.py),
ships in every release wheel, and ships as its own Docker image (`<version>-mosaico_agent`)
starting at `v0.36.0`. The server is unbiased about the git provider: every request carries
either a PR URL or a raw diff. Follow-up messages can refer to that input by reusing the
returned `contextId`; the server considers up to 100 prior tasks in the same context by default
to find the latest PR URL or diff. Set `mosaico.context_history_max_tasks` (1 to 1000) to change
this limit. Task history is kept in memory, so send the input again after a restart or
when routing a follow-up to a different server replica.

### What the mode is

The A2A server exposes three endpoints:

| Path | Method | Purpose |
| --- | --- | --- |
| `/.well-known/agent-card.json` | GET | The A2A agent card |
| `/` | POST | A2A 1.0 JSON-RPC. Sends the `SendMessage` method with an `A2A-Version: 1.0` header (the header is required: the server treats a request without it as protocol 0.3 and rejects it). The reply arrives as a task artifact (`result.task.artifacts[].parts[].text`), not as a status message. |
| `/health` | GET | A **live LLM connectivity probe** — `200` when an LLM round-trip succeeds, `503` otherwise |

Images with health-probe hardening return `Unhealthy: LLM probe failed` for provider
failures; the health check's own warning records only the exception type.
`mosaico.health_timeout_seconds` sets a finite positive deadline in seconds (default: 10)
for cooperative asynchronous preparation, dispatch, stream consumption, and cleanup waiting.
Stream cleanup can continue in the background after this deadline. Synchronous
initialization and blocking SDK work can still exceed it.
Older images may predate these protections.
Set `MOSAICO__HEALTH_TIMEOUT_SECONDS` in the server's environment to override the default.
Invalid values produce the generic unhealthy response (503), rather than using the default.
When increasing the budget, also allow sufficient time in any external health-check client
and container healthcheck; the bundled Compose probe uses a separate 25-second HTTP timeout.

Chat and health-probe streams attempt cleanup on completion, failure, or cancellation.
Consumer cancellation does not interrupt stream cleanup, which may continue after `/health`
times out while the event loop remains active. Cleanup has no separate wait budget or
configuration key and does not impose a local stream admission limit.

The advertised agent card carries the skills `review`, `improve`, `describe`, and `ask`, the
name `"PR-Agent Solution Agent"`, a `version` derived from the running build (never
hand-maintained), and the required
`https://mosaico-project.eu/extensions/mosaico-observability` extension. Streaming is
advertised as disabled, which is load-bearing: the reference agent selects
`message/send` vs `message/stream` from that capability.

### Request limits and caller authentication

MOSAICO uses the shared `config.max_webhook_request_body_bytes` limit (10 MiB by default),
including streamed bodies without a valid Content-Length. Oversized requests return HTTP 413
before JSON-RPC parsing or tool execution. `mosaico.routing_scan_max_chars` (default: 65536,
a positive integer) bounds PR URL and command detection per text segment. An incomplete token
at the boundary is ignored; supplied diffs are still processed in full. Put the PR URL or
command near the start of the message or its surrounding prose.

Configure `mosaico.bearer_tokens` as a map of stable principal names to distinct opaque secrets
in secret settings, or through Dynaconf's JSON environment syntax:

```bash
export MOSAICO__BEARER_TOKENS='@json {"reference-agent":"replace-with-generated-secret"}'
```

With a nonempty map, JSON-RPC and `/health` require `Authorization: Bearer <secret>`.
Missing or invalid credentials return HTTP 401 before reading the body or running the LLM.
The agent-card GET stays public and advertises the bearer requirement without exposing secrets.
Use HTTPS at your ingress and configure the caller to send its credential. Tasks, artifacts,
histories and context follow-ups are scoped to the configured principal; callers sharing a
secret share that principal. Keep principal names stable when rotating secrets. Invalid
credential maps, including duplicate secrets, fail app construction.

The default empty map preserves anonymous access and shared task ownership for a trusted,
single-tenant network. Configure authentication before exposing the service to other callers.
These limits do not provide a task retention policy, rate limit or concurrency quota: the
in-memory store still retains tasks until restart, and each authorized health probe performs
a live completion.

Observability root/super task IDs must be canonical UUIDs. Invalid IDs are omitted independently;
valid IDs are normalized to lowercase before producing Langfuse trace context. A metadata error
does not fail the review.

For the bundled smoke test, provide the client credential as `MOSAICO_BEARER_TOKEN` in the
script's environment when the server map is configured. For the Compose overlay, supply
`PR_AGENT_BEARER_TOKEN` for the healthcheck and add `MOSAICO__BEARER_TOKENS` to the service's
`environment` mapping through your secret configuration. Configure the reference caller's
credential separately. Both probes continue to work without a token in anonymous mode.

### Run the standalone container

The server boots from a bare `docker pull` in a couple of seconds — no repo clone, no build:

```bash
docker pull pragent/pr-agent:0.41.0-mosaico_agent
docker run -d --name pr-agent-mosaico -p 9000:9000 \
  -e API_BASE=https://your-openai-compatible-endpoint/v1 \
  -e API_KEY=sk-... \
  -e MODEL_NAME=openai/your-model-slug \
  pragent/pr-agent:0.41.0-mosaico_agent

curl -s http://localhost:9000/.well-known/agent-card.json | python3 -m json.tool
```

Pin a version tag in production (see the "Immutable releases and version tags" note on the
[installation page](./index.md)); the plain `mosaico_agent`
rolling tag moves to the newest build on every release.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_BASE` | — | Base URL of the OpenAI-compatible LLM endpoint |
| `API_KEY` | — | API key for that endpoint |
| `MODEL_NAME` | — | Model slug to call |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `9000` | Bind port |
| `AGENT_CARD_HOST`, `AGENT_CARD_PORT` | unset | URL advertised in the card's `supportedInterfaces`; see the warning below |
| `MODEL_MAX_TOKENS` | `32000` | Token budget for models whose context size pr-agent does not already know |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | unset | Optional Langfuse observability |

:::warning[AGENT_CARD_HOST / AGENT_CARD_PORT — the one thing to get right]

These two variables set the URL the agent advertises in `supportedInterfaces`. Leave them
unset and the card advertises `http://localhost:9000/`, which is reachable only from
inside the container itself. The failure this causes is **silent and late**: registration
with MOSAICO succeeds, the repository stores the unreachable URL, and the reference agent
only fails to dereference it once it tries to route a task to this agent. Set them to
whatever host/port the *caller* will use to reach the container, and verify with:

```bash
curl -s http://<host>:<port>/.well-known/agent-card.json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['supportedInterfaces'][0]['url'])"
```

If that prints a `localhost` URL, the deployment is wrong.
:::

### Deploy into the mosaico-demonstrator

The [`docker/mosaico/`](https://github.com/the-pr-agent/pr-agent/tree/main/docker/mosaico)
directory is a full deployment bundle (compose overlay, registration template, env template,
smoke test, LICENSE, and the canonical README). To run the agent as a task agent in the
[mosaico-demonstrator](https://gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-demonstrator):

1. Copy `docker-compose.pr-agent.yml` into the demonstrator's `compose/` directory, next to
   `base-definitions.yml` (the overlay's `extends:` references resolve relative to that
   directory).
2. Copy `pr-agent-solution-agent.json` into the demonstrator's
   `docker/agent-registrations/` directory.
3. Append the "demonstrator overlay" block from `pr-agent.env.example` to the demonstrator's
   `env/llm.env` and fill in `PR_AGENT_MODEL` (`PR_AGENT_HOST` may stay empty to use the
   demonstrator's auto-detected LAN IP; `PR_AGENT_PORT` defaults to `23000`).
4. Add `-f compose/docker-compose.pr-agent.yml` to the demonstrator's `01-compose.sh`, next to
   the other task-agent overlays.
5. Run `./01-compose.sh up -d`.

The registration template carries only `description`, `role`, `objective`, `version`; the
demonstrator's `register-agent.py` injects `name`, `a2aAgentCardUrl`, and
`deployment.mode = ENDPOINT` at registration time. Two names are intentionally different and
should not be "fixed": the repository entry is `pr-agent-solution-agent` (what
`register-agent.py` looks the agent up by), while the card's own `name` is
`"PR-Agent Solution Agent"` (a display string).

### Verify

```bash
./smoke_test.sh
```

in the bundle directory gives one of two outcomes:

- **`SMOKE PASSED`** — no LLM credentials were available; the script pulled the pinned image,
  booted it, and validated the agent card only.
- **`FULL ROUND-TRIP PASSED`** — credentials were present (via a `.env` file beside the script,
  copied from `pr-agent.env.example`); the script additionally exercised `GET /health` and an
  A2A `SendMessage` review over an inline diff.

### Troubleshooting

- **The container stays `unhealthy` and registration never runs.** `/health` is a live LLM
  probe and returns `503` on bad or missing credentials — this is intended. Check
  `API_BASE` / `API_KEY` / `MODEL_NAME`, not the compose file.
- **The agent registers but the reference agent never reaches it.** The advertised card URL is
  `localhost`; see the `AGENT_CARD_HOST` / `AGENT_CARD_PORT` warning above.
- **The registration container itself cannot fetch the agent card.** `01-compose.sh` falls back
  to `get_fallback_ip`, which can resolve to `localhost` — reachable from the host but not from
  inside the registration container on the Docker network. Set `PR_AGENT_HOST` explicitly to an
  address reachable from inside Docker (for example the host's LAN IP, or `host.docker.internal`).

### Keep reading

The [bundle README](https://github.com/the-pr-agent/pr-agent/blob/main/docker/mosaico/README.md)
is the canonical deep dive for this surface and the source of the summary above; it covers the
upgrade procedure, the registration flow, and the full env-var contract in one place.
