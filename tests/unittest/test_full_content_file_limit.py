"""Test configurable optional full-file reads and their trust boundary."""

from types import SimpleNamespace

import pytest

from pr_agent.algo.cli_args import CliArgs
from pr_agent.config_security import (
    REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS,
    is_repo_host_only_key,
)
from pr_agent.git_providers import git_provider


@pytest.mark.parametrize(("value", "expected"), [
    (0, 0), (1, 1), (24, 24), (49, 49), (50, 50), (200, 200),
    ("0", 0), ("49", 49), ("50", 50), (" 200 ", 200),
    (-1, 49), (201, 49), (True, 49), ("201", 49), ("1e2", 49),
    ("24.0", 49), (24.0, 49), (None, 49),
])
def test_full_content_limit_validation(monkeypatch, value, expected):
    settings = SimpleNamespace(
        get=lambda key, default=None: value if key == "CONFIG.MAX_FULL_CONTENT_FILES" else default
    )
    monkeypatch.setattr(git_provider, "get_settings", lambda: settings)

    assert git_provider.get_max_full_content_files() == expected


def test_full_content_limit_trust_boundaries():
    assert not is_repo_host_only_key("config", "max_full_content_files")
    assert "max_full_content_files" not in REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS["config"]
    assert CliArgs.validate_user_args(["--config.max_full_content_files=200"])[0] is False
    assert CliArgs.validate_user_args(["--config__max_full_content_files=200"])[0] is False
    assert CliArgs.validate_user_args(["--config={max_full_content_files: 200}"])[0] is False
