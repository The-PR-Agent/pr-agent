# Repository Guidelines

This file is the shared source of repository guidance for coding agents. Tool-specific instruction files should import it instead of repeating repository-wide rules.

## Dos and Don’ts

- **Do** match the interpreter requirement declared in `pyproject.toml` (Python ≥ 3.12) and install `requirements.txt` plus `requirements-dev.txt` before running tools.
- **Do** run tests with `PYTHONPATH=.` set to keep imports functional (for example `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q`).
- **Do** adjust configuration through `.pr_agent.toml` or files under `pr_agent/settings/` instead of hard-coding values.
- **Don’t** commit secrets or access tokens; rely on environment variables as shown in the health and e2e tests.
- **Don’t** reformat or reorder files globally; match existing 120-character lines, import ordering, and docstring style.
- **Don’t** delete or rename configuration, prompt, or workflow files without maintainer approval.

## Project Structure and Module Organization

PR-Agent automates AI-assisted reviews for pull requests across multiple git providers.

- `pr_agent/agent/` orchestrates commands (`review`, `describe`, `improve`, etc.) via `pr_agent/agent/pr_agent.py`.
- `pr_agent/tools/` implements individual capabilities such as reviewers, code suggestions, docs updates, and label generation.
- `pr_agent/algo/` contains shared algorithms, model handlers, prompt/token handling, types, and utilities.
- `pr_agent/git_providers/` handles integrations with GitHub, GitLab, Bitbucket (cloud and server), Azure DevOps, Gitea, Gerrit, CodeCommit, local checkouts, and plain diffs; `pr_agent/identity_providers/` handles identity and `pr_agent/secret_providers/` handles secrets.
- `pr_agent/settings/` stores Dynaconf defaults (prompts, configuration templates, ignore lists) respected at runtime; `.pr_agent.toml` overrides repository-level behavior.
- `pr_agent/servers/` contains webhook and service entrypoints.
- `tests/unittest/`, `tests/e2e_tests/`, and `tests/health_test/` contain pytest-based unit, end-to-end, and smoke checks.
- `docs/` holds the MkDocs site (`docs/mkdocs.yml` plus content under `docs/docs/`); overrides live in `docs/overrides/`.
- `.github/workflows/` defines CI pipelines for unit tests, coverage, docs deployment, pre-commit, and PR-agent self-review.
- `docker/` and the root Dockerfiles provide build targets for services (`github_app`, `gitlab_webhook`, etc.) and the `test` stage used in CI.

## Architecture and Request Flow

PR-Agent is a CLI/server that runs AI-powered tools (`/review`, `/describe`, `/improve`, `/ask`, etc.) against pull requests on supported git providers or local input. The main dispatch flow is `pr_agent/agent/pr_agent.py` → `command2class` → a tool class under `pr_agent/tools/`.

A tool generally obtains the appropriate git provider, gathers pull-request context, prepares prompt variables and templates, calls the configured model handler, and publishes or stores the result through the provider.

### Prompt Building

Prompt-driven tools generally construct a `self.vars` dictionary and pass it with the system/user prompt strings to `TokenHandler`. Prompt rendering uses Jinja2 with `StrictUndefined`, so variables referenced by a template should be present in the corresponding vars dictionary; define optional values explicitly and guard optional sections with Jinja conditionals.

System/user prompt strings live as TOML files under `pr_agent/settings/` and are loaded into `global_settings` by `pr_agent/config_loader.py`. Tool and prompt names normally correspond, for example:

- `pr_reviewer.py` ↔ `pr_reviewer_prompts.toml`
- `pr_description.py` ↔ `pr_description_prompts.toml`
- `pr_code_suggestions.py` ↔ `code_suggestions/pr_code_suggestions_prompts.toml` (plus its related variants)

New prompt files must also be registered in the `settings_files=[...]` list in `pr_agent/config_loader.py` or they will not be loaded into `global_settings`.

### Settings and Runtime Configuration

Use `get_settings()` from `pr_agent/config_loader.py` as the shared settings accessor. It returns the request-scoped Dynaconf object from `starlette_context` when one is present, otherwise the module-level `global_settings` object.

Defaults live in `pr_agent/settings/configuration.toml`. Per-repository overrides come from the repository's `.pr_agent.toml` and are applied by `pr_agent/git_providers/utils.py::apply_repo_settings` before command dispatch. When introducing a configuration section, add its defaults and comments to `configuration.toml` and keep related prompt/config changes synchronized.

Sensitive values should stay in environment variables or the gitignored `.secrets.toml` files under `pr_agent/settings/` and `pr_agent/settings_prod/`. `apply_secrets_manager_config()` optionally loads values from AWS Secrets Manager.

### Git Providers

`pr_agent/git_providers/` contains provider implementations that share the `GitProvider` interface in `pr_agent/git_providers/git_provider.py`. Provider-dependent behavior should be selected through capability checks such as `provider.is_supported("feature")` rather than concrete provider-type checks, because individual providers can stub or override capabilities. Some output is gated this way too: semantic file types and several other `/describe` sections are only emitted where `gfm_markdown` is supported.

### Servers and Entrypoints

`pr_agent/servers/` hosts webhook and service entrypoints that translate provider events into `PRAgent.handle_request(...)` calls. The CLI entrypoint is `pr_agent/cli.py`, registered as the `pr-agent` console script.

## Build, Test, and Development Commands

- Create or activate a virtual environment, then install runtime dependencies with `pip install -r requirements.txt`; add development tooling via `pip install -r requirements-dev.txt`.
- Run a single unit test (verified): `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q`.
- Run the full unit suite: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest -v`.
- Execute the CLI locally once dependencies and API keys are available: `python -m pr_agent.cli --pr_url <https://host/org/repo/pull/123> review`.
- Build the test Docker target mirror of CI when containerizing: `docker build -f docker/Dockerfile --target test .` (loads dev dependencies and copies `tests/`).
- Generate and deploy documentation with MkDocs after installing the same extras as CI (`mkdocs-material`, `mkdocs-glightbox`): `mkdocs serve -f docs/mkdocs.yml` for previews and `mkdocs gh-deploy -f docs/mkdocs.yml` for publication.

## Coding Style and Existing Tooling

The repository currently has overlapping Ruff, Flake8, and isort tooling. Until their intended responsibilities are explicitly aligned, treat the points below as the checked-in state rather than an instruction to migrate or consolidate tools.

- Keep Python lines within the 120-character limit declared in `pyproject.toml`.
- `pyproject.toml` currently configures Ruff rules `E`, `F`, `B`, `I001`, and `I002`; only `I001` is listed as fixable.
- `.pre-commit-config.yaml` currently runs standalone `isort` for Python import ordering, together with trailing-whitespace, final-newline, TOML, YAML, and large-file checks. Ruff and `ruff-format` hooks are present there only as commented examples.
- `requirements-dev.txt` includes Flake8, but the repository has no dedicated Flake8 configuration, so a bare `flake8` does not use the project's 120-character line limit. If running Flake8, pass the scope and `--max-line-length=120` explicitly, and distinguish pre-existing findings from violations introduced by the change.
- The pre-commit GitHub Actions workflow is manual-only. The normal `build-and-test` workflow runs the unit suite rather than Ruff, Flake8, isort, or a formatter.
- No general-purpose Python formatter is currently enforced. Preserve the surrounding file's formatting and avoid unrelated rewrites or repository-wide formatting.
- If existing tools disagree, keep the patch focused rather than resolving the disagreement with unrelated mass changes; surface the conflict when it affects the task.
- Prefer double quotes for Python strings where consistent with the surrounding file.
- Match existing docstring and comment style—concise English comments using imperative phrasing only where necessary.
- Configuration files in `pr_agent/settings/` are TOML; preserve formatting, section order, and comments when editing prompts or defaults.
- Markdown in `docs/` uses MkDocs conventions (YAML front matter absent; rely on heading hierarchy already in place).
- When pre-commit is available, run it on the files you touched (`pre-commit run --files <paths>`) rather than across the whole tree, and review the automatic edits so unrelated changes are not included.

## Testing Guidelines

- Pytest is the standard framework; keep new tests under the closest matching directory (`tests/unittest/` for unit logic, `tests/e2e_tests/` for integration flows, `tests/health_test/` for smoke coverage).
- Pytest configuration lives in `pyproject.toml`, including `asyncio_mode = "auto"` and `testpaths = ["tests"]`. The current Docker test image removes `pyproject.toml` after dependency installation, so CI does not inherit those settings; mark every async test explicitly with `@pytest.mark.asyncio`.
- Prefer focused unit tests that isolate helpers in `pr_agent/algo/`, `pr_agent/tools/`, or provider adapters; use parameterized tests where existing files already do so.
- Set `PYTHONPATH=.` when invoking pytest from the repository root to avoid import errors.
- End-to-end suites require provider tokens (`TOKEN_GITHUB`, `TOKEN_GITLAB`, `BITBUCKET_USERNAME`, `BITBUCKET_PASSWORD`) and may take several minutes; run them only when credentials and sandboxes are configured.
- The health test (`tests/health_test/main.py`) exercises `/describe`, `/review`, and `/improve`; update expected artifacts if prompts change meaningfully.

## Commit and Pull Request Guidelines

- Follow `CONTRIBUTING.md`: keep changes focused, add or update tests, and use Conventional Commit-style messages (e.g., `fix: handle missing repo settings gracefully`).
- Target branch names follow `feature/<name>` or `fix/<issue>` patterns for substantial work.
- Reference related issues and update README or docs when user-facing behavior shifts.
- Ensure CI workflows (`build-and-test`, `code-coverage`, `docs-ci`) succeed locally or in draft PRs before requesting review; reproduce failures with the documented commands above.
- Include screenshots or terminal captures when modifying user-visible output or documentation previews.

## Safety and Permissions

- Ask for confirmation before adding dependencies, renaming files, or changing workflow definitions; many consumers embed these paths and prompts.
- Stay within existing formatting and directory conventions—avoid mass refactors, re-sorting of prompts, or reformatting Markdown beyond the touched sections.
- You may read files, list directories, and run targeted lint/test/doc commands without prior approval; coordinate before launching full Docker builds or e2e suites that rely on external credentials.
- Never commit cached credentials, API keys, or coverage artifacts; CI already handles secrets through GitHub Actions.
- Treat prompt and configuration files as single sources of truth—update mirrors (`.pr_agent.toml`, `pr_agent/settings/*.toml`) together when behavior changes.

## Security and Configuration Tips

- Secrets should be supplied through environment variables (see usages in `tests/e2e_tests/test_github_app.py` and `tests/health_test/main.py`); do not persist them in code or configuration files.
- Adjust runtime behavior by overriding keys in `.pr_agent.toml` or by supplying repository-specific Dynaconf files; keep overrides minimal and documented inside the PR description.
- Review `SECURITY.md` before disclosing vulnerabilities and follow its contact instructions for responsible reporting.
