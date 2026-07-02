# Codebase Structure

**Analysis Date:** 2026-07-02

## Directory Layout

```
pr-agent/
├── pr_agent/                  # Main Python package (all application code)
│   ├── agent/                 # Command dispatcher (PRAgent class)
│   ├── algo/                  # Core algorithms (diff processing, tokens, AI)
│   │   └── ai_handlers/      # LLM abstraction layer (LiteLLM, OpenAI, LangChain)
│   ├── git_providers/         # Git hosting platform integrations
│   ├── identity_providers/    # User identity/eligibility abstraction
│   ├── log/                   # Logging setup (Loguru-based)
│   ├── mosaico/              # Card-based notification/rendering subsystem
│   ├── secret_providers/      # Secret management (GCS, AWS Secrets Manager)
│   ├── servers/              # Entry points: webhook servers, action runners
│   ├── settings/             # TOML configuration and prompt templates
│   │   └── code_suggestions/ # Prompt templates for code suggestions tool
│   ├── tools/                # PR analysis tools (review, describe, improve, etc.)
│   ├── cli.py                # CLI entry point
│   ├── cli_pip.py            # Alternative CLI for pip-installed usage
│   └── config_loader.py      # Dynaconf configuration bootstrap
├── tests/                     # Test suite
│   ├── unittest/             # Unit tests
│   ├── e2e_tests/            # End-to-end tests
│   └── health_test/          # Health check tests
├── docs/                      # MkDocs documentation site
│   └── docs/                 # Documentation content (markdown)
├── docker/                    # Docker support files
├── github_action/            # GitHub Action entrypoint script
├── scripts/                  # Utility/maintenance scripts
├── pyproject.toml            # Project metadata, build config, tool config
├── requirements.txt          # Production dependencies
├── requirements-dev.txt      # Development dependencies
├── setup.py                  # Legacy setuptools entry
├── action.yaml               # GitHub Action definition
├── Dockerfile.github_action  # Dockerfile for GitHub Action
└── pr_compliance_checklist.yaml  # PR compliance rules definition
```

## Directory Purposes

**`pr_agent/agent/`:**
- Purpose: Central command dispatcher
- Contains: `pr_agent.py` with `PRAgent` class and `command2class` mapping
- Key files: `pr_agent/agent/pr_agent.py`

**`pr_agent/algo/`:**
- Purpose: Shared algorithms used by all tools
- Contains: Diff processing, token budget management, file filtering, language detection, utility functions
- Key files:
  - `pr_agent/algo/pr_processing.py` — diff retrieval, token-budgeted patch construction
  - `pr_agent/algo/token_handler.py` — token counting and budget management
  - `pr_agent/algo/git_patch_processing.py` — patch parsing and hunk manipulation
  - `pr_agent/algo/file_filter.py` — file ignore/include logic
  - `pr_agent/algo/language_handler.py` — language detection and sorting
  - `pr_agent/algo/utils.py` — shared utilities (model helpers, YAML parsing, markdown conversion)
  - `pr_agent/algo/types.py` — core data types (FilePatchInfo, EDIT_TYPE)
  - `pr_agent/algo/cli_args.py` — CLI argument validation

**`pr_agent/algo/ai_handlers/`:**
- Purpose: LLM API abstraction
- Contains: Base class and implementations for different AI backends
- Key files:
  - `pr_agent/algo/ai_handlers/base_ai_handler.py` — ABC defining `chat_completion` interface
  - `pr_agent/algo/ai_handlers/litellm_ai_handler.py` — Primary handler via litellm (supports OpenAI, Claude, Bedrock, etc.)
  - `pr_agent/algo/ai_handlers/litellm_helpers.py` — Streaming, Azure AD, response processing helpers
  - `pr_agent/algo/ai_handlers/langchain_ai_handler.py` — LangChain-based handler
  - `pr_agent/algo/ai_handlers/openai_ai_handler.py` — Direct OpenAI handler

**`pr_agent/git_providers/`:**
- Purpose: Abstraction over git hosting platforms
- Contains: One provider class per platform, all inheriting from `GitProvider` ABC
- Key files:
  - `pr_agent/git_providers/git_provider.py` — `GitProvider` ABC with shared clone logic
  - `pr_agent/git_providers/__init__.py` — Provider registry and factory functions
  - `pr_agent/git_providers/github_provider.py` — GitHub API integration
  - `pr_agent/git_providers/gitlab_provider.py` — GitLab API integration
  - `pr_agent/git_providers/bitbucket_provider.py` — Bitbucket Cloud integration
  - `pr_agent/git_providers/bitbucket_server_provider.py` — Bitbucket Server integration
  - `pr_agent/git_providers/azuredevops_provider.py` — Azure DevOps integration
  - `pr_agent/git_providers/gerrit_provider.py` — Gerrit integration
  - `pr_agent/git_providers/gitea_provider.py` — Gitea integration
  - `pr_agent/git_providers/codecommit_provider.py` — AWS CodeCommit integration
  - `pr_agent/git_providers/local_git_provider.py` — Local git repo (for testing/dev)
  - `pr_agent/git_providers/utils.py` — Shared provider utilities (repo settings application)

**`pr_agent/servers/`:**
- Purpose: HTTP servers and webhook handlers for each platform
- Contains: FastAPI apps, webhook processors, polling loops, GitHub Action runner
- Key files:
  - `pr_agent/servers/github_app.py` — GitHub App webhook server (FastAPI)
  - `pr_agent/servers/gitlab_webhook.py` — GitLab webhook server
  - `pr_agent/servers/bitbucket_app.py` — Bitbucket webhook server
  - `pr_agent/servers/bitbucket_server_webhook.py` — Bitbucket Server webhook
  - `pr_agent/servers/azuredevops_server_webhook.py` — Azure DevOps webhook
  - `pr_agent/servers/gerrit_server.py` — Gerrit event server
  - `pr_agent/servers/gitea_app.py` — Gitea webhook server
  - `pr_agent/servers/github_action_runner.py` — GitHub Actions runner
  - `pr_agent/servers/github_polling.py` — Polling-based GitHub integration
  - `pr_agent/servers/github_lambda_webhook.py` — AWS Lambda handler for GitHub
  - `pr_agent/servers/gitlab_lambda_webhook.py` — AWS Lambda handler for GitLab
  - `pr_agent/servers/utils.py` — Shared server utilities (signature verification, timeout dicts)
  - `pr_agent/servers/help.py` — Help message generation

**`pr_agent/tools/`:**
- Purpose: Individual PR analysis command implementations
- Contains: One class per command, each with a `run()` async method
- Key files:
  - `pr_agent/tools/pr_reviewer.py` — `PRReviewer` — AI code review
  - `pr_agent/tools/pr_description.py` — `PRDescription` — auto-generate PR description
  - `pr_agent/tools/pr_code_suggestions.py` — `PRCodeSuggestions` — inline code improvements
  - `pr_agent/tools/pr_questions.py` — `PRQuestions` — ask questions about PR
  - `pr_agent/tools/pr_line_questions.py` — `PR_LineQuestions` — line-specific questions
  - `pr_agent/tools/pr_update_changelog.py` — `PRUpdateChangelog` — changelog generation
  - `pr_agent/tools/pr_add_docs.py` — `PRAddDocs` — documentation suggestions
  - `pr_agent/tools/pr_generate_labels.py` — `PRGenerateLabels` — label suggestions
  - `pr_agent/tools/pr_similar_issue.py` — `PRSimilarIssue` — find similar issues
  - `pr_agent/tools/pr_help_message.py` — `PRHelpMessage` — help command
  - `pr_agent/tools/pr_help_docs.py` — `PRHelpDocs` — documentation Q&A (currently disabled)
  - `pr_agent/tools/pr_config.py` — `PRConfig` — show configuration
  - `pr_agent/tools/ticket_pr_compliance_check.py` — ticket/compliance extraction
  - `pr_agent/tools/progress_comment.py` — progress indicator comment builder

**`pr_agent/settings/`:**
- Purpose: Default configuration and prompt templates
- Contains: TOML files with settings defaults and Jinja2 prompt templates
- Key files:
  - `pr_agent/settings/configuration.toml` — main configuration defaults (models, features, thresholds)
  - `pr_agent/settings/pr_reviewer_prompts.toml` — system/user prompts for review tool
  - `pr_agent/settings/pr_description_prompts.toml` — prompts for description tool
  - `pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml` — prompts for improve tool
  - `pr_agent/settings/ignore.toml` — file ignore patterns
  - `pr_agent/settings/language_extensions.toml` — file extension to language mapping
  - `pr_agent/settings/custom_labels.toml` — custom label definitions

**`pr_agent/identity_providers/`:**
- Purpose: User identity and eligibility checking
- Contains: Provider abstraction for access control
- Key files: `pr_agent/identity_providers/__init__.py`

**`pr_agent/secret_providers/`:**
- Purpose: Secure secret retrieval from external stores
- Contains: Google Cloud Storage and AWS Secrets Manager integrations
- Key files: `pr_agent/secret_providers/__init__.py`

**`pr_agent/mosaico/`:**
- Purpose: Card-based rendering and notification dispatch subsystem
- Contains: Card rendering, diff provider, execution, observability
- Key files:
  - `pr_agent/mosaico/server.py` — Mosaico server entry point
  - `pr_agent/mosaico/dispatch.py` — Notification dispatch
  - `pr_agent/mosaico/card.py` — Card rendering
  - `pr_agent/mosaico/executor.py` — Execution orchestration

**`pr_agent/log/`:**
- Purpose: Centralized logging configuration
- Contains: Loguru setup, JSON formatter, context enrichment
- Key files: `pr_agent/log/__init__.py`

## Key File Locations

**Entry Points:**
- `pr_agent/cli.py`: CLI interface (`pr-agent` console script)
- `pr_agent/cli_pip.py`: Pip-installed CLI variant
- `pr_agent/servers/github_app.py`: GitHub App webhook server
- `pr_agent/servers/github_action_runner.py`: GitHub Actions runner

**Configuration:**
- `pr_agent/config_loader.py`: Dynaconf bootstrap and `get_settings()` factory
- `pr_agent/settings/configuration.toml`: All default settings
- `pr_agent/settings/.secrets.toml`: Local secrets (gitignored)
- `pyproject.toml`: Build system, tool config (ruff, pytest, bandit)

**Core Logic:**
- `pr_agent/agent/pr_agent.py`: Command routing
- `pr_agent/algo/pr_processing.py`: Diff retrieval and token-budgeted construction
- `pr_agent/algo/ai_handlers/litellm_ai_handler.py`: LLM call execution
- `pr_agent/algo/token_handler.py`: Token budget management

**Testing:**
- `tests/unittest/`: Unit test files
- `tests/e2e_tests/`: End-to-end integration tests
- `tests/health_test/`: Health check tests

## Naming Conventions

**Files:**
- Snake_case for all Python modules: `pr_reviewer.py`, `github_provider.py`
- Tool files prefixed with `pr_`: `pr_description.py`, `pr_code_suggestions.py`
- Provider files suffixed with `_provider`: `github_provider.py`, `gitlab_provider.py`
- Prompt templates suffixed with `_prompts.toml`: `pr_reviewer_prompts.toml`

**Directories:**
- Snake_case: `git_providers/`, `ai_handlers/`, `identity_providers/`
- Plural for collections of implementations: `servers/`, `tools/`, `settings/`

**Classes:**
- PascalCase with `PR` prefix for tools: `PRReviewer`, `PRDescription`, `PRCodeSuggestions`
- PascalCase with platform suffix for providers: `GithubProvider`, `GitLabProvider`
- `Base` prefix for abstract bases: `BaseAiHandler`

**Settings keys:**
- Section headers in lowercase snake_case: `[pr_reviewer]`, `[pr_description]`
- Keys in lowercase snake_case: `num_code_suggestions_per_chunk`
- Environment override via `UPPERCASE` (Dynaconf convention)

## Where to Add New Code

**New PR Tool (command):**
1. Create tool class in `pr_agent/tools/pr_<name>.py`
2. Implement `__init__(self, pr_url, args, ai_handler)` and `async run()`
3. Create prompt template in `pr_agent/settings/pr_<name>_prompts.toml`
4. Register in `pr_agent/settings/configuration.toml` (add `[pr_<name>]` section)
5. Add to `command2class` dict in `pr_agent/agent/pr_agent.py`
6. Add TOML file to `settings_files` list in `pr_agent/config_loader.py`
7. Add tests in `tests/unittest/`

**New Git Provider:**
1. Create `pr_agent/git_providers/<platform>_provider.py`
2. Inherit from `GitProvider` ABC in `pr_agent/git_providers/git_provider.py`
3. Implement all abstract methods (`get_files`, `get_diff_files`, `publish_comment`, etc.)
4. Register in `_GIT_PROVIDERS` dict in `pr_agent/git_providers/__init__.py`

**New AI Handler:**
1. Create `pr_agent/algo/ai_handlers/<name>_ai_handler.py`
2. Inherit from `BaseAiHandler` in `pr_agent/algo/ai_handlers/base_ai_handler.py`
3. Implement `chat_completion()` and `deployment_id` property

**New Webhook Server:**
1. Create `pr_agent/servers/<platform>_app.py` or `<platform>_webhook.py`
2. Define FastAPI router or equivalent HTTP handler
3. Call `PRAgent().handle_request(pr_url, [command])` to dispatch

**New Secret Provider:**
1. Create `pr_agent/secret_providers/<name>_provider.py`
2. Register in `pr_agent/secret_providers/__init__.py` factory

**Shared Utility Code:**
- General utilities: `pr_agent/algo/utils.py`
- Git patch manipulation: `pr_agent/algo/git_patch_processing.py`
- Server helpers: `pr_agent/servers/utils.py`
- Provider helpers: `pr_agent/git_providers/utils.py`

## Special Directories

**`pr_agent/settings/`:**
- Purpose: Default TOML configuration and Jinja2 prompt templates
- Generated: No (hand-authored)
- Committed: Yes
- Note: `.secrets.toml` is gitignored; only the template/placeholder is committed

**`docs/`:**
- Purpose: MkDocs documentation source
- Generated: Site is built from these sources
- Committed: Yes (source only, not built site)

**`docker/`:**
- Purpose: Docker support files (compose configs, Mosaico Dockerfile)
- Generated: No
- Committed: Yes

**`github_action/`:**
- Purpose: GitHub Action entrypoint shell script
- Generated: No
- Committed: Yes
- Key file: `github_action/entrypoint.sh`

---

*Structure analysis: 2026-07-02*
