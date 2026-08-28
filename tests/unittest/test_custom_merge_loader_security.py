"""
Security and behavior tests for pr_agent.custom_merge_loader.

These tests exercise validate_file_security directly with forbidden directives at
various nesting positions, deep-nesting guard, and a representative safe config.
They also exercise the load() entry point against a minimal fake Dynaconf-like
object to verify file-skipping behavior, security enforcement, and single-key
loading semantics.
"""

import importlib
from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

# Import pr_agent.config_loader first (for its module-level side effects) to
# complete the config_loader import chain and avoid the circular import between
# pr_agent.log and pr_agent.custom_merge_loader.
importlib.import_module("pr_agent.config_loader")
custom_merge_loader = importlib.import_module("pr_agent.custom_merge_loader")
load = custom_merge_loader.load
validate_file_security = custom_merge_loader.validate_file_security

FORBIDDEN_DIRECTIVES = [
    "dynaconf_include",
    "dynaconf_includes",
    "includes",
    "preload",
    "preload_for_dynaconf",
    "preloads",
    "dynaconf_merge",
    "dynaconf_merge_enabled",
    "merge_enabled",
    "loaders",
    "loaders_for_dynaconf",
    "core_loaders",
    "core_loaders_for_dynaconf",
    "settings_module",
    "settings_file_for_dynaconf",
    "settings_files_for_dynaconf",
    "envvar_prefix",
    "envvar_prefix_for_dynaconf",
]


class FakeDynaconf:
    """Minimal Dynaconf-like object exposing settings_files and a .set() recorder."""

    def __init__(self, settings_files, includes=None, preload=None):
        self.settings_files = settings_files
        if includes is not None:
            self.includes = includes
        if preload is not None:
            self.preload = preload
        self._store = {}

    def set(self, key, value):
        self._store[key] = value


# ---------------------------------------------------------------------------
# validate_file_security: forbidden directives at varying positions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("directive", FORBIDDEN_DIRECTIVES)
def test_forbidden_directive_at_top_level_raises(directive):
    data = {directive: "anything"}
    with pytest.raises(SecurityError):
        validate_file_security(data, "test.toml")


@pytest.mark.parametrize("directive", FORBIDDEN_DIRECTIVES)
def test_forbidden_directive_inside_section_raises(directive):
    data = {"config": {"some_key": "ok", directive: "bad"}}
    with pytest.raises(SecurityError):
        validate_file_security(data, "test.toml")


@pytest.mark.parametrize("directive", FORBIDDEN_DIRECTIVES)
def test_forbidden_directive_deeply_nested_raises(directive):
    data = {
        "config": {
            "subsection": {
                "deeper": {
                    "evendeeper": {directive: True},
                },
            },
        },
    }
    with pytest.raises(SecurityError):
        validate_file_security(data, "test.toml")


@pytest.mark.parametrize("directive", FORBIDDEN_DIRECTIVES)
def test_forbidden_directive_mixed_case_raises(directive):
    # The implementation lowercases keys before comparison; ensure mixed case is caught.
    mixed = directive.upper() if directive.islower() else directive.swapcase()
    # Ensure case is actually mixed/different
    if mixed == directive:
        mixed = directive.upper()
    data = {"config": {mixed: "bad"}}
    with pytest.raises(SecurityError):
        validate_file_security(data, "test.toml")


# ---------------------------------------------------------------------------
# validate_file_security: max depth guard
# ---------------------------------------------------------------------------

def test_excessive_nesting_raises_security_error():
    # Build a dict deeper than MAX_DEPTH (50) so the guard trips.
    data = current = {}
    for _ in range(120):
        nxt = {}
        current["nested"] = nxt
        current = nxt
    current["leaf"] = "value"
    with pytest.raises(SecurityError):
        validate_file_security(data, "deep.toml")


# ---------------------------------------------------------------------------
# validate_file_security: representative safe PR-Agent config does not raise
# ---------------------------------------------------------------------------

def test_safe_pr_agent_config_does_not_raise():
    data = {
        "config": {
            "model": "gpt-4",
            "fallback_models": ["gpt-3.5-turbo"],
            "git_provider": "github",
            "publish_output": True,
            "verbosity_level": 0,
        },
        "pr_reviewer": {
            "require_score_review": False,
            "num_code_suggestions": 4,
            "extra_instructions": "",
        },
        "pr_description": {
            "publish_labels": True,
            "add_original_user_description": True,
        },
        "github": {
            "deployment_type": "user",
            "ratelimit_retries": 5,
        },
    }
    # Should not raise.
    validate_file_security(data, "safe.toml")


# ---------------------------------------------------------------------------
# load(): behavior tests using a minimal fake Dynaconf-like object
# ---------------------------------------------------------------------------

def _write(tmp_path, name, content):
    p = Path(tmp_path) / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_load_skips_non_toml_files(tmp_path):
    non_toml = _write(tmp_path, "settings.yaml", "config:\n  model: foo\n")
    obj = FakeDynaconf(settings_files=[non_toml])
    load(obj)
    assert obj._store == {}


def test_load_skips_missing_files(tmp_path):
    missing = str(Path(tmp_path) / "does_not_exist.toml")
    obj = FakeDynaconf(settings_files=[missing])
    load(obj)
    assert obj._store == {}


def test_load_silent_true_skips_on_forbidden_directive(tmp_path):
    bad = _write(
        tmp_path,
        "bad.toml",
        "[config]\nmodel = \"gpt-4\"\ndynaconf_include = [\"other.toml\"]\n",
    )
    obj = FakeDynaconf(settings_files=[bad])
    # silent=True: exception is swallowed; no values should be set
    load(obj, silent=True)
    assert obj._store == {}


def test_load_silent_false_raises_on_forbidden_directive(tmp_path):
    bad = _write(
        tmp_path,
        "bad.toml",
        "[config]\nmodel = \"gpt-4\"\nincludes = [\"other.toml\"]\n",
    )
    obj = FakeDynaconf(settings_files=[bad])
    with pytest.raises(SecurityError):
        load(obj, silent=False)


def test_load_silent_false_raises_on_top_level_includes_attr(tmp_path):
    # The loader also checks the object's own .includes attribute.
    good = _write(tmp_path, "ok.toml", "[config]\nmodel = \"gpt-4\"\n")
    obj = FakeDynaconf(settings_files=[good], includes=["something.toml"])
    with pytest.raises(SecurityError):
        load(obj, silent=False)


def test_load_silent_false_raises_on_top_level_preload_attr(tmp_path):
    good = _write(tmp_path, "ok.toml", "[config]\nmodel = \"gpt-4\"\n")
    obj = FakeDynaconf(settings_files=[good], preload=["something.toml"])
    with pytest.raises(SecurityError):
        load(obj, silent=False)


def test_load_valid_toml_sets_expected_sections(tmp_path):
    a = _write(
        tmp_path,
        "a.toml",
        "[config]\nmodel = \"gpt-4\"\nverbosity_level = 1\n\n[pr_reviewer]\nnum_code_suggestions = 4\n",
    )
    obj = FakeDynaconf(settings_files=[a])
    load(obj)
    assert "config" in obj._store
    assert obj._store["config"]["model"] == "gpt-4"
    assert obj._store["config"]["verbosity_level"] == 1
    assert "pr_reviewer" in obj._store
    assert obj._store["pr_reviewer"]["num_code_suggestions"] == 4


def test_load_respects_single_key_loading(tmp_path):
    a = _write(
        tmp_path,
        "a.toml",
        "[config]\nmodel = \"gpt-4\"\n\n[pr_reviewer]\nnum_code_suggestions = 4\n",
    )
    obj = FakeDynaconf(settings_files=[a])
    # key matching is case-insensitive in the loader
    load(obj, key="CONFIG")
    assert "config" in obj._store
    assert "pr_reviewer" not in obj._store


def test_load_later_file_replaces_earlier_field(tmp_path):
    a = _write(tmp_path, "a.toml", "[config]\nmodel = \"gpt-4\"\nshared = \"from_a\"\n")
    b = _write(tmp_path, "b.toml", "[config]\nshared = \"from_b\"\n")
    obj = FakeDynaconf(settings_files=[a, b])
    load(obj)
    assert obj._store["config"]["shared"] == "from_b"
    # earlier-only field is preserved (accumulated, not replaced wholesale at section level)
    assert obj._store["config"]["model"] == "gpt-4"
