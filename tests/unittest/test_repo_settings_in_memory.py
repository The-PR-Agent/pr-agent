"""In-memory repo settings loading (apply_repo_settings_bytes) and its file-based compatibility wrapper."""

import copy
import os
import tempfile
from contextlib import suppress

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.custom_merge_loader import SecurityError
from pr_agent.git_providers import utils as git_utils

SECTIONS = ("CONFIG", "PR_REVIEWER", "OPENAI", "PROMPT_FRAGMENTS")


@pytest.fixture(autouse=True)
def restore_settings():
    settings = get_settings()
    snapshot = {section: copy.deepcopy(settings.as_dict().get(section)) for section in SECTIONS}
    per_directory = copy.deepcopy(vars(settings).get("_per_directory_original_values"))
    auto_cast = os.environ.get("AUTO_CAST_FOR_DYNACONF")
    yield
    for section, data in snapshot.items():
        with suppress(KeyError):
            settings.unset(section, force=True)
        if data is not None:
            settings.set(section, copy.deepcopy(data), merge=False)
    vars(settings).pop("_per_directory_original_values", None)
    if per_directory is not None:
        vars(settings)["_per_directory_original_values"] = per_directory
    if auto_cast is None:
        os.environ.pop("AUTO_CAST_FOR_DYNACONF", None)
    else:
        os.environ["AUTO_CAST_FOR_DYNACONF"] = auto_cast


def _section(name):
    return get_settings().as_dict().get(name.upper(), {})


class FakeProvider:
    def __init__(self, repo_settings):
        self.repo_settings = repo_settings

    def get_repo_settings(self):
        return self.repo_settings

    def is_supported(self, capability):
        return False


def _no_temp_files(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("repo settings must be applied without temp files")

    monkeypatch.setattr(tempfile, "mkstemp", fail)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail)
    monkeypatch.setattr(git_utils, "_write_settings_temp", fail)


@pytest.mark.parametrize("payload", [
    b"[pr_reviewer]\nnum_max_findings = 11\n",
    "[pr_reviewer]\nnum_max_findings = 11\n",
])
def test_bytes_and_str_payloads_apply_without_temp_files(monkeypatch, payload):
    _no_temp_files(monkeypatch)
    sibling = _section("pr_reviewer").get("require_tests_review")

    git_utils.apply_repo_settings_bytes(payload)

    assert _section("pr_reviewer")["num_max_findings"] == 11
    assert _section("pr_reviewer").get("require_tests_review") == sibling


def test_apply_repo_settings_uses_in_memory_path(monkeypatch):
    _no_temp_files(monkeypatch)
    errors = []
    provider = FakeProvider(b"[pr_reviewer]\nnum_max_findings = 13\n")
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(git_utils, "_get_per_directory_settings", lambda git_provider: [
        ("services/api", b"[pr_reviewer]\nnum_max_findings = 14\n"),
    ])
    monkeypatch.setattr(git_utils, "handle_configurations_errors", lambda errs, git_provider: errors.extend(errs))
    get_settings().set("config.use_repo_settings_file", True)

    git_utils.apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert errors == []
    # The per-directory layer is applied after the root config, so it wins.
    assert _section("pr_reviewer")["num_max_findings"] == 14


def test_oversized_payload_is_skipped_with_warning(monkeypatch):
    warnings = []
    monkeypatch.setattr(git_utils, "MAX_TOML_SIZE_IN_BYTES", 5)
    monkeypatch.setattr(git_utils.tomllib, "loads", lambda *a, **k: pytest.fail("oversized payload was parsed"))
    monkeypatch.setattr(git_utils, "get_logger", lambda: type("L", (), {"warning": warnings.append})())
    before = _section("pr_reviewer").get("num_max_findings")

    git_utils.apply_repo_settings_bytes(b"[pr_reviewer]\nnum_max_findings = 99\n")

    assert _section("pr_reviewer").get("num_max_findings") == before
    assert any("too large" in message for message in warnings)


def _apply_via_bytes(payload, scope):
    git_utils.apply_repo_settings_bytes(payload, repo_settings_scope=scope)


def _apply_via_file(payload, scope, tmp_path):
    path = tmp_path / "settings.toml"
    path.write_bytes(payload)
    git_utils._apply_repo_settings_file(str(path), repo_settings_scope=scope)


@pytest.mark.parametrize("via_file", [False, True])
@pytest.mark.parametrize("payload, scope, section, key", [
    (b"[openai]\nkey = 'sk-repo'\n", "repo", "openai", "key"),
    (b"[prompt_fragments]\nreview = 'x'\n", "repo", "prompt_fragments", "review"),
    (b"[config]\ngit_provider = 'gitlab'\n", "per_directory", "config", "git_provider"),
    (b"[config]\nai_handler = 'evil'\n", "per_directory", "config", "ai_handler"),
])
def test_forbidden_keys_blocked_identically(tmp_path, via_file, payload, scope, section, key):
    before = copy.deepcopy(_section(section).get(key))

    if via_file:
        _apply_via_file(payload, scope, tmp_path)
    else:
        _apply_via_bytes(payload, scope)

    assert _section(section).get(key) == before


@pytest.mark.parametrize("via_file", [False, True])
def test_security_directives_raise_identically(tmp_path, via_file):
    payload = b"dynaconf_include = ['/etc/passwd.toml']\n[pr_reviewer]\nnum_max_findings = 42\n"
    before = _section("pr_reviewer").get("num_max_findings")

    with pytest.raises(SecurityError) as excinfo:
        if via_file:
            _apply_via_file(payload, "repo", tmp_path)
        else:
            _apply_via_bytes(payload, "repo")

    # The generic label is reported, never a filesystem path.
    assert ".pr_agent.toml" in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)
    assert _section("pr_reviewer").get("num_max_findings") == before


def test_source_label_is_used_in_security_errors():
    with pytest.raises(SecurityError, match="services/api/.pr_agent.toml"):
        git_utils.apply_repo_settings_bytes(b"preload = ['x.toml']\n", source_label="services/api/.pr_agent.toml")


def test_legacy_file_wrapper_applies_real_file(tmp_path):
    path = tmp_path / ".pr_agent.toml"
    path.write_text("[pr_reviewer]\nnum_max_findings = 21\n")

    git_utils._apply_repo_settings_file(str(path))

    assert _section("pr_reviewer")["num_max_findings"] == 21
