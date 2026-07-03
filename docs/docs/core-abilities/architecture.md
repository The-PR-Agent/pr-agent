# Architecture Overview

This page walks through how a PR-Agent command flows through the codebase, and gives recipes for the three most common kinds of contribution: adding context to a prompt, adding a tool, and adding a git provider.

## The dispatch flow

Every entry point — CLI, GitHub App, GitLab webhook, Bitbucket app, GitHub Action, polling server — converges on the same call:

```
PRAgent.handle_request(pr_url, command)        # pr_agent/agent/pr_agent.py
```

From there:

```
entry point (pr_agent/cli.py, pr_agent/servers/*.py)
        │
        ▼
apply_repo_settings(pr_url)                    # pr_agent/git_providers/utils.py
  merges: defaults → extra config → repo .pr_agent.toml → env vars
        │
        ▼
PRAgent.handle_request → command2class map     # pr_agent/agent/pr_agent.py
        │
        ▼
tool class (pr_agent/tools/pr_*.py), e.g. PRReviewer
  __init__: build git provider, self.vars, TokenHandler
  run():    fetch diff → render Jinja2 prompts → call model → parse → publish
        │
        ├── git provider (pr_agent/git_providers/)  fetches PR data, publishes output
        ├── prompt TOMLs (pr_agent/settings/)       system/user templates per tool
        └── AI handler (pr_agent/algo/ai_handlers/) LiteLLM by default
```

Key modules:

| Module | Responsibility |
|--------|----------------|
| `pr_agent/agent/pr_agent.py` | maps command strings (`/review`, `improve`, ...) to tool classes |
| `pr_agent/tools/` | one class per command; owns prompt variables and output publishing |
| `pr_agent/git_providers/` | one class per platform behind the `GitProvider` interface |
| `pr_agent/algo/pr_processing.py` | diff retrieval, token-budget compression, model fallback loop |
| `pr_agent/algo/token_handler.py` | prompt token accounting |
| `pr_agent/algo/ai_handlers/` | model invocation (LiteLLM, OpenAI, LangChain) |
| `pr_agent/settings/` | prompt TOMLs and `configuration.toml` (the authoritative option list) |
| `pr_agent/config_loader.py` | Dynaconf setup; `get_settings()` is the single accessor |
| `pr_agent/servers/` | webhook entry points per platform |

## Prompt building (the hot path)

Every tool follows the same shape. In `__init__` it constructs a `self.vars` dict and passes it, together with the system/user prompt strings from settings, to a `TokenHandler`. At run time the prompts are rendered with `jinja2.Environment(undefined=StrictUndefined)` against `self.vars`.

Because templates use `StrictUndefined`, **every variable referenced in a template must be present in `vars`** — guard optional content with `{%- if my_var %}` blocks, never optional Jinja lookups. The unit test `tests/unittest/test_prompt_templates_render.py` renders all prompt templates in CI and fails on a missing variable or syntax error.

### Recipe: add new context to a prompt

1. Add the value to the tool's `self.vars` dict (e.g. in `pr_agent/tools/pr_reviewer.py`).
2. Add a guarded block to the matching prompt TOML (e.g. `pr_agent/settings/pr_reviewer_prompts.toml`):
   ```jinja
   {%- if my_new_var %}
   ...use {{ my_new_var }}...
   {%- endif %}
   ```
3. If the feature is configurable, add the option to `pr_agent/settings/configuration.toml` with a comment — that file is the single source of truth for options.

The tool ↔ prompt file mapping follows naming conventions: `pr_reviewer.py` ↔ `pr_reviewer_prompts.toml`, `pr_description.py` ↔ `pr_description_prompts.toml`, `pr_code_suggestions.py` ↔ `code_suggestions/pr_code_suggestions_prompts.toml` (and the `_not_decoupled` variant).

### Recipe: add a new tool

1. Create `pr_agent/tools/pr_my_tool.py` with a class exposing `__init__(pr_url, args, ai_handler)` and `async run()`.
2. Create `pr_agent/settings/pr_my_tool_prompts.toml` with `[pr_my_tool_prompt]` `system`/`user` strings.
3. Register the prompt file in the `settings_files` list in `pr_agent/config_loader.py` (new files are **not** picked up automatically).
4. Add a `[pr_my_tool]` section to `configuration.toml` for the tool's options.
5. Map the command in the `command2class` dict in `pr_agent/agent/pr_agent.py`.

### Recipe: add a git provider

1. Create `pr_agent/git_providers/my_provider.py` subclassing `GitProvider` and implement the abstract methods, including `_is_supported(capability)` (capability strings must come from `KNOWN_CAPABILITIES` in `git_provider.py`).
2. Register it in the `_GIT_PROVIDERS` dict in `pr_agent/git_providers/__init__.py`.
3. Add a configuration section (URL, credentials) to `configuration.toml`.
4. The contract test `tests/unittest/test_git_provider_contract.py` will pick the provider up automatically and verify the interface is fully implemented.

Tools must never branch on `isinstance(provider, GithubProvider)` for behavior — query `provider.is_supported("capability")` instead, since providers may stub or override features.

## Configuration precedence

`get_settings()` returns a request-scoped Dynaconf object in server flows (stored in `starlette_context`) or the module-level `global_settings` otherwise. Values are merged in this order (later wins):

1. Defaults from `pr_agent/settings/*.toml`
2. `.secrets.toml` / AWS Secrets Manager
3. Extra config file (`--extra_config_url`)
4. The repo's `.pr_agent.toml` (per-section merge, see `apply_repo_settings`)
5. Environment variables
6. CLI `--section.key=value` arguments

Unknown sections or keys in a repo's `.pr_agent.toml` are logged as warnings (see `pr_agent/settings_validator.py`). Run `pr-agent --validate_config` to sanity-check a deployment's configuration without contacting a PR.
