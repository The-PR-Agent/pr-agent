"""config__* env-var access for the fork describe toggles (Phase 4).

These tests prove the fork toggles can be flipped via the legacy `CONFIG__*`
env-var style IN ADDITION to the existing `PR_DESCRIPTION__*` style, and that
`CONFIG__USE_DESCRIPTION_MARKERS` is mirrored into `pr_description.use_description_markers`
so the upstream marker-vs-normal branch (pr_description.py:358, :453) sees it
without editing upstream code.

Isolation landmine (same as test_org_toggles_env_override.py): dynaconf binds
environment variables at `Dynaconf(...)` construction time, so each test builds
a FRESH `Dynaconf` mirroring `config_loader.dynconf_kwargs`, pointed at the real
`pr_agent/settings/configuration.toml`.
"""

from os.path import join

from dynaconf import Dynaconf

import pr_agent.config_loader as config_loader
from pr_agent.config_loader import _mirror_fork_config_keys
from pr_agent.tools.pr_description import _fork_toggle


def _build_fresh_dynaconf() -> Dynaconf:
    """Mirror `config_loader.py`'s `Dynaconf(...)` construction, loading only
    `configuration.toml` (the file the fork toggles live in)."""
    settings_file = join(config_loader.current_dir, "settings", "configuration.toml")
    return Dynaconf(
        envvar_prefix=False,
        load_dotenv=False,
        settings_files=[settings_file],
        **config_loader.dynconf_kwargs,
    )


def _clean_env(monkeypatch):
    """Remove every toggle env var so each test starts from a known baseline."""
    for key in (
        "CONFIG__ENABLE_ORG_TEMPLATE",
        "CONFIG__ENABLE_CONVENTIONAL_TITLE",
        "CONFIG__ENABLE_PR_AGENT_OUTPUT",
        "CONFIG__USE_DESCRIPTION_MARKERS",
        "PR_DESCRIPTION__ENABLE_ORG_TEMPLATE",
        "PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_config_env_flips_enable_org_template(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONFIG__ENABLE_ORG_TEMPLATE", "true")

    settings = _build_fresh_dynaconf()

    # Lands in [config] (the SECTION__KEY convention maps CONFIG__ to [config]).
    assert settings.config.enable_org_template is True
    # [pr_description] is untouched when only CONFIG__ is set.
    assert settings.pr_description.enable_org_template is False


def test_config_env_flips_enable_conventional_title(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONFIG__ENABLE_CONVENTIONAL_TITLE", "true")

    settings = _build_fresh_dynaconf()

    assert settings.config.enable_conventional_title is True
    assert settings.pr_description.enable_conventional_title is False


def test_config_env_sets_enable_pr_agent_output_false(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONFIG__ENABLE_PR_AGENT_OUTPUT", "false")

    settings = _build_fresh_dynaconf()

    assert settings.config.enable_pr_agent_output is False


def test_config_env_wins_over_pr_description(monkeypatch):
    """When both CONFIG__ and PR_DESCRIPTION__ set the same toggle, config.*
    (the legacy prefix) wins through the dual-read helper."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONFIG__ENABLE_ORG_TEMPLATE", "true")
    monkeypatch.setenv("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", "false")

    settings = _build_fresh_dynaconf()

    assert settings.config.enable_org_template is True
    assert settings.pr_description.enable_org_template is False
    # The fork's dual-read helper picks config.* first.
    assert _fork_toggle("enable_org_template", False, settings=settings) is True


def test_pr_description_env_still_flips_toggle(monkeypatch):
    """Backward compatibility: PR_DESCRIPTION__* still flips the toggle through
    the dual-read helper's pr_description.* fallback."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", "true")

    settings = _build_fresh_dynaconf()

    assert settings.pr_description.enable_org_template is True
    assert _fork_toggle("enable_org_template", False, settings=settings) is True


def test_fork_toggle_falls_through_when_neither_set(monkeypatch):
    _clean_env(monkeypatch)

    settings = _build_fresh_dynaconf()

    # No env override -> default false from [pr_description].
    assert _fork_toggle("enable_org_template", False, settings=settings) is False
    assert _fork_toggle("enable_conventional_title", False, settings=settings) is False
    assert _fork_toggle("enable_pr_agent_output", False, settings=settings) is False


def test_mirror_copies_config_use_description_markers(monkeypatch):
    """CONFIG__USE_DESCRIPTION_MARKERS is mirrored into pr_description so the
    upstream marker-vs-normal branch (which reads pr_description.* directly)
    sees the legacy-style override."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONFIG__USE_DESCRIPTION_MARKERS", "false")

    settings = _build_fresh_dynaconf()

    assert settings.config.use_description_markers is False
    # Mirror is idempotent and only fires when [config] value is explicitly set.
    _mirror_fork_config_keys(settings)
    assert settings.pr_description.use_description_markers is False


def test_mirror_noop_when_config_unset(monkeypatch):
    """When CONFIG__USE_DESCRIPTION_MARKERS is NOT set, the mirror must NOT
    overwrite pr_description.use_description_markers (preserve the TOML/env value)."""
    _clean_env(monkeypatch)

    settings = _build_fresh_dynaconf()
    before = settings.pr_description.use_description_markers

    _mirror_fork_config_keys(settings)

    assert settings.pr_description.use_description_markers == before
