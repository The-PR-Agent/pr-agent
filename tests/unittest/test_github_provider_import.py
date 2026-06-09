import subprocess
import sys

_IMPORT_WITHOUT_GITHUB_SECTION = """
from pr_agent.config_loader import global_settings

global_settings.unset("GITHUB", force=True)

import pr_agent.git_providers.github_provider  # noqa: F401
"""


class TestGithubProviderImport:
    """Regression tests for importing the GitHub provider without a [github] settings section (issue #2427)."""

    def test_import_without_github_section(self):
        """The module must import even when the mounted configuration has no [github] section,
        e.g. a GitLab-only deployment that replaces configuration.toml entirely.
        Runs in a subprocess so the modified global settings cannot leak into other tests."""
        result = subprocess.run([sys.executable, "-c", _IMPORT_WITHOUT_GITHUB_SECTION],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"import failed without a [github] section:\n{result.stderr}"
