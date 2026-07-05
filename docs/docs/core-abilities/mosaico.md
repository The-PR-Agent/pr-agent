# MOSAICO / A2A Integration

`pr_agent/mosaico/` exposes PR-Agent as an [A2A 1.0](https://a2a-protocol.org/) "Solution Agent" for the MOSAICO platform. It wraps the regular tools (`/review`, `/improve`, `/describe`, `/ask`) behind an agent-to-agent JSON-RPC server so other agents can send it work — either a PR URL or a pasted unified diff — without any git-host credentials.

## Running the server

```bash
python -m pr_agent.mosaico.server
```

This starts a Starlette app (default port `9000`) with three surfaces:

- the A2A agent-card routes (advertising the agent's skills and the MOSAICO observability extension),
- the A2A JSON-RPC endpoint that accepts `message/send` tasks,
- `GET /health`, which issues a single non-retried LLM probe and returns 200/503.

## How a request flows

1. `PRAgentExecutor.execute` (`executor.py`) installs a request-scoped deep copy of the global settings into `starlette_context`, so concurrent tasks never mutate each other's configuration.
2. `route_and_run` (`dispatch.py`) turns the inbound text into a PR-Agent command via three paths:
    - **PR URL** — the public unified diff is fetched by appending `.diff` (with SSRF guards: scheme/host validation and private-address rejection), then routed through the token-free `mosaico_diff` provider.
    - **Pasted unified diff** — stored on the context settings (`MOSAICO.INPUT`) and executed against `DiffInputProvider`.
    - **Free text with neither** — returns honest guidance instead of guessing.
3. The rendered markdown is published as an A2A task artifact and the task is completed (or failed with an error artifact — the reference client reads `task.artifacts`, not the completion message).

`route_and_run` never raises; failures degrade to an explanatory fallback string.

## The `mosaico_diff` git provider

`diff_provider.py` implements `DiffInputProvider`, a `GitProvider` that feeds a supplied unified diff to the tools with no host and no checkout. Input methods (files, languages, diff) are real; publish/label/comment methods are safe no-op stubs — with `publish_output=false` the tools render into `get_settings().data` instead of publishing. `provider_registration.py` registers it under the `mosaico_diff` key via `setdefault`, and is only imported by the MOSAICO server, so the provider registry is untouched on every other code path.

## Configuration and observability

- `env_bridge.py` maps MOSAICO's environment-variable contract (`API_BASE`, `API_KEY`, `MODEL_NAME`, `MODEL_MAX_TOKENS`, `LANGFUSE_*`) onto PR-Agent's Dynaconf settings. Every mapping is a no-op unless the variable is set.
- `observability.py` parses the MOSAICO observability-extension metadata (root/super task IDs), binds them into the loguru context, and opens a Langfuse trace span. Parsing is tolerant: partial or absent metadata degrades gracefully rather than failing the request.
- When Langfuse credentials are present, the server initializes the Langfuse client once at startup with A2A transport spans suppressed.

## Tests

The subsystem is covered by the `tests/unittest/test_mosaico_*` suite (round-trip, card, env bridge, executor, health, isolation, metadata, provider, router, smoke).
