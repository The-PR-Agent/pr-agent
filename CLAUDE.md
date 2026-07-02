<!-- GSD:project-start source:PROJECT.md -->

## Project

**PR-Agent — Org MR Enhancements**

A fork of PR-Agent (Qodo Merge) — an AI tool that auto-reviews, describes, and improves merge/pull requests. This milestone enhances the `describe` command for GitLab MRs: rewriting MR titles to follow the Angular Commit Convention and prepending the organization's legacy MR description template with AI-filled sections, while preserving PR-Agent's existing generated walkthrough.

**Core Value:** When a GitLab MR opens, the `describe` command produces a conventionally-formatted title and an org-standard description body (What/Risk filled by AI, checklist for the human) on top of the existing PR-Agent walkthrough — with zero manual formatting by the author.

### Constraints

- **Tech stack**: Python 3.12+, litellm, Jinja2, dynaconf — must match existing patterns (snake_case, Ruff 120-char, loguru logging, graceful fallback on error)
- **Platform**: GitLab MRs only for v1
- **Compatibility**: New behavior must be config-gated so default PR-Agent behavior is unchanged when toggles are off
- **Architecture**: Stay within the existing Tool + prompt-template pattern; avoid introducing new abstractions unless the diff requires it

<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->

## Technology Stack

## Languages

- Python 3.12+ - Entire application (`pr_agent/`, `tests/`, `scripts/`)
- TOML - Configuration and prompt templates (`pr_agent/settings/`)
- YAML - GitHub Actions, Docker Compose, CI/CD (`action.yaml`, `codecov.yml`)
- Dockerfile - Container builds (`docker/Dockerfile`, `Dockerfile.github_action`, `Dockerfile.github_action_dockerhub`)

## Runtime

- Python 3.12+ (CPython, `python:3.12.13-slim` Docker base image)
- Async event loop (aiohttp, asyncio throughout)
- pip + setuptools (build backend)
- Lockfile: None (pinned versions in `requirements.txt`)

## Frameworks

- FastAPI 0.118.0 - HTTP webhook servers (`pr_agent/servers/`)
- Starlette (via FastAPI) - Middleware, context management
- Gunicorn 23.0.0 + Uvicorn 0.22.0 - Production ASGI serving
- pytest 9.0.2 - Test runner (`pyproject.toml` `[tool.pytest.ini_options]`)
- pytest-asyncio >=1.3.0 - Async test support (asyncio_mode = "auto")
- pytest-cov 7.0.0 - Coverage reporting
- setuptools >=61.0 - Build system (`pyproject.toml`)
- Ruff - Linting and import sorting (`pyproject.toml` `[tool.ruff]`)
- Bandit - Security linting (`pyproject.toml` `[tool.bandit]`)
- pre-commit >=4,<5 - Git hooks (`requirements-dev.txt`)
- flake8 7.3.0 - Additional linting (`requirements-dev.txt`)

## Key Dependencies

- litellm 1.84.0 - Unified LLM API gateway (`pr_agent/algo/ai_handlers/litellm_ai_handler.py`)
- openai >=1.55.3 - OpenAI API client
- anthropic >=0.69.0 - Anthropic/Claude API client
- tiktoken 0.12.0 - Token counting
- google-cloud-aiplatform 1.154.0 - Vertex AI integration
- aiohttp 3.13.4 - Async HTTP client
- boto3 1.40.45 - AWS SDK (Bedrock, Secrets Manager, CodeCommit)
- PyGithub 1.59.* - GitHub API client (`pr_agent/git_providers/github_provider.py`)
- python-gitlab 8.3.0 - GitLab API client (`pr_agent/git_providers/gitlab_provider.py`)
- azure-devops 7.1.0b4 - Azure DevOps API client
- atlassian-python-api 3.41.4 - Bitbucket/Jira integration
- giteapy 1.0.8 - Gitea API client
- GitPython 3.1.41 - Local git operations
- dynaconf 3.2.4 - Configuration management (`pr_agent/config_loader.py`)
- Jinja2 3.1.6 - Prompt templating
- pydantic 2.13.3 - Data validation
- loguru 0.7.2 - Structured logging (`pr_agent/log/`)
- PyJWT 2.10.1 - JWT token handling (GitHub App auth)
- tenacity 8.2.3 - Retry logic
- retry 0.9.2 - Additional retry support
- ujson 5.8.0 - Fast JSON parsing
- starlette-context 0.3.6 - Request-scoped context
- langfuse 3.14.5 - LLM observability (`pr_agent/mosaico/observability.py`)
- a2a-sdk[http-server] 1.0.3 - Agent-to-agent protocol (`pr_agent/mosaico/`)
- html2text 2024.2.26 - HTML to markdown conversion
- pinecone-client - Vector DB for similar issues
- lancedb - Embedded vector DB
- qdrant-client - Vector DB
- langchain/langchain-openai - LangChain integration

## Configuration

- Dynaconf-based with TOML settings files (`pr_agent/settings/configuration.toml`)
- Supports `.secrets.toml` for local secrets (gitignored)
- Environment variables override settings (dynaconf env_loader)
- Per-repo settings via `.pr_agent.toml` in target repository
- Wiki settings file support
- `pyproject.toml` `[tool.pr-agent]` section support
- `pr_agent/settings/configuration.toml` - Main config (models, features, provider settings)
- `pr_agent/settings/ignore.toml` - File ignore patterns
- `pr_agent/settings/language_extensions.toml` - Language detection
- `pr_agent/settings/*.toml` - Prompt templates per tool
- `pyproject.toml` - Package metadata, build config, tool settings
- `requirements.txt` - Pinned production dependencies
- `requirements-dev.txt` - Development dependencies

## Platform Requirements

- Python >=3.12
- Git (for GitPython operations)
- pip for dependency installation
- Docker (multi-stage builds in `docker/Dockerfile`)
- AWS Lambda via Mangum adapter (`docker/Dockerfile.lambda`, `pr_agent/servers/github_lambda_webhook.py`)
- GitHub Actions runner (`Dockerfile.github_action`)
- Any ASGI-compatible host (Gunicorn + Uvicorn)

<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

## Naming Patterns

- snake_case for all Python modules: `pr_processing.py`, `token_handler.py`, `litellm_ai_handler.py`
- Test files prefixed with `test_`: `test_clip_tokens.py`, `test_pr_description.py`
- Private/helper modules prefixed with underscore: `_settings_helpers.py`
- snake_case throughout: `get_pr_diff()`, `clip_tokens()`, `retry_with_fallback_models()`
- Private methods prefixed with underscore: `_prepare_data()`, `_parse_pr_url()`
- Factory/builder helpers prefixed with `_make_`: `_make_instance()`, `_make_reviewer()`
- snake_case for locals and instance attributes: `self.git_provider`, `self.main_pr_language`
- UPPER_SNAKE_CASE for module-level constants: `MAX_FILES_ALLOWED_FULL`, `OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD`, `MODEL_RETRIES`
- Constants defined at module top, after imports
- PascalCase for classes: `PRDescription`, `LiteLLMAIHandler`, `GitProvider`
- PascalCase for Enums: `ModelType`, `ReasoningEffort`, `PRReviewHeader`
- Enums inherit from `(str, Enum)` for string-serializable constants
- Pydantic `BaseModel` for structured data: `Range` in `pr_agent/algo/utils.py`
- `TypedDict` for dictionary shapes: `TodoItem` in `pr_agent/algo/utils.py`

## Code Style

- Ruff (configured in `pyproject.toml`)
- Line length: 120 characters
- No explicit formatter (black/autopep8) configured; Ruff handles style
- Ruff with rules: E (pycodestyle), F (pyflakes), B (flake8-bugbear), I001/I002 (isort)
- `# noqa: E501` used inline for intentionally long lines (test assertions)
- Bandit for security scanning (configured in `pyproject.toml`, skips B101 assert)
- `lint.exclude = ["api/code_completions"]`

## Import Organization

- Explicit imports preferred over star imports
- Multi-symbol imports wrapped in parentheses across lines:
- isort auto-fixed via Ruff (I001 rule)
- None. All imports use full dotted paths from `pr_agent` root package.

## Error Handling

- Try/except with logging and graceful fallback (never crash silently):
- Specific exceptions caught where possible: `RateLimitExceededException`, `ClientError`
- Re-raise after logging when the caller needs to know:
- Functions return empty string `""`, empty list `[]`, or original input on failure (never `None` unless explicitly documented)
- `ValueError` raised for invalid input in parsers (e.g., `_parse_pr_url`)

## Logging

- `debug` - Internal flow tracing
- `info` - Normal operations, configuration choices
- `warning` - Recoverable issues, fallbacks, misconfigurations
- `error` - Failures that affect output
- `exception` - Failures with full traceback

## Comments

- Module-level docstrings for complex classes (especially `__init__` methods)
- Inline comments for non-obvious logic or configuration choices
- `# noqa` annotations for intentional lint suppressions with rule code
- Triple-quoted, Google-style for public APIs:
- Not universally applied; many internal functions lack docstrings

## Function Design

- Type hints on parameters and return types for public APIs
- Optional params use `= None` default, not `Optional[X]` alone
- `partial` used for deferred handler construction: `ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler`
- Single return type preferred
- Tuples for multi-value returns: `Tuple[str, int]`
- Empty string or empty collection as "no result" (not None, unless input was None)

## Module Design

- No `__all__` defined in most modules
- `__init__.py` files used for re-exports from subpackages (e.g., `pr_agent/git_providers/__init__.py`)
- `ABC` + `@abstractmethod` for provider interfaces: `GitProvider`, `BaseAiHandler`
- Concrete implementations in separate files per provider
- Dynaconf singleton accessed via `get_settings()` from `pr_agent/config_loader.py`
- Request-scoped settings via starlette context (deep-copied per request)
- TOML files in `pr_agent/settings/` for defaults
- Settings accessed as dotted attributes: `get_settings().pr_description.enable_semantic_files_types`

## Async Patterns

- `async def` for AI handler methods and webhook handlers
- `asyncio.run()` used at entry points (CLI)
- `acompletion` from litellm for async LLM calls
- `tenacity` retry decorators for resilient API calls

## Design Patterns

- Abstract base (`GitProvider`, `BaseAiHandler`) with multiple concrete implementations
- Factory functions: `get_git_provider()`, `get_git_provider_with_context()`
- Dynaconf global singleton with per-request context override
- TOML-based defaults with environment variable overrides
- Jinja2 with `StrictUndefined` for prompt templates
- Variables dict pattern: `self.vars = {"title": ..., "diff": ...}`

<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

## System Overview

```text

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

- Each user command (review, describe, improve) maps to a dedicated Tool class via `command2class` dict
- Git hosting platforms are abstracted behind a `GitProvider` ABC, selected at runtime by config
- AI model access is abstracted behind `BaseAiHandler` ABC, with `LiteLLMAIHandler` as default implementation
- Configuration is hierarchical: defaults (TOML) → repo settings → env vars → per-request context
- Async throughout — tools use `await` for AI calls and git API interactions

## Layers

- Purpose: Receive external triggers (webhooks, CLI args, polling) and dispatch to PRAgent
- Location: `pr_agent/servers/`, `pr_agent/cli.py`, `pr_agent/cli_pip.py`
- Contains: FastAPI apps, webhook handlers, GitHub Action runner, polling loops
- Depends on: PRAgent dispatcher, config_loader
- Used by: External systems (GitHub webhooks, CLI users, CI pipelines)
- Purpose: Parse command string, validate args, route to correct Tool class
- Location: `pr_agent/agent/pr_agent.py`
- Contains: `command2class` mapping, request handling logic, language settings
- Depends on: Tools layer, config_loader, git_providers.utils
- Used by: All entry points
- Purpose: Implement each PR analysis feature end-to-end
- Location: `pr_agent/tools/`
- Contains: One class per command (PRReviewer, PRDescription, PRCodeSuggestions, etc.)
- Depends on: algo layer (pr_processing, token_handler), git_providers, ai_handlers
- Used by: PRAgent dispatcher
- Purpose: Shared algorithms for diff processing, token management, prompt construction
- Location: `pr_agent/algo/`
- Contains: Patch processing, file filtering, language detection, token counting, model utilities
- Depends on: config_loader, ai_handlers
- Used by: Tools layer
- Purpose: Abstract LLM API calls with retry, streaming, credential management
- Location: `pr_agent/algo/ai_handlers/`
- Contains: BaseAiHandler ABC, LiteLLMAIHandler, LangChain handler, OpenAI handler
- Depends on: litellm, config_loader
- Used by: Algo layer, Tools layer
- Purpose: Abstract git hosting API operations (get diff, publish comments, etc.)
- Location: `pr_agent/git_providers/`
- Contains: Provider implementations for GitHub, GitLab, Bitbucket, Azure DevOps, Gerrit, Gitea, CodeCommit, Local
- Depends on: config_loader, hosting platform SDKs
- Used by: Tools layer

## Data Flow

### Primary Request Path (Webhook → AI Review → Comment)

### CLI Path

- Per-request state via `starlette_context` (server mode) or global `Dynaconf` singleton (CLI mode)
- `get_settings()` checks context first, falls back to `global_settings`
- No persistent database — stateless request processing

## Key Abstractions

- Purpose: Uniform interface to any git hosting platform
- Examples: `pr_agent/git_providers/github_provider.py`, `pr_agent/git_providers/gitlab_provider.py`
- Pattern: Strategy pattern, selected by `config.git_provider` setting
- Key methods: `get_diff_files()`, `publish_comment()`, `publish_description()`, `get_files()`
- Purpose: Uniform interface to any LLM backend
- Examples: `pr_agent/algo/ai_handlers/litellm_ai_handler.py`
- Pattern: Strategy pattern, injected via constructor `ai_handler` parameter
- Key method: `chat_completion(model, system, user, temperature)`
- Purpose: Manage token budgets when constructing prompts from diffs
- Examples: `pr_agent/algo/token_handler.py`
- Pattern: Budget allocation — fits as much diff content as token limits allow
- Purpose: Structured representation of a file's diff/patch data
- Defined in: `pr_agent/algo/types.py`
- Pattern: Data class carrying filename, patch content, edit type, language

## Entry Points

- Location: `pr_agent/cli.py`
- Triggers: `pr-agent` console script (pyproject.toml `[project.scripts]`)
- Responsibilities: Parse args, configure settings, invoke PRAgent
- Location: `pr_agent/servers/github_app.py`
- Triggers: GitHub webhook POST to `/api/v1/github_webhooks`
- Responsibilities: Signature verification, event routing, background task dispatch
- Location: `pr_agent/servers/github_action_runner.py`
- Triggers: GitHub Actions workflow events
- Responsibilities: Run commands in CI context
- Location: `pr_agent/servers/gitlab_webhook.py`
- Triggers: GitLab MR webhook events
- Responsibilities: Parse GitLab events, dispatch to PRAgent
- Location: `pr_agent/servers/bitbucket_app.py`
- Triggers: Bitbucket webhook events
- Responsibilities: Bitbucket-specific event handling
- Location: `pr_agent/servers/gerrit_server.py`
- Triggers: Gerrit change events
- Responsibilities: Gerrit-specific integration
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

### CLI singleton leakage

## Error Handling

- Tools raise exceptions on failure; `PRAgent.handle_request()` catches all with `get_logger().exception()` and returns `False`
- AI handler uses `tenacity` retry with configurable attempts (`MODEL_RETRIES = 2`)
- Rate limit exceptions (`RateLimitExceededException`) are explicitly caught and re-raised in `pr_processing.py`
- Fallback models: `retry_with_fallback_models()` tries primary model, then falls back to `fallback_models` list

## Cross-Cutting Concerns

<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
