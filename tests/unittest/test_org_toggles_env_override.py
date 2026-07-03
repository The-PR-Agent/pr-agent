"""Env-override tests for the two org-MR toggles (CFG-06).

These tests prove that the two `[pr_description]` toggles introduced in
Plan 01-01 (`enable_conventional_title`, `enable_org_template`) can be
flipped from the environment via PR-Agent's existing dynaconf `env_loader`
using the `SECTION__KEY` double-underscore convention with no prefix
(because `config_loader.py` builds Dynaconf with `envvar_prefix=False`).

Test isolation landmine (see 01-RESEARCH.md section 7): dynaconf binds
environment variables at `Dynaconf(...)` construction time. The
module-level `global_settings` singleton is built when `pr_agent.config_loader`
is first imported, so a `monkeypatch.setenv` performed later cannot affect
that singleton. To exercise the env override path faithfully, each test
constructs a FRESH `Dynaconf` mirroring `config_loader.py`'s `dynconf_kwargs`,
pointed at the real `pr_agent/settings/configuration.toml`.
"""

from os.path import join

import pytest
from dynaconf import Dynaconf

import pr_agent.config_loader as config_loader


def _build_fresh_dynaconf() -> Dynaconf:
    """Mirror the exact `Dynaconf(...)` construction from `config_loader.py`
    but only load `configuration.toml` — that is the file the two org-MR
    toggles live in, and using only that keeps the test cheap.

    Kwargs must match `config_loader.dynconf_kwargs` + the singleton's
    top-level kwargs so we exercise the same env-loader wiring:
        - `core_loaders=[]`  (disable default loaders)
        - `loaders=['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader']`
        - `merge_enabled=True`
        - `envvar_prefix=False`  (so `SECTION__KEY` maps directly, no prefix)
        - `load_dotenv=False`    (env vars must be set in the real process env)
    """
    settings_file = join(config_loader.current_dir, "settings", "configuration.toml")
    return Dynaconf(
        envvar_prefix=False,
        load_dotenv=False,
        settings_files=[settings_file],
        **config_loader.dynconf_kwargs,
    )


def test_defaults_are_false_without_env_vars(monkeypatch):
    """Control assertion: without either env var set, a fresh Dynaconf
    reads both toggles as False. This proves the defaults from
    `configuration.toml` are what the env-override tests below are
    actually overriding."""
    monkeypatch.delenv("PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE", raising=False)
    monkeypatch.delenv("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", raising=False)

    settings = _build_fresh_dynaconf()

    assert settings.pr_description.enable_conventional_title is False
    assert settings.pr_description.enable_org_template is False


def test_env_var_flips_enable_conventional_title(monkeypatch):
    """CFG-06 (a): setting `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true`
    in the process environment before constructing Dynaconf flips the toggle
    to `True` through the existing env_loader — no new plumbing required."""
    monkeypatch.setenv("PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE", "true")
    # Isolate: ensure the sibling env var is not carried over from a
    # previous test / shell.
    monkeypatch.delenv("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", raising=False)

    settings = _build_fresh_dynaconf()

    assert settings.pr_description.enable_conventional_title is True
    # Sibling toggle must NOT be flipped as a side effect
    assert settings.pr_description.enable_org_template is False


def test_env_var_flips_enable_org_template(monkeypatch):
    """CFG-06 (b): setting `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true`
    in the process environment before constructing Dynaconf flips the toggle
    to `True` through the existing env_loader — no new plumbing required."""
    monkeypatch.setenv("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", "true")
    monkeypatch.delenv("PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE", raising=False)

    settings = _build_fresh_dynaconf()

    assert settings.pr_description.enable_org_template is True
    # Sibling toggle must NOT be flipped as a side effect
    assert settings.pr_description.enable_conventional_title is False


@pytest.mark.parametrize(
    "env_name,attr_name",
    [
        ("PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE", "enable_conventional_title"),
        ("PR_DESCRIPTION__ENABLE_ORG_TEMPLATE", "enable_org_template"),
    ],
)
def test_env_var_case_variants_flip_toggle(monkeypatch, env_name, attr_name):
    """Dynaconf coerces boolean-literal strings ("true", "True") to the
    boolean `True`. Cover the case variants an operator might realistically
    set in a CI pipeline so regressions in the coercion path are caught.

    Deliberately NOT covering "1" — dynaconf coerces integer-literal strings
    to `int`, not `bool`, so `PR_DESCRIPTION__ENABLE_*=1` yields the value
    `1` (truthy but not `is True`). The `.get(key, False)` read pattern used
    at the call sites (per plan 01-01) would still short-circuit correctly,
    but asserting `is True` here would fail. Document the boolean-literal
    contract explicitly instead."""
    for value in ("true", "True"):
        monkeypatch.setenv(env_name, value)

        settings = _build_fresh_dynaconf()

        assert getattr(settings.pr_description, attr_name) is True, (
            f"env var {env_name}={value!r} must flip {attr_name} to True"
        )
