# Technology Stack

**Analysis Date:** 2026-07-02

## Languages

**Primary:**
- Python 3.12+ - Entire application (`pr_agent/`, `tests/`, `scripts/`)

**Secondary:**
- TOML - Configuration and prompt templates (`pr_agent/settings/`)
- YAML - GitHub Actions, Docker Compose, CI/CD (`action.yaml`, `codecov.yml`)
- Dockerfile - Container builds (`docker/Dockerfile`, `Dockerfile.github_action`, `Dockerfile.github_action_dockerhub`)

## Runtime

**Environment:**
- Python 3.12+ (CPython, `python:3.12.13-slim` Docker base image)
- Async event loop (aiohttp, asyncio throughout)

**Package Manager:**
- pip + setuptools (build backend)
- Lockfile: None (pinned versions in `requirements.txt`)

## Frameworks

**Core:**
- FastAPI 0.118.0 - HTTP webhook servers (`pr_agent/servers/`)
- Starlette (via FastAPI) - Middleware, context management
- Gunicorn 23.0.0 + Uvicorn 0.22.0 - Production ASGI serving

**Testing:**
- pytest 9.0.2 - Test runner (`pyproject.toml` `[tool.pytest.ini_options]`)
- pytest-asyncio >=1.3.0 - Async test support (asyncio_mode = "auto")
- pytest-cov 7.0.0 - Coverage reporting

**Build/Dev:**
- setuptools >=61.0 - Build system (`pyproject.toml`)
- Ruff - Linting and import sorting (`pyproject.toml` `[tool.ruff]`)
- Bandit - Security linting (`pyproject.toml` `[tool.bandit]`)
- pre-commit >=4,<5 - Git hooks (`requirements-dev.txt`)
- flake8 7.3.0 - Additional linting (`requirements-dev.txt`)

## Key Dependencies

**Critical (AI/LLM):**
- litellm 1.84.0 - Unified LLM API gateway (`pr_agent/algo/ai_handlers/litellm_ai_handler.py`)
- openai >=1.55.3 - OpenAI API client
- anthropic >=0.69.0 - Anthropic/Claude API client
- tiktoken 0.12.0 - Token counting
- google-cloud-aiplatform 1.154.0 - Vertex AI integration

**Infrastructure:**
- aiohttp 3.13.4 - Async HTTP client
- boto3 1.40.45 - AWS SDK (Bedrock, Secrets Manager, CodeCommit)
- PyGithub 1.59.* - GitHub API client (`pr_agent/git_providers/github_provider.py`)
- python-gitlab 8.3.0 - GitLab API client (`pr_agent/git_providers/gitlab_provider.py`)
- azure-devops 7.1.0b4 - Azure DevOps API client
- atlassian-python-api 3.41.4 - Bitbucket/Jira integration
- giteapy 1.0.8 - Gitea API client
- GitPython 3.1.41 - Local git operations

**Application:**
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

**Optional (commented out in `requirements.txt`):**
- pinecone-client - Vector DB for similar issues
- lancedb - Embedded vector DB
- qdrant-client - Vector DB
- langchain/langchain-openai - LangChain integration

## Configuration

**Environment:**
- Dynaconf-based with TOML settings files (`pr_agent/settings/configuration.toml`)
- Supports `.secrets.toml` for local secrets (gitignored)
- Environment variables override settings (dynaconf env_loader)
- Per-repo settings via `.pr_agent.toml` in target repository
- Wiki settings file support
- `pyproject.toml` `[tool.pr-agent]` section support

**Key Configuration Files:**
- `pr_agent/settings/configuration.toml` - Main config (models, features, provider settings)
- `pr_agent/settings/ignore.toml` - File ignore patterns
- `pr_agent/settings/language_extensions.toml` - Language detection
- `pr_agent/settings/*.toml` - Prompt templates per tool

**Build:**
- `pyproject.toml` - Package metadata, build config, tool settings
- `requirements.txt` - Pinned production dependencies
- `requirements-dev.txt` - Development dependencies

## Platform Requirements

**Development:**
- Python >=3.12
- Git (for GitPython operations)
- pip for dependency installation

**Production:**
- Docker (multi-stage builds in `docker/Dockerfile`)
- AWS Lambda via Mangum adapter (`docker/Dockerfile.lambda`, `pr_agent/servers/github_lambda_webhook.py`)
- GitHub Actions runner (`Dockerfile.github_action`)
- Any ASGI-compatible host (Gunicorn + Uvicorn)

---

*Stack analysis: 2026-07-02*
