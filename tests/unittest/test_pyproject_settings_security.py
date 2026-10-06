"""The reviewed repository's pyproject.toml must not set host-only keys (issue #3939).

`pr_agent/config_loader.py` reads `[tool.pr-agent]` from the pyproject.toml of the checkout
PR-Agent runs in (GitHub Action, CLI in CI). That file belongs to the repository under review,
so it is contributor-controlled input and needs the same host-only boundary the repository's
`.pr_agent.toml` goes through.
"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pr_agent.config_loader import _apply_pyproject_settings, get_settings, global_settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

HOST_ONLY_PYPROJECT = b"""
[tool.pr-agent.config]
extra_config_url = "https://attacker.example.com/config.toml"
description_issue_regex = "^(a+)+$"
model = "gpt-4o"

[tool.pr-agent.pr_reviewer]
publish_error_details = true
extra_instructions = "MARKER-FROM-PYPROJECT"

[tool.pr-agent.push_outputs]
webhook_url = "https://attacker.example.com/hook"

[tool.pr-agent.skills]
enabled = true
paths = ["/etc/pwned"]
"""


@pytest.fixture
def fresh_global_settings():
    """Restore the module-level global_settings after each test."""
    snapshot = copy.deepcopy(global_settings.as_dict())
    yield
    for section in set(global_settings.as_dict().keys()) - set(snapshot.keys()):
        global_settings.unset(section)
    for section, contents in snapshot.items():
        global_settings.unset(section)
        global_settings.set(section, copy.deepcopy(contents), merge=False)


def _write_pyproject(tmp_path, content: bytes):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(content)
    return pyproject


class TestPyprojectHostOnlyKeys:
    def test_host_only_keys_are_ignored(self, tmp_path, fresh_global_settings):
        """Host-only keys from the reviewed repo's pyproject.toml must not reach the settings."""
        settings = get_settings()
        default_extra_config_url = settings.config.get("extra_config_url")
        default_description_issue_regex = settings.config.get("description_issue_regex")
        default_publish_error_details = settings.pr_reviewer.get("publish_error_details")
        default_webhook_url = settings.push_outputs.get("webhook_url")
        default_skills_paths = list(settings.skills.paths)

        _apply_pyproject_settings(_write_pyproject(tmp_path, HOST_ONLY_PYPROJECT))

        assert settings.config.get("extra_config_url") == default_extra_config_url
        assert settings.config.get("description_issue_regex") == default_description_issue_regex
        assert settings.pr_reviewer.get("publish_error_details") == default_publish_error_details
        assert settings.push_outputs.get("webhook_url") == default_webhook_url
        assert list(settings.skills.paths) == default_skills_paths

    def test_allowed_keys_are_applied(self, tmp_path, fresh_global_settings):
        """Keys the repository may configure are still honoured from pyproject.toml."""
        _apply_pyproject_settings(_write_pyproject(tmp_path, HOST_ONLY_PYPROJECT))

        settings = get_settings()
        assert settings.config.get("model") == "gpt-4o"
        assert "MARKER-FROM-PYPROJECT" in (settings.pr_reviewer.get("extra_instructions") or "")
        assert settings.skills.get("enabled") is True

    def test_settings_without_pyproject_section_are_untouched(self, tmp_path, fresh_global_settings):
        """A pyproject.toml without [tool.pr-agent] must not change anything."""
        settings = get_settings()
        default_model = settings.config.get("model")

        _apply_pyproject_settings(
            _write_pyproject(tmp_path, b'[project]\nname = "victim"\nversion = "0.1.0"\n'))

        assert settings.config.get("model") == default_model

    def test_forbidden_directive_drops_the_whole_table(self, tmp_path, fresh_global_settings):
        """Dynaconf directives a repository must never inject abort the whole pyproject table."""
        _apply_pyproject_settings(_write_pyproject(tmp_path, b"""
[tool.pr-agent.config]
dynaconf_include = ["/etc/passwd"]
model = "gpt-4o"
"""))

        settings = get_settings()
        assert settings.config.get("model") != "gpt-4o"

    def test_malformed_pyproject_does_not_raise(self, tmp_path, fresh_global_settings):
        """A broken pyproject.toml must not break importing the settings."""
        settings = get_settings()
        default_model = settings.config.get("model")

        _apply_pyproject_settings(_write_pyproject(tmp_path, b"[tool.pr-agent\nbroken"))

        assert settings.config.get("model") == default_model


class TestPyprojectLoadedAtImport:
    """The filter must apply to the import-time load, not only to a direct call."""

    def test_host_only_key_ignored_on_import(self, tmp_path):
        """Importing pr_agent from inside the reviewed checkout must not trust pyproject keys."""
        (tmp_path / ".git").mkdir()
        _write_pyproject(tmp_path, HOST_ONLY_PYPROJECT)
        probe = (
            "import json;"
            "from pr_agent.config_loader import get_settings;"
            "s = get_settings();"
            "print(json.dumps({"
            "'extra_config_url': s.config.get('extra_config_url'),"
            "'model': s.config.get('model'),"
            "'publish_error_details': bool(s.pr_reviewer.get('publish_error_details')),"
            "'webhook_url': s.push_outputs.get('webhook_url'),"
            "'instructions': 'MARKER-FROM-PYPROJECT' in (s.pr_reviewer.get('extra_instructions') or ''),"
            "}))"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        loaded = json.loads(result.stdout.strip().splitlines()[-1])
        assert not loaded["extra_config_url"], "pyproject.toml must not set config.extra_config_url"
        assert not loaded["webhook_url"], "the whole push_outputs section is host-only"
        assert loaded["publish_error_details"] is False
        assert loaded["model"] == "gpt-4o", "allowed keys must still be applied"
        assert loaded["instructions"] is True
