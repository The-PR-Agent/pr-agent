"""
Security-focused tests for pr_agent.git_providers.utils.apply_repo_settings.

These tests verify:
- The repo settings fetch path is skipped when use_repo_settings_file is disabled.
- Valid repo TOML overrides only the specified keys and preserves siblings.
- Invalid TOML produces exactly one local-category configuration error and
  does not pollute global settings.
- Forbidden directives (e.g. dynaconf_include) are rejected and produce a
  local-category configuration error without polluting settings.
- The temporary file created from the repo settings bytes is removed after
  apply_repo_settings, both on success and on failure.
"""

import copy
import os
import tempfile
from contextlib import suppress

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.git_providers.utils import apply_repo_settings


class FakeGitProvider:
    """Minimal fake provider exposing the methods apply_repo_settings touches."""

    def __init__(self, repo_settings_bytes=b""):
        self._repo_settings = repo_settings_bytes
        self.persistent_comments = []
        self.comments = []
        self.get_repo_settings_calls = 0

    def get_repo_settings(self):
        self.get_repo_settings_calls += 1
        return self._repo_settings

    def is_supported(self, capability):
        return capability == "gfm_markdown"

    def publish_persistent_comment(self, body, initial_header, update_header, final_update_message):
        self.persistent_comments.append(
            {
                "body": body,
                "initial_header": initial_header,
                "update_header": update_header,
                "final_update_message": final_update_message,
            }
        )

    def publish_comment(self, body):
        self.comments.append(body)


SNAPSHOT_SECTIONS = ("CONFIG", "PR_REVIEWER", "CUSTOM_SECTION_FOR_TEST")


def _snapshot_settings_sections(settings):
    return {section: copy.deepcopy(settings.as_dict().get(section)) for section in SNAPSHOT_SECTIONS}


def _restore_settings_sections(settings, snapshot):
    for section, data in snapshot.items():
        # ``unset`` raises ``KeyError`` if the section was never set during
        # the test; that's expected and safe to ignore. Anything else (e.g.
        # a Dynaconf internal error) should propagate so a broken teardown
        # surfaces instead of silently leaking state into other tests.
        with suppress(KeyError):
            settings.unset(section, force=True)
        if data is not None:
            settings.set(section, copy.deepcopy(data), merge=False)


_ENV_ABSENT = object()


@pytest.fixture
def settings_snapshot():
    """Snapshot the keys mutated by these tests and restore them afterwards.

    Also snapshots the ``AUTO_CAST_FOR_DYNACONF`` environment variable, which
    ``apply_repo_settings`` unconditionally sets to ``"false"``. Using a
    sentinel for "originally absent" ensures the env restore is exact:
    keys that were absent are deleted, never left as a stray ``None``-like
    string that could leak into other tests.
    """
    settings = get_settings()
    snapshot = _snapshot_settings_sections(settings)
    env_before = os.environ.get("AUTO_CAST_FOR_DYNACONF", _ENV_ABSENT)
    try:
        yield
    finally:
        _restore_settings_sections(settings, snapshot)
        if env_before is _ENV_ABSENT:
            os.environ.pop("AUTO_CAST_FOR_DYNACONF", None)
        else:
            os.environ["AUTO_CAST_FOR_DYNACONF"] = env_before


def _install_provider(monkeypatch, provider):
    captured = {"errors": None, "git_provider": None}

    def fake_get_git_provider_with_context(pr_url):
        return provider

    def fake_handle_configurations_errors(errors, git_provider):
        captured["errors"] = errors
        captured["git_provider"] = git_provider

    monkeypatch.setattr(git_utils, "get_git_provider_with_context", fake_get_git_provider_with_context)
    monkeypatch.setattr(git_utils, "handle_configurations_errors", fake_handle_configurations_errors)
    return captured


def test_disabled_repo_settings_skips_provider_fetch(monkeypatch, settings_snapshot):
    provider = FakeGitProvider(repo_settings_bytes=b"[pr_reviewer]\nnum_max_findings = 99\n")
    captured = _install_provider(monkeypatch, provider)

    get_settings().set("config.use_repo_settings_file", False)
    original_num = get_settings().as_dict().get("PR_REVIEWER", {}).get("num_max_findings")

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert provider.get_repo_settings_calls == 0
    assert captured["errors"] is None
    # Settings were not touched.
    assert get_settings().as_dict().get("PR_REVIEWER", {}).get("num_max_findings") == original_num


def _section(settings, name):
    """Return a section dict from settings using a case-insensitive lookup."""
    data = settings.as_dict()
    upper = name.upper()
    for key, value in data.items():
        if key.upper() == upper:
            return value if isinstance(value, dict) else {}
    return {}


def test_valid_repo_settings_merge_overrides_key_and_preserves_siblings(monkeypatch, settings_snapshot):
    provider = FakeGitProvider(repo_settings_bytes=b"[pr_reviewer]\nnum_max_findings = 11\n")
    captured = _install_provider(monkeypatch, provider)

    get_settings().set("config.use_repo_settings_file", True)
    settings = get_settings()
    sibling_before = _section(settings, "pr_reviewer").get("require_tests_review")
    assert sibling_before is not None, "Test precondition: sibling key should already be present"

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert provider.get_repo_settings_calls == 1
    assert captured["errors"] is None, f"Unexpected configuration errors: {captured['errors']}"

    pr_reviewer = _section(settings, "pr_reviewer")
    assert pr_reviewer.get("num_max_findings") == 11
    # Unrelated sibling key in the same section must be preserved by the merge logic.
    assert pr_reviewer.get("require_tests_review") == sibling_before


def test_invalid_toml_does_not_pollute_settings(monkeypatch, settings_snapshot):
    """
    Malformed TOML must never leak into the live settings. The custom loader
    currently swallows the TOMLDecodeError and logs it, so no local error is
    propagated to handle_configurations_errors; the surviving security
    guarantee is that the existing settings are untouched.
    """
    malformed = b"[pr_reviewer\nnum_max_findings = 7\n"
    provider = FakeGitProvider(repo_settings_bytes=malformed)
    _install_provider(monkeypatch, provider)

    get_settings().set("config.use_repo_settings_file", True)
    settings = get_settings()
    before = copy.deepcopy(_section(settings, "pr_reviewer"))

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    after = _section(settings, "pr_reviewer")
    assert after == before
    # Whatever errors may or may not be published, the malformed payload must
    # never be merged silently into pr_reviewer.
    assert after.get("num_max_findings") != 7


def test_invalid_toml_publishes_one_local_error(monkeypatch, settings_snapshot):
    malformed = b"[pr_reviewer\nnum_max_findings = 7\n"
    provider = FakeGitProvider(repo_settings_bytes=malformed)
    captured = _install_provider(monkeypatch, provider)
    get_settings().set("config.use_repo_settings_file", True)

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert captured["errors"] is not None
    assert len(captured["errors"]) == 1
    assert captured["errors"][0]["category"] == "local"
    assert captured["errors"][0]["settings"] == malformed


def test_forbidden_directive_does_not_pollute_settings(monkeypatch, settings_snapshot):
    """
    A repo TOML containing forbidden directives (e.g. dynaconf_include) must
    not leak into the live settings. As with malformed TOML, the loader's
    SecurityError is currently swallowed silently; the security guarantee
    checked here is that no part of the payload (including the legitimate
    pr_reviewer override) reaches the settings.
    """
    forbidden_toml = b"dynaconf_include = ['evil.toml']\n[pr_reviewer]\nnum_max_findings = 42\n"
    provider = FakeGitProvider(repo_settings_bytes=forbidden_toml)
    _install_provider(monkeypatch, provider)

    get_settings().set("config.use_repo_settings_file", True)
    settings = get_settings()
    before = copy.deepcopy(_section(settings, "pr_reviewer"))

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    after = _section(settings, "pr_reviewer")
    # The forbidden file must be rejected wholesale; neither the directive
    # nor the piggy-backed pr_reviewer override should be applied.
    assert after == before
    assert after.get("num_max_findings") != 42
    assert "dynaconf_include" not in {k.lower() for k in settings.as_dict().keys()}


def test_forbidden_directive_publishes_one_local_error(monkeypatch, settings_snapshot):
    forbidden_toml = b"dynaconf_include = ['evil.toml']\n[pr_reviewer]\nnum_max_findings = 42\n"
    provider = FakeGitProvider(repo_settings_bytes=forbidden_toml)
    captured = _install_provider(monkeypatch, provider)
    get_settings().set("config.use_repo_settings_file", True)

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert captured["errors"] is not None
    assert len(captured["errors"]) == 1
    assert captured["errors"][0]["category"] == "local"
    assert captured["errors"][0]["settings"] == forbidden_toml
    # The error message must not leak the server's internal temp path to PR users.
    import tempfile
    error_text = captured["errors"][0]["error"]
    assert tempfile.gettempdir() not in error_text
    assert ".pr_agent.toml" in error_text


def test_temp_file_is_removed_after_successful_apply(monkeypatch, tmp_path, settings_snapshot):
    provider = FakeGitProvider(repo_settings_bytes=b"[pr_reviewer]\nnum_max_findings = 5\n")
    _install_provider(monkeypatch, provider)
    get_settings().set("config.use_repo_settings_file", True)

    known_path = tmp_path / "repo_settings_success.toml"

    def fake_mkstemp(suffix=None, prefix=None, dir=None, text=False):
        fd = os.open(str(known_path), os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        return fd, str(known_path)

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert not known_path.exists(), "Temp settings file must be removed after successful apply"


def test_temp_file_is_removed_after_failed_apply(monkeypatch, tmp_path, settings_snapshot):
    """The temp file must be removed even when the Dynaconf load step raises.

    We use *valid* TOML bytes (so the failure cannot be confused with the
    silent-swallow malformed-TOML path) and force the failure by replacing
    the ``Dynaconf`` symbol bound inside ``pr_agent.git_providers.utils``
    with a stub that raises *after* ``mkstemp`` has been called. We do not
    patch the external ``dynaconf`` module — only the imported reference
    that ``apply_repo_settings`` actually uses.
    """
    valid_toml = b"[pr_reviewer]\nnum_max_findings = 3\n"
    provider = FakeGitProvider(repo_settings_bytes=valid_toml)
    captured = _install_provider(monkeypatch, provider)
    get_settings().set("config.use_repo_settings_file", True)

    known_path = tmp_path / "repo_settings_failure.toml"
    mkstemp_calls = {"n": 0}

    def fake_mkstemp(suffix=None, prefix=None, dir=None, text=False):
        mkstemp_calls["n"] += 1
        fd = os.open(str(known_path), os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        return fd, str(known_path)

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)

    def exploding_validate(*args, **kwargs):
        raise RuntimeError("boom")

    # Force a failure during apply (validate_file_security runs after mkstemp/parse).
    monkeypatch.setattr(git_utils, "validate_file_security", exploding_validate)

    apply_repo_settings("https://example.com/owner/repo/pull/1")

    assert mkstemp_calls["n"] == 1, "mkstemp must have run before the failure"
    assert not known_path.exists(), "Temp settings file must be removed even after a failed apply"

    # The local-category configuration error path must have been exercised.
    assert captured["errors"] is not None, "handle_configurations_errors should have been called"
    assert len(captured["errors"]) == 1
    err = captured["errors"][0]
    assert err["category"] == "local"
    assert err["settings"] == valid_toml
    assert "boom" in err["error"]


def test_restore_settings_sections_removes_section_created_after_snapshot():
    settings = get_settings()
    original_snapshot = _snapshot_settings_sections(settings)

    try:
        settings.unset("CUSTOM_SECTION_FOR_TEST", force=True)
        assert "CUSTOM_SECTION_FOR_TEST" not in settings.as_dict()

        snapshot = _snapshot_settings_sections(settings)
        assert snapshot["CUSTOM_SECTION_FOR_TEST"] is None

        settings.set("CUSTOM_SECTION_FOR_TEST", {"foo": "bar"}, merge=False)
        assert settings.as_dict()["CUSTOM_SECTION_FOR_TEST"] == {"foo": "bar"}

        _restore_settings_sections(settings, snapshot)

        assert "CUSTOM_SECTION_FOR_TEST" not in settings.as_dict()
    finally:
        _restore_settings_sections(settings, original_snapshot)
