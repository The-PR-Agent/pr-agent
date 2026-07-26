# PR-Agent — MOSAICO solution-agent deployment bundle

Deployment assets for running **PR-Agent** as a [MOSAICO](https://mosaico-project.eu/) A2A
*solution agent*. This directory contains no Python and no pr-agent source — it consumes
PR-Agent as a published, version-pinned Docker image. The agent's source lives upstream in
[`qodo-ai/pr-agent`](https://github.com/qodo-ai/pr-agent), under `pr_agent/mosaico/`; it is
merged into `main` and ships in every release wheel and image starting at `v0.37.0`.

## Relationship to upstream

This is not a fork and it never becomes one:

- The MOSAICO A2A server is upstream code, released in tags `v0.37.0`–`v0.41.0`. This bundle
  holds zero Python — only a compose overlay, a registration template, an env template, a
  smoke test, and this README.
- **Staying current is one line**: bump the pinned tag in `docker-compose.pr-agent.yml`, then
  re-run `./smoke_test.sh` to confirm the new image still boots and serves a valid card. That
  is the entire upgrade procedure:
  ```diff
  -    image: pragent/pr-agent:0.41.0-mosaico_agent
  +    image: pragent/pr-agent:0.42.0-mosaico_agent
  ```
- Upstream publishes `pragent/pr-agent:<version>-mosaico_agent` for every release, from the
  same CI matrix that builds its other images — the MOSAICO target cannot silently stop being
  built without the whole release failing.
- Canonical source of every file in this bundle is `docker/mosaico/` in
  `github.com/qodo-ai/pr-agent`. Edit there, copy here. Never edit here directly.

## Quick start (standalone, no demonstrator)

Boots from a bare `docker pull` in a couple of seconds — no repo clone, no build:

```bash
docker pull pragent/pr-agent:0.41.0-mosaico_agent
docker run -d --name pr-agent-mosaico -p 9000:9000 \
  -e API_BASE=https://your-openai-compatible-endpoint/v1 \
  -e API_KEY=sk-... \
  -e MODEL_NAME=openai/your-model-slug \
  pragent/pr-agent:0.41.0-mosaico_agent

curl -s http://localhost:9000/.well-known/agent-card.json | python3 -m json.tool
```

Endpoints (port `9000`):
- `GET /.well-known/agent-card.json` — the A2A agent card
- `POST /` — A2A 1.0 JSON-RPC. Send the `SendMessage` method with an
  `A2A-Version: 1.0` header; the header is required, as the server treats a request
  without it as protocol 0.3 and rejects it. The reply comes back as a task artifact
  (`result.task.artifacts[].parts[].text`), not as a status message.
- `GET /health` — a **live LLM connectivity probe** (200 healthy / 503 unhealthy)

Env-var contract (MOSAICO agent requirements are defined in the demonstrator's
[`docs/agent-requirements.md`](https://gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-demonstrator/-/blob/main/docs/agent-requirements.md)):
- `API_BASE`, `API_KEY`, `MODEL_NAME` — the LLM connection
- `HOST` (default `0.0.0.0`), `PORT` (default `9000`) — bind address
- `AGENT_CARD_HOST`, `AGENT_CARD_PORT` — see below; unset by default
- `MODEL_MAX_TOKENS` (default `32000`) — token budget for models whose context size
  pr-agent does not already know
- `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — optional observability

Expect a card whose top level carries `name: "PR-Agent Solution Agent"` and `version` equal to
the image tag's version (it is derived from the running build, never hand-maintained), with
skills `review`, `improve`, `describe`, `ask`, and the required
`https://mosaico-project.eu/extensions/mosaico-observability` extension.

### `AGENT_CARD_HOST` / `AGENT_CARD_PORT` — the one thing to get right

These two variables set the URL the agent advertises in `supportedInterfaces`. Leave them
unset and the card advertises `http://localhost:9000/`, which is reachable only from inside
the container itself. The failure this causes is **silent and late**: registration with
MOSAICO succeeds, the repository stores the unreachable URL, and the reference agent only
fails to dereference it once it tries to route a task to this agent.

In the demonstrator overlay below, these are already wired correctly:

```yaml
AGENT_CARD_HOST: ${PR_AGENT_HOST:-${DEFAULT_TASK_AGENT_HOST}}
AGENT_CARD_PORT: ${PR_AGENT_PORT:-23000}
```

Standalone, set them explicitly to whatever host/port the *caller* will use to reach the
container. Verify with:

```bash
curl -s http://<host>:<port>/.well-known/agent-card.json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['supportedInterfaces'][0]['url'])"
```

If that prints a `localhost` URL, the deployment is wrong.

## Deploy into the mosaico-demonstrator

1. Copy `docker-compose.pr-agent.yml` into the demonstrator's `compose/` directory, next to
   `base-definitions.yml` — the overlay's `extends:` references resolve relative to that
   directory.
2. Copy `pr-agent-solution-agent.json` into the demonstrator's
   `docker/agent-registrations/` directory.
3. Append the "demonstrator overlay" block from `pr-agent.env.example` to the demonstrator's
   `env/llm.env` and fill in `PR_AGENT_MODEL` (`PR_AGENT_HOST` may stay empty to use the
   demonstrator's auto-detected LAN IP; `PR_AGENT_PORT` defaults to `23000`).
4. Add `-f compose/docker-compose.pr-agent.yml` to the demonstrator's `01-compose.sh`, next to
   the other task-agent overlays.
5. `./01-compose.sh up -d`.

## Registration

The demonstrator's `register-agent.py` reads `pr-agent-solution-agent.json` and injects, at
registration time: `name` (from the overlay's `AGENT_NAME`), `a2aAgentCardUrl` (from
`AGENT_CARD_URL`), and `deployment.mode = ENDPOINT`. That is why the template carries only
four fields: `description`, `role`, `objective`, `version`.

Two names are intentionally different, so don't "fix" the mismatch:
- The MOSAICO repository entry's `name` is `pr-agent-solution-agent` (kebab-case, what
  `register-agent.py` looks the agent up by).
- The A2A card's own `name` field is `"PR-Agent Solution Agent"` (a display string, asserted
  by `smoke_test.sh`).

## Verify

```bash
./smoke_test.sh
```

Two outcomes:
- **`SMOKE PASSED`** — no LLM creds available; the script pulled the pinned image, booted it,
  and validated the agent card only.
- **`FULL ROUND-TRIP PASSED`** — LLM creds were present (via a `.env` file beside the script,
  copied from `pr-agent.env.example`); the script additionally exercised `GET /health` and an A2A
  `SendMessage` review over an inline diff.

## Troubleshooting

- **Container stays `unhealthy`, registration never runs.** `/health` is a live LLM probe and
  returns `503` on bad/missing credentials — this is intended (the healthcheck matches the
  peer solution agents' probe verbatim, and a registered card backed by a dead LLM is worse
  than no registration). Check `API_BASE`/`API_KEY`/`MODEL_NAME`, not the compose file.
- **Agent registers but the reference agent never reaches it.** The advertised card URL is
  `localhost`; see the `AGENT_CARD_HOST`/`AGENT_CARD_PORT` section above.
- **The registration container itself can't fetch the agent card.** `01-compose.sh` falls back
  to `get_fallback_ip`, which can resolve to `localhost` — reachable from the host, but not
  from inside the `pr-agent-solution-agent-registration` container on the Docker network. If
  registration fails to fetch `AGENT_CARD_URL`, set `PR_AGENT_HOST` explicitly to an address
  reachable from inside Docker (e.g. the host's LAN IP, or `host.docker.internal`). Every peer
  task agent shares this same exposure; it is not specific to PR-Agent.

## License

MIT — see the bundled [`LICENSE`](./LICENSE). Upstream `qodo-ai/pr-agent` is MIT-licensed too.
