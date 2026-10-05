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
