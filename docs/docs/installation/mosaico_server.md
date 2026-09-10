## MOSAICO A2A server

PR-Agent can run as an [A2A](https://a2a-protocol.org/) 1.0 *solution agent* for the
[MOSAICO](https://mosaico-project.eu/) ecosystem: a small Starlette server that exposes the
standard A2A surface (agent card + JSON-RPC) plus a health probe. It is **not** a fork or a
separate project — the server is PR-Agent code under [`pr_agent/mosaico/`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/mosaico/server.py),
ships in every release wheel, and ships as its own Docker image (`<version>-mosaico_agent`)
starting at `v0.36.0`. The server is unbiased about the git provider: every request carries
either a PR URL or a raw diff, and the agent answers from that input alone.

### What the mode is

The A2A server exposes three endpoints:

| Path | Method | Purpose |
| --- | --- | --- |
| `/.well-known/agent-card.json` | GET | The A2A agent card |
| `/` | POST | A2A 1.0 JSON-RPC. Sends the `SendMessage` method with an `A2A-Version: 1.0` header (the header is required: the server treats a request without it as protocol 0.3 and rejects it). The reply arrives as a task artifact (`result.task.artifacts[].parts[].text`), not as a status message. |
| `/health` | GET | A **live LLM connectivity probe** — `200` when an LLM round-trip succeeds, `503` otherwise |

The advertised agent card carries the skills `review`, `improve`, `describe`, and `ask`, the
name `"PR-Agent Solution Agent"`, a `version` derived from the running build (never
hand-maintained), and the required
`https://mosaico-project.eu/extensions/mosaico-observability` extension. Streaming is
advertised as disabled, which is load-bearing: the reference agent selects
`message/send` vs `message/stream` from that capability.

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

!!! warning "AGENT_CARD_HOST / AGENT_CARD_PORT — the one thing to get right"

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
