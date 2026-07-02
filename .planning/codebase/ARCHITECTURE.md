<!-- refreshed: 2026-07-02 -->
# Architecture

**Analysis Date:** 2026-07-02

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                    Entry Points / Servers                     │
├──────────────┬───────────────┬───────────────┬──────────────┤
│  CLI         │  GitHub App   │  GitLab       │  Bitbucket   │
│ `cli.py`     │ `servers/     │  Webhook      │  App         │
│              │  github_app`  │  `servers/    │  `servers/   │
│              │               │  gitlab_..`   │  bitbucket_` │
└──────┬───────┴───────┬───────┴───────┬───────┴──────┬───────┘
       │               │               │              │
       ▼               ▼               ▼              ▼
┌─────────────────────────────────────────────────────────────┐
│                      PRAgent Dispatcher                       │
│         `pr_agent/agent/pr_agent.py`                          │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                         Tools Layer                           │
│  `pr_agent/tools/pr_reviewer.py`                             │
│  `pr_agent/tools/pr_description.py`                          │
│  `pr_agent/tools/pr_code_suggestions.py`                     │
│  `pr_agent/tools/pr_questions.py`  ...                       │
└────────┬────────────────────┬───────────────────────────────┘
         │                    │
         ▼                    ▼
┌────────────────────┐  ┌────────────────────────────────────┐
│  Algo Layer        │  │  Git Providers (Abstraction)        │
│ `pr_agent/algo/`   │  │  `pr_agent/git_providers/`          │
│  - pr_processing   │  │  - github_provider.py               │
│  - token_handler   │  │  - gitlab_provider.py               │
│  - git_patch_proc  │  │  - bitbucket_provider.py            │
└────────┬───────────┘  └─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│                    AI Handlers                                │
│  `pr_agent/algo/ai_handlers/litellm_ai_handler.py`           │
│  Uses LiteLLM to route to OpenAI, Anthropic, AWS Bedrock..  │
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| PRAgent | Command dispatcher — maps slash commands to tool classes | `pr_agent/agent/pr_agent.py` |
| PRReviewer | AI-powered code review with structured feedback | `pr_agent/tools/pr_reviewer.py` |
| PRDescription | Auto-generate/update PR title and description | `pr_agent/tools/pr_description.py` |
| PRCodeSuggestions | Generate inline code improvement suggestions | `pr_agent/tools/pr_code_suggestions.py` |
| PRQuestions | Answer user questions about PR context | `pr_agent/tools/pr_questions.py` |
| GitProvider (ABC) | Abstract base for all git hosting integrations | `pr_agent/git_providers/git_provider.py` |
| LiteLLMAIHandler | LLM abstraction via litellm (OpenAI, Claude, Bedrock) | `pr_agent/algo/ai_handlers/litellm_ai_handler.py` |
| TokenHandler | Token budget management and encoding | `pr_agent/algo/token_handler.py` |
| ConfigLoader | Dynaconf-based hierarchical config (TOML + env + context) | `pr_agent/config_loader.py` |

## Pattern Overview

**Overall:** Command Pattern + Strategy Pattern + Provider Abstraction

**Key Characteristics:**
- Each user command (review, describe, improve) maps to a dedicated Tool class via `command2class` dict
- Git hosting platforms are abstracted behind a `GitProvider` ABC, selected at runtime by config
- AI model access is abstracted behind `BaseAiHandler` ABC, with `LiteLLMAIHandler` as default implementation
- Configuration is hierarchical: defaults (TOML) → repo settings → env vars → per-request context
- Async throughout — tools use `await` for AI calls and git API interactions

## Layers

**Entry Points / Servers:**
- Purpose: Receive external triggers (webhooks, CLI args, polling) and dispatch to PRAgent
- Location: `pr_agent/servers/`, `pr_agent/cli.py`, `pr_agent/cli_pip.py`
- Contains: FastAPI apps, webhook handlers, GitHub Action runner, polling loops
- Depends on: PRAgent dispatcher, config_loader
- Used by: External systems (GitHub webhooks, CLI users, CI pipelines)

**Agent / Dispatcher:**
- Purpose: Parse command string, validate args, route to correct Tool class
- Location: `pr_agent/agent/pr_agent.py`
- Contains: `command2class` mapping, request handling logic, language settings
- Depends on: Tools layer, config_loader, git_providers.utils
- Used by: All entry points

**Tools:**
- Purpose: Implement each PR analysis feature end-to-end
- Location: `pr_agent/tools/`
- Contains: One class per command (PRReviewer, PRDescription, PRCodeSuggestions, etc.)
- Depends on: algo layer (pr_processing, token_handler), git_providers, ai_handlers
- Used by: PRAgent dispatcher

**Algo (Core Processing):**
- Purpose: Shared algorithms for diff processing, token management, prompt construction
- Location: `pr_agent/algo/`
- Contains: Patch processing, file filtering, language detection, token counting, model utilities
- Depends on: config_loader, ai_handlers
- Used by: Tools layer

**AI Handlers:**
- Purpose: Abstract LLM API calls with retry, streaming, credential management
- Location: `pr_agent/algo/ai_handlers/`
- Contains: BaseAiHandler ABC, LiteLLMAIHandler, LangChain handler, OpenAI handler
- Depends on: litellm, config_loader
- Used by: Algo layer, Tools layer

**Git Providers:**
- Purpose: Abstract git hosting API operations (get diff, publish comments, etc.)
- Location: `pr_agent/git_providers/`
- Contains: Provider implementations for GitHub, GitLab, Bitbucket, Azure DevOps, Gerrit, Gitea, CodeCommit, Local
- Depends on: config_loader, hosting platform SDKs
- Used by: Tools layer

## Data Flow

### Primary Request Path (Webhook → AI Review → Comment)

1. Webhook received by server (e.g., `servers/github_app.py:handle_github_webhooks`)
2. Request body parsed, context initialized with per-request settings copy
3. `PRAgent().handle_request(pr_url, [command])` dispatched (`agent/pr_agent.py:55`)
4. Repo settings applied via `apply_repo_settings(pr_url)` (`git_providers/utils.py`)
5. Tool class instantiated (e.g., `PRReviewer(pr_url, ai_handler=...)`)
6. Tool calls `get_pr_diff()` → GitProvider fetches diff from hosting API (`algo/pr_processing.py:38`)
7. Diff processed: filtered, extended, token-budgeted (`algo/pr_processing.py`)
8. Jinja2 prompt rendered with diff + settings → sent to AI via `ai_handler.chat_completion()`
9. AI response parsed (YAML), formatted as markdown
10. Result published back via `git_provider.publish_comment()` or similar

### CLI Path

1. `cli.py:run()` parses args, sets CLI_MODE
2. `PRAgent().handle_request(pr_url, [command])` called via `asyncio.run()`
3. Same flow as webhook from step 3 onward

**State Management:**
- Per-request state via `starlette_context` (server mode) or global `Dynaconf` singleton (CLI mode)
- `get_settings()` checks context first, falls back to `global_settings`
- No persistent database — stateless request processing

## Key Abstractions

**GitProvider (ABC):**
- Purpose: Uniform interface to any git hosting platform
- Examples: `pr_agent/git_providers/github_provider.py`, `pr_agent/git_providers/gitlab_provider.py`
- Pattern: Strategy pattern, selected by `config.git_provider` setting
- Key methods: `get_diff_files()`, `publish_comment()`, `publish_description()`, `get_files()`

**BaseAiHandler (ABC):**
- Purpose: Uniform interface to any LLM backend
- Examples: `pr_agent/algo/ai_handlers/litellm_ai_handler.py`
- Pattern: Strategy pattern, injected via constructor `ai_handler` parameter
- Key method: `chat_completion(model, system, user, temperature)`

**TokenHandler:**
- Purpose: Manage token budgets when constructing prompts from diffs
- Examples: `pr_agent/algo/token_handler.py`
- Pattern: Budget allocation — fits as much diff content as token limits allow

**FilePatchInfo:**
- Purpose: Structured representation of a file's diff/patch data
- Defined in: `pr_agent/algo/types.py`
- Pattern: Data class carrying filename, patch content, edit type, language

## Entry Points

**CLI:**
- Location: `pr_agent/cli.py`
- Triggers: `pr-agent` console script (pyproject.toml `[project.scripts]`)
- Responsibilities: Parse args, configure settings, invoke PRAgent

**GitHub App (FastAPI):**
- Location: `pr_agent/servers/github_app.py`
- Triggers: GitHub webhook POST to `/api/v1/github_webhooks`
- Responsibilities: Signature verification, event routing, background task dispatch

**GitHub Action:**
- Location: `pr_agent/servers/github_action_runner.py`
- Triggers: GitHub Actions workflow events
- Responsibilities: Run commands in CI context

**GitLab Webhook:**
- Location: `pr_agent/servers/gitlab_webhook.py`
- Triggers: GitLab MR webhook events
- Responsibilities: Parse GitLab events, dispatch to PRAgent

**Bitbucket App:**
- Location: `pr_agent/servers/bitbucket_app.py`
- Triggers: Bitbucket webhook events
- Responsibilities: Bitbucket-specific event handling

**Gerrit Server:**
- Location: `pr_agent/servers/gerrit_server.py`
- Triggers: Gerrit change events
- Responsibilities: Gerrit-specific integration

**Gitea App:**
- Location: `pr_agent/servers/gitea_app.py`
- Triggers: Gitea webhook events
- Responsibilities: Gitea-specific integration

## Architectural Constraints

- **Threading:** Async/await (asyncio) throughout. Server entry points use FastAPI with background tasks. CLI uses `asyncio.run()`.
- **Global state:** `global_settings` Dynaconf singleton in `config_loader.py`. Per-request isolation via `starlette_context` in server mode.
- **Circular imports:** Managed via lazy imports in `config_loader.py` (secret_providers, log module).
- **Token limits:** All diff processing is token-budget-aware. Large PRs are clipped or chunked per `large_patch_policy` setting.
- **Stateless processing:** No database. Each request is self-contained. No cross-request state persistence.

## Anti-Patterns

### Copying global_settings for request isolation

**What happens:** Server handlers do `context["settings"] = copy.deepcopy(global_settings)` on every request (`servers/github_app.py:51`).
**Why it's wrong:** Deep-copying a large Dynaconf object on every request adds latency and memory pressure.
**Do this instead:** Use Dynaconf's layered override mechanism or a lightweight per-request overlay rather than full deep copy.

### CLI singleton leakage

**What happens:** `get_settings().set(...)` mutates the global singleton in CLI mode (`cli.py:91-103`), with explicit comments about preventing leakage between `run()` calls.
**Why it's wrong:** Indicates the settings architecture doesn't cleanly support multiple sequential commands in one process.
**Do this instead:** Wrap CLI invocations in a scoped settings context rather than manual cleanup of global state.

## Error Handling

**Strategy:** Exception-based with top-level catch-all in `PRAgent.handle_request()`

**Patterns:**
- Tools raise exceptions on failure; `PRAgent.handle_request()` catches all with `get_logger().exception()` and returns `False`
- AI handler uses `tenacity` retry with configurable attempts (`MODEL_RETRIES = 2`)
- Rate limit exceptions (`RateLimitExceededException`) are explicitly caught and re-raised in `pr_processing.py`
- Fallback models: `retry_with_fallback_models()` tries primary model, then falls back to `fallback_models` list

## Cross-Cutting Concerns

**Logging:** Loguru-based via `pr_agent/log/`. Supports JSON format for server mode. Contextual logging with `get_logger().contextualize()`.

**Configuration:** Dynaconf with TOML files, env var override, per-repo `.pr_agent.toml`, wiki settings, and AWS Secrets Manager integration.

**Authentication:** Per-provider (GitHub App JWT, GitLab tokens, Bitbucket OAuth). Identity provider abstraction in `pr_agent/identity_providers/`.

**Secrets:** Pluggable secret providers: Google Cloud Storage, AWS Secrets Manager (`pr_agent/secret_providers/`).

---

*Architecture analysis: 2026-07-02*
