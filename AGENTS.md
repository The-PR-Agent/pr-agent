# Repository Guidelines

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
- `pr_agent/git_providers/` and `pr_agent/identity_providers/` handle integrations with GitHub, GitLab, Bitbucket, Azure DevOps, and secrets.
- `pr_agent/settings/` stores Dynaconf defaults (prompts, configuration templates, ignore lists) respected at runtime; `.pr_agent.toml` overrides repository-level behavior.
- `tests/unittest/`, `tests/e2e_tests/`, and `tests/health_test/` contain pytest-based unit, end-to-end, and smoke checks.
- `docs/` holds the MkDocs site (`docs/mkdocs.yml` plus content under `docs/docs/`); overrides live in `docs/overrides/`.
- `.github/workflows/` defines CI pipelines for unit tests, coverage, docs deployment, pre-commit, and PR-agent self-review.
- `docker/` and the root Dockerfiles provide build targets for services (`github_app`, `gitlab_webhook`, etc.) and the `test` stage used in CI.

## Build, Test, and Development Commands

- Create or activate a virtual environment, then install runtime dependencies with `pip install -r requirements.txt`; add development tooling via `pip install -r requirements-dev.txt`.
- Run a single unit test (verified): `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q`.
- Run the full unit suite: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest -v`.
- Execute the CLI locally once dependencies and API keys are available: `python -m pr_agent.cli --pr_url <https://host/org/repo/pull/123> review`.
- Build the test Docker target mirror of CI when containerizing: `docker build -f docker/Dockerfile --target test .` (loads dev dependencies and copies `tests/`).
- Generate and deploy documentation with MkDocs after installing the same extras as CI (`mkdocs-material`, `mkdocs-glightbox`): `mkdocs serve -f docs/mkdocs.yml` for previews and `mkdocs gh-deploy -f docs/mkdocs.yml` for publication.

## Coding Style and Naming Conventions

- Python sources follow the Ruff configuration in `pyproject.toml` (`line-length = 120`, Pyflakes plus `flake8-bugbear` checks, and isort ordering). Keep imports grouped as isort would produce and prefer double quotes for strings.
- Pre-commit (`.pre-commit-config.yaml`) enforces trailing whitespace cleanup, final newlines, TOML/YAML validity, and optional `isort`; run `pre-commit run --all-files` before submitting patches if installed.
- Before committing, run `flake8` and fix every issue it reports. Keep fixes mechanical (rename, reformat, remove unused imports, add missing newlines); do not alter program logic while cleaning up—if a lint fix would change behavior, surface it instead of applying it silently.
- Match existing docstring and comment style—concise English comments using imperative phrasing only where necessary.
- Configuration files in `pr_agent/settings/` are TOML; preserve formatting, section order, and comments when editing prompts or defaults.
- Markdown in `docs/` uses MkDocs conventions (YAML front matter absent; rely on heading hierarchy already in place).

## Testing Guidelines

- Pytest is the standard framework; keep new tests under the closest matching directory (`tests/unittest/` for unit logic, `tests/e2e_tests/` for integration flows, `tests/health_test/` for smoke coverage).
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
