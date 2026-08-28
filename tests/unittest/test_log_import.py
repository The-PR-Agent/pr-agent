import subprocess
import sys

_IMPORT_GET_LOGGER_FIRST = """
from pr_agent.log import get_logger

assert get_logger() is not None
"""


class TestLogImport:
    """Regression for importing pr_agent.log before any other pr_agent module (issue #2694)."""

    def test_get_logger_as_first_pr_agent_import(self):
        """`from pr_agent.log import get_logger` must succeed even when it is
        the first pr_agent import. A cwd with pyproject.toml used to trip a
        circular import through config_loader -> custom_merge_loader.
        Runs in a subprocess so a failed partial import cannot poison this process."""
        result = subprocess.run(
            [sys.executable, "-c", _IMPORT_GET_LOGGER_FIRST],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"from pr_agent.log import get_logger failed as the first import:\n{result.stderr}"
        )
