# Repository Guidelines

This file is the shared source of repository guidance for coding agents. Tool-specific instruction files should import it instead of repeating repository-wide rules.

## Dos and Don’ts

- **Do** match the interpreter requirement declared in `pyproject.toml` (Python ≥ 3.12) and install dependencies with `uv sync` (runtime + dev, from `uv.lock`) before running tools.
- **Do** run tests with `PYTHONPATH=.` set to keep imports functional (for example `PYTHONPATH=. uv run pytest tests/unittest/test_output_models.py -q`).
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
- `docs/` holds the Docusaurus site (`docs/docusaurus.config.js` and `docs/sidebars.js` plus content under `docs/docs/`); theme overrides live in `docs/src/` and static assets in `docs/static/`.
- `.github/workflows/` defines CI pipelines for unit tests, coverage, docs deployment, pre-commit, and PR-agent self-review.
- `docker/` provides build targets for services (`github_app`, `gitlab_webhook`, etc.) and the `test` stage used in CI; the root `Dockerfile.github_action_dockerhub` only wraps the published `github_action` image for `action.yaml`.

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

`docs/docs/usage-guide/configuration_reference.md` is generated from `configuration.toml` by `python scripts/generate_config_reference.py [output_path]` (run from the repository root), and its header says not to edit it by hand. Write the output to a temporary path and diff it against the page before replacing anything. `tests/unittest/test_config_reference.py` checks that the page matches the generator output, lists exactly the active keys, and asserts a hard-coded active-key count, so update that count when adding or removing keys.

Repository overrides are filtered by `pr_agent/config_security.py`; host-only keys must be supplied through host configuration. Configuration file merges preserve other fields in a section but replace overlapping list/dict values rather than appending or deep-merging; environment variables take precedence.

Sensitive values should stay in environment variables or the gitignored `.secrets.toml` files under `pr_agent/settings/` and `pr_agent/settings_prod/`. `apply_secrets_manager_config()` optionally loads values from AWS Secrets Manager.

### Git Providers

`pr_agent/git_providers/` contains provider implementations that share the `GitProvider` interface in `pr_agent/git_providers/git_provider.py`. Provider-dependent behavior should be selected through capability checks such as `provider.is_supported("feature")` rather than concrete provider-type checks, because individual providers can stub or override capabilities. Some output is gated this way too: semantic file types and several other `/describe` sections are only emitted where `gfm_markdown` is supported.

### Servers and Entrypoints

`pr_agent/servers/` hosts webhook and service entrypoints that translate provider events into `PRAgent.handle_request(...)` calls. The CLI entrypoint is `pr_agent/cli.py`, registered as the `pr-agent` console script.

## Build, Test, and Development Commands

- Install dependencies (runtime + dev) into a project virtualenv from the lockfile with `uv sync` using a uv version that satisfies `tool.uv.required-version` in `pyproject.toml`; `uv run` auto-syncs before each command.
- Run a single unit test (verified): `PYTHONPATH=. uv run pytest tests/unittest/test_output_models.py -q`.
- Run the full unit suite: `PYTHONPATH=. uv run pytest tests/unittest -v`.
- CI also runs `uv run pytest -q tests/packaging/test_help_docs_package.py`; it is outside `testpaths`, so the unit suite does not cover it. The distribution-contract test checks that the installed uv version satisfies `tool.uv.required-version` in `pyproject.toml`.
- Execute the CLI locally once dependencies and API keys are available: `uv run pr-agent --pr_url <https://host/org/repo/pull/123> review`.
- Build the test Docker target mirror of CI when containerizing: `docker build -f docker/Dockerfile --target test .` (loads dev dependencies and copies `tests/`).
- Build the documentation the same way CI does (Node.js 22): `npm ci` then `npm run build` from `docs/`; `npm start` serves a live preview. After building, run `python3 scripts/check_docs_urls.py` from the repository root to check `docs/build` as CI does. The `docs-ci` workflow publishes `docs/build` to GitHub Pages.

## Coding Style and Existing Tooling

Ruff is the single linting tool: `pyproject.toml` configures it and the pre-commit Ruff hook enforces it (Flake8 and the standalone isort hook have been removed).

- Keep Python lines within the 120-character limit declared in `pyproject.toml`.
- `pyproject.toml` configures Ruff rules `E`, `F`, `B`, `I`, `UP` and `FURB`. Within the files touched by the task, fix lint violations rather than adding ignore entries. Remove corresponding ignore entries only when they are no longer needed, and preserve intentional exceptions.
- Before committing, run `uv run ruff check --fix <paths>` on the Python files you touched and fix every issue it reports. Keep fixes mechanical (rename, remove unused imports, sort imports); do not alter program logic while cleaning up — if a lint fix would change behavior, surface it instead of applying it silently.
- `.pre-commit-config.yaml` is the source of truth for enabled pre-commit hooks. Run them on the files you touched with `uvx pre-commit run --files <paths>` and review the automatic edits so unrelated changes are not included. The pre-commit GitHub Actions workflow runs on pull requests, so the hooks are enforced in CI.
- No general-purpose Python formatter is currently enforced (`ruff format` is deliberately not adopted yet). Preserve the surrounding file's formatting and avoid unrelated rewrites or repository-wide formatting.
- Prefer double quotes for Python strings where consistent with the surrounding file.
- Match existing docstring and comment style—concise English comments using imperative phrasing only where necessary.
- Configuration files in `pr_agent/settings/` are TOML; preserve formatting, section order, and comments when editing prompts or defaults.
- Markdown in `docs/` uses Docusaurus conventions: every page starts with YAML front matter (`title`, `sidebar_position`), admonitions use `:::note` fences, collapsible sections use `<details>`, and images live in `docs/static/img/` referenced as `/img/<file>`. Pages stay `.md` (CommonMark) unless they need a JSX component, in which case they are `.mdx`. Register every new page in `docs/sidebars.js`.

## Testing Guidelines

- Pytest is the standard framework; keep new tests under the closest matching directory (`tests/unittest/` for unit logic, `tests/e2e_tests/` for integration flows, `tests/health_test/` for smoke coverage).
- Pytest configuration lives in `pyproject.toml`, including `asyncio_mode = "auto"` and `testpaths = ["tests/unittest"]`. The Docker test image keeps `pyproject.toml` at `/app` (uv installs from it), so CI inherits these settings as well. Plain `PYTHONPATH=. uv run pytest` therefore defaults to the unit suite; invoke end-to-end tests explicitly.
- Prefer focused unit tests that isolate helpers in `pr_agent/algo/`, `pr_agent/tools/`, or provider adapters; use parameterized tests where existing files already do so.
- Set `PYTHONPATH=.` when invoking pytest from the repository root to avoid import errors.
- End-to-end suites require provider credentials and may take several minutes; `TOKEN_GITHUB`, `TOKEN_GITLAB`, `BITBUCKET_USERNAME`, and `BITBUCKET_PASSWORD` are CI secret names, not runtime environment variables (see the mappings in `.github/workflows/e2e_tests.yaml`). These tests create remote branches and pull/merge requests and expect deployed apps/webhooks to process them; run them only when credentials and sandboxes are configured.
- `tests/health_test/main.py` is a manual script for `/describe`, `/review`, and `/improve`; it asserts artifact headers inline, not checked-in artifact files. Update those inline assertions if prompts change meaningfully. CI provider smoke runs `tests/health_test/test_github_smoke.py` instead (`/describe` only, model stubbed, `GITHUB.USER_TOKEN`).

## Review Checklist

- A setting can arrive as a TOML list, a plain string, or an environment value that Dynaconf parses as TOML (`SECTION__KEY=123` arrives as an int). Code that reads it handles each form.
- A fix in one git provider, AI handler or server usually applies to its siblings. Check the same logic in the others and fix them or name them in the PR.
- New or changed regexes run in linear time: no nested or adjacent unbounded quantifiers that can match the same characters, and no unbounded leading quantifier in an unanchored search. User-supplied patterns and globs get a length cap before they are compiled or translated.
- Docs and config comments describe what the branch does. A PR that changes a default or a setting's behavior updates the `configuration.toml` comment and the docs page that state it, and none documents behavior from another PR.

## Commit and Pull Request Guidelines

- Follow `CONTRIBUTING.md`: keep changes focused, add or update tests, and use Conventional Commit-style messages (e.g., `fix: handle missing repo settings gracefully`).
- Target branch names follow `feature/<name>` or `fix/<issue>` patterns for substantial work.
- Reference related issues and update README or docs when user-facing behavior shifts.
- Before requesting review, run the relevant local checks above and make sure the corresponding CI workflows (`build-and-test`, `pre-commit`) pass; coverage is collected and uploaded by `build-and-test`, and `docs-ci` builds the documentation on every pull request that touches `docs/**` (publishing only on pushes to `main` and `add-docs-portal`). That build fails on broken links and anchors, and also runs `scripts/check_docs_urls.py` to confirm the documentation URLs hardcoded in `pr_agent/` and the README still resolve.
- Include screenshots or terminal captures when modifying user-visible output or documentation previews.

## Safety and Permissions

- Ask for confirmation before adding dependencies, renaming files, or changing workflow definitions; many consumers embed these paths and prompts.
- Stay within existing formatting and directory conventions—avoid mass refactors, re-sorting of prompts, or reformatting Markdown beyond the touched sections.
- You may read files, list directories, and run targeted lint/test/doc commands without prior approval; coordinate before launching full Docker builds or e2e suites that rely on external credentials.
- Never commit cached credentials, API keys, or coverage artifacts; CI already handles secrets through GitHub Actions.
- Treat prompt and configuration files as single sources of truth—keep related defaults and prompts under `pr_agent/settings/` synchronized when behavior changes. The root `.pr_agent.toml` is intended for this repository's hosted Qodo reviews, not as a mirror of the packaged defaults; some entries have no open-source implementation.

## Security and Configuration Tips

- Secrets should be supplied through environment variables (see usages in `tests/e2e_tests/test_github_app.py` and `tests/health_test/main.py`); do not persist them in code or configuration files.
- Adjust runtime behavior by overriding keys in `.pr_agent.toml` or by supplying repository-specific Dynaconf files; keep overrides minimal and documented inside the PR description.
- Review `SECURITY.md` before disclosing vulnerabilities and follow its contact instructions for responsible reporting.
