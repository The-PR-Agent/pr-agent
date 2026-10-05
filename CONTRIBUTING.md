# Contributing to PR-Agent

Thank you for your interest in contributing to the PR-Agent project!

## Getting Started

1. Fork the repository and clone your fork
2. Install [uv](https://docs.astral.sh/uv/) and Python 3.12 or higher (the interpreter requirement declared in `pyproject.toml`)
3. Install dependencies with `uv sync` (creates `.venv` from `uv.lock`)
4. Create a new branch for your contribution:
   - For new features: `git checkout -b feature/your-feature-name`
   - For bug fixes: `git checkout -b fix/issue-description`
5. Make your changes
6. Write or update tests as needed
7. Run tests locally to ensure everything passes:
   ```bash
   PYTHONPATH=. uv run pytest tests/unittest
   ```
   The end-to-end and health suites require provider tokens or API keys,
   so the unit suite is the default local check.
8. Lint your changed files, then run the pre-commit hooks on them:
   ```bash
   uv run ruff check --fix <changed Python files>
   uv run pre-commit run --files <changed files>
   ```
9. Commit your changes using conventional commit messages.
10. Push to your fork and submit a pull request

## Development Guidelines

- Keep pull requests focused on a single feature or fix
- Follow the existing code style and formatting conventions
- Add unit tests for any new functionality using pytest
- Ensure test coverage for your changes
- Update documentation as needed

## Pull Request Process

1. Ensure your PR includes a clear description of the changes
2. Link any related issues
3. Update the README.md if needed
4. Wait for review from maintainers

## Questions or Need Help?

- Ask questions or start a discussion in [GitHub Discussions](https://github.com/the-pr-agent/pr-agent/discussions)
- Check the [documentation](https://docs.pr-agent.ai/) for detailed information
- Report bugs or request features through [GitHub Issues](https://github.com/the-pr-agent/pr-agent/issues)

## Release publishing setup (maintainers)

Before merging changes to Trusted Publishing, configure both services:

1. In GitHub **Settings → Environments → release**, require a trusted maintainer
   or team to review deployments, enable **Prevent self-review**, and disable
   administrator bypass. Under **Selected branches and tags**, add a branch rule
   for `main` and a separate tag rule for `v*`. A tag-name rule does not verify
   ancestry; `publish.yml` also checks that the published commit is in `main` history.
2. A PyPI owner of the existing `pr-agent` project must add a GitHub Trusted
   Publisher with owner `The-PR-Agent`, repository `pr-agent`, workflow filename
   `publish.yml`, and environment `release` (case-sensitive). Register it before
   merging the token-free workflow; an absent or mismatched publisher rejects uploads.
3. Validate the first controlled release: confirm the required environment approval,
   successful PyPI OIDC upload and attestations, Docker publication, and finalization.
   Delete `PYPI_API_TOKEN` only after a successful Trusted Publishing upload.

The workflow accepts releases whose commit is in `main` history, including older
main commits, and manual dispatches from `main`. It builds distributions in a
read-only job and transfers them to a separate publisher job with OIDC permission;
package build dependencies do not receive that permission. Build frontend and
backend versions are pinned, but runner tools and transitive dependencies are not
fully locked, so this does not guarantee reproducible builds.

See [PyPI publisher registration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
and [GitHub environment protection rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
