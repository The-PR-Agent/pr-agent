import importlib

from pr_agent.config_loader import global_settings


class TestGithubProviderImport:
    """Regression tests for importing the GitHub provider without a [github] settings section (issue #2427)."""

    def test_import_without_github_section(self):
        """The module must import even when the mounted configuration has no [github] section,
        e.g. a GitLab-only deployment that replaces configuration.toml entirely."""
        import pr_agent.git_providers.github_provider as github_provider

        github_section = global_settings.get("GITHUB")
        assert github_section is not None  # sanity check: default configuration defines [github]
        try:
            global_settings.unset("GITHUB", force=True)
            importlib.reload(github_provider)  # evaluates the class-body @retry decorator again
        finally:
            global_settings.set("GITHUB", github_section)
            importlib.reload(github_provider)
