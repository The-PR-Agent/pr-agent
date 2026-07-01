# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See `AGENTS.md` for the full repository guidelines (dos/don'ts, coding style, safety, security). The notes below are the high-leverage subset for navigating PR-Agent quickly.

## Common commands

Run from the repo root with the virtualenv activated:

- Single unit test: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q`
- Full unit suite: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest -v`
- Pytest auto-discovery is configured in `pyproject.toml` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`); always set `PYTHONPATH=.` to avoid import errors.
- Local CLI run: `python -m pr_agent.cli --pr_url <url> review`
- Lint: project uses Ruff with `line-length = 120` (config in `pyproject.toml`); pre-commit hooks live in `.pre-commit-config.yaml`.
- Docker test target (mirror of CI): `docker build -f docker/Dockerfile --target test .`
- E2E (`tests/e2e_tests/`) and health (`tests/health_test/`) suites require provider tokens (`TOKEN_GITHUB`, `TOKEN_GITLAB`, `BITBUCKET_USERNAME`/`PASSWORD`) and are slow — only run when configured.

Python ≥ 3.12 is required (see `pyproject.toml`).

## Architecture

PR-Agent is a CLI/server that runs AI-powered tools (`/review`, `/describe`, `/improve`, `/ask`, etc.) against a pull request on GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, Gerrit, or local. The dispatch flow is `pr_agent/agent/pr_agent.py` → `command2class` map → tool class in `pr_agent/tools/`. Each tool is responsible for fetching the PR via a git provider, building a Jinja2 prompt, calling the model, and publishing the result.

### Prompt building (the hot path)

Every tool follows the same shape: in `__init__` it constructs a `self.vars` dict, then passes it together with system/user prompt strings to a `TokenHandler`. At run time the prompts are rendered with `jinja2.Environment(undefined=StrictUndefined)` against `self.vars`. Adding new context to a prompt means: extend `self.vars` in the tool, then add a `{%- if my_var %}` block in the matching prompt TOML. Because templates use `StrictUndefined`, every variable referenced in the template must be present in `vars` (use `{%- if … %}` guards, never optional Jinja lookups).

System/user prompt strings live as TOML in `pr_agent/settings/`, loaded via Dynaconf as part of `global_settings` in `pr_agent/config_loader.py`. The mapping between tool and prompt file follows naming conventions: `pr_reviewer.py` ↔ `pr_reviewer_prompts.toml`, `pr_description.py` ↔ `pr_description_prompts.toml`, `pr_code_suggestions.py` ↔ `code_suggestions/pr_code_suggestions_prompts.toml` (and the `_not_decoupled` variant). New prompt files must also be registered in the `settings_files=[...]` list in `config_loader.py` to be loaded into `global_settings`.

### Settings and runtime config

`get_settings()` from `pr_agent/config_loader.py` is the single accessor for configuration. It returns either a request-scoped Dynaconf object stored in `starlette_context` (server flows) or the module-level `global_settings`. Defaults live in `pr_agent/settings/configuration.toml`; per-repo overrides come from the repo's `.pr_agent.toml`, merged in `pr_agent/git_providers/utils.py::apply_repo_settings` (called once per request before tool dispatch). When introducing a new config section, add it to `configuration.toml` with comments — that file is the authoritative listing of options, and `apply_repo_settings` does a per-section merge so partial overrides work.

Sensitive values (API keys, tokens) come from environment variables or `.secrets.toml` (gitignored); `apply_secrets_manager_config()` optionally pulls from AWS Secrets Manager.

### Git providers

`pr_agent/git_providers/` contains one provider per platform (GitHub, GitLab, Bitbucket variants, Azure DevOps, Gitea, Gerrit, Codecommit, local). They share the `GitProvider` interface in `git_providers/git_provider.py` (capabilities probed via `is_supported("feature")`) and are selected via `config.git_provider`. Tools should never branch on `isinstance(provider, GithubProvider)` for behavior — query `is_supported(...)` instead, since providers may stub or override features. Some prompt features (e.g. semantic file types in `/describe`) are gated on `gfm_markdown` support.

### Servers and entrypoints

`pr_agent/servers/` hosts the webhook entrypoints (`github_app.py`, `gitlab_webhook.py`, `bitbucket_app.py`, etc.) that translate webhooks into `PRAgent.handle_request(pr_url, command)` calls. The CLI entry point is `pr_agent/cli.py` (registered as the `pr-agent` console script).

### Tests

Unit tests in `tests/unittest/` are the right place for helpers in `pr_agent/algo/`, prompt-building logic, and provider adapters; mirror the file naming pattern (`test_<module>.py`). Use `parametrize` where the surrounding files do. The health test (`tests/health_test/main.py`) exercises `/describe`, `/review`, `/improve` against real PRs and is the canary for prompt regressions — update its expected artifacts when prompts change meaningfully.

## Conventions to keep in mind

- Prompt and configuration TOMLs are single sources of truth. When changing behavior, update the prompts and the config defaults together; don't fork values across files.
- Conventional Commit messages; feature branches as `feature/<name>` or `fix/<issue>`.
- Don't reformat or reorder unrelated lines in prompt/config files — diffs in those files are reviewed closely and small noise is rejected.
