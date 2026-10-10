import copy

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils
from pr_agent.git_providers.utils import apply_repo_settings, handle_configurations_errors


class FakeMarkdownProvider:
    def __init__(self):
        self.persistent_comments = []

    def is_supported(self, capability):
        return capability == "gfm_markdown"

    def publish_persistent_comment(self, body, initial_header, update_header, final_update_message, name='review'):
        self.persistent_comments.append({
            "body": body,
            "initial_header": initial_header,
            "update_header": update_header,
            "final_update_message": final_update_message,
            "name": name,
        })


class FakePlainProvider:
    def __init__(self):
        self.comments = []

    def is_supported(self, capability):
        return False

    def publish_comment(self, body):
        self.comments.append(body)


class FakeMarkdownCommentProvider(FakePlainProvider):
    def is_supported(self, capability):
        return capability == "gfm_markdown"


class FakeSettingsProvider:
    def get_repo_settings(self):
        return [
            ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\nenable_intro_text = false\n"),
            ("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n"),
        ]


def test_apply_repo_settings_merges_global_before_local_settings(monkeypatch):
    settings = get_settings()
    original_extra_instructions = settings.pr_reviewer.extra_instructions
    original_enable_intro_text = settings.pr_reviewer.enable_intro_text
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda pr_url: FakeSettingsProvider())
    monkeypatch.delenv("AUTO_CAST_FOR_DYNACONF", raising=False)

    try:
        apply_repo_settings("https://github.example.com/org/service/pull/1")

        assert settings.pr_reviewer.extra_instructions == "local"
        assert settings.pr_reviewer.enable_intro_text is False
    finally:
        settings.pr_reviewer.extra_instructions = original_extra_instructions
        settings.pr_reviewer.enable_intro_text = original_enable_intro_text


class FakeErrorReportingProvider(FakePlainProvider):
    def __init__(self, settings_list):
        super().__init__()
        self._settings_list = settings_list

    def get_repo_settings(self):
        return self._settings_list


def test_apply_repo_settings_attributes_global_error_and_still_applies_local(monkeypatch):
    # A malformed global settings file must (a) actually surface an error rather than being silently
    # swallowed by the loader, (b) be reported against the *global* scope with content redacted, and
    # (c) not prevent a valid local file from taking effect — sources are applied independently.
    settings = get_settings()
    original_extra_instructions = settings.pr_reviewer.extra_instructions
    provider = FakeErrorReportingProvider([
        ("global", b"[unclosed_section\n"),  # malformed TOML
        ("local", b"[pr_reviewer]\nextra_instructions = \"local-applied\"\n"),
    ])
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.delenv("AUTO_CAST_FOR_DYNACONF", raising=False)

    try:
        apply_repo_settings("https://github.example.com/org/service/pull/1")

        # Local settings still applied despite the global parse error.
        assert settings.pr_reviewer.extra_instructions == "local-applied"
        # One error comment, attributed to global, with the global content redacted (not echoed).
        assert len(provider.comments) == 1
        assert "global settings repository (`config.global_settings_repo`)" in provider.comments[0]
        assert "unclosed_section" not in provider.comments[0]
    finally:
        settings.pr_reviewer.extra_instructions = original_extra_instructions


def test_handle_configurations_errors_uses_persistent_comment_when_supported():
    provider = FakeMarkdownProvider()

    handle_configurations_errors([{
        "settings": b"[config]\nmodel =",
        "error": "Invalid value",
        "category": "local",
    }], provider)

    assert len(provider.persistent_comments) == 1
    comment = provider.persistent_comments[0]
    assert comment["initial_header"] == "❌ **PR-Agent failed to apply 'local' repo settings**"
    assert comment["update_header"] is False
    assert comment["final_update_message"] is False
    assert "PR-Agent failed to apply 'local' repo settings" in comment["body"]
    assert "Invalid value" in comment["body"]
    assert "https://docs.pr-agent.ai/usage-guide/configuration_options/" in comment["body"]
    assert "qodo-merge-docs.qodo.ai" not in comment["body"]
    assert "```toml\n[config]\nmodel =\n```" in comment["body"]
    assert "<details><summary>Configuration content:</summary>" in comment["body"]


def test_handle_configurations_errors_keeps_markdown_details_when_persistent_comment_is_missing():
    provider = FakeMarkdownCommentProvider()

    handle_configurations_errors([{
        "settings": b"[config]\nmodel =",
        "error": "Invalid value",
        "category": "local",
    }], provider)

    assert len(provider.comments) == 1
    assert "PR-Agent failed to apply 'local' repo settings" in provider.comments[0]
    assert "Invalid value" in provider.comments[0]
    assert "```toml\n[config]\nmodel =\n```" in provider.comments[0]
    assert "<details><summary>Configuration content:</summary>" in provider.comments[0]


def test_handle_configurations_errors_uses_plain_comment_without_markdown_support():
    provider = FakePlainProvider()

    handle_configurations_errors([{
        "settings": b"[config]\nmodel =",
        "error": "Invalid value",
        "category": "local",
    }], provider)

    assert len(provider.comments) == 1
    assert "❌ **PR-Agent failed to apply 'local' repo settings**" in provider.comments[0]
    assert "Invalid value" in provider.comments[0]
    assert "```toml\n[config]\nmodel =\n```" in provider.comments[0]
    assert "<details>" not in provider.comments[0]


def test_handle_configurations_errors_returns_without_errors():
    provider = FakePlainProvider()

    handle_configurations_errors([], provider)

    assert provider.comments == []


def test_handle_configurations_errors_publishes_each_error():
    provider = FakePlainProvider()

    handle_configurations_errors([
        {
            "settings": b"[config]\nmodel =",
            "error": "First error",
            "category": "local",
        },
        {
            "settings": b"[github]\nuser_token = \"dummy-value\"\n[pr_reviewer]\nnum_max_findings =",
            "error": "Second error",
            "category": "global",
        },
    ], provider)

    assert len(provider.comments) == 2
    assert "First error" in provider.comments[0]
    assert "[config]\nmodel =" in provider.comments[0]
    assert "Second error" not in provider.comments[1]
    assert "Invalid shared configuration" in provider.comments[1]
    assert "global settings repository (`config.global_settings_repo`)" in provider.comments[1]
    assert "dummy-value" not in provider.comments[1]
    assert "num_max_findings" not in provider.comments[1]


def test_apply_repo_settings_file_skips_oversized_file(monkeypatch, tmp_path):
    # An oversized settings file must be skipped by size BEFORE it is parsed, so it can't be
    # fully read/parsed in-process (the explicit validation would otherwise bypass the loader cap).
    from unittest.mock import MagicMock

    f = tmp_path / "big.toml"
    f.write_bytes(b"[pr_reviewer]\nnum_max_findings = 3\n")
    monkeypatch.setattr(utils, "MAX_TOML_SIZE_IN_BYTES", 5)  # smaller than the file
    load_spy = MagicMock(side_effect=AssertionError("oversized file must not be parsed"))
    monkeypatch.setattr(utils.tomllib, "load", load_spy)

    # Returns quietly (skips) without parsing or raising.
    utils._apply_repo_settings_file(str(f))
    load_spy.assert_not_called()


def test_handle_configurations_errors_tolerates_non_utf8_settings():
    # Non-UTF-8 settings bytes must not raise (UnicodeDecodeError) and abort posting the error.
    provider = FakePlainProvider()

    handle_configurations_errors([
        {"settings": b"\xff\xfe[config]\nmodel =", "error": "bad config", "category": "local"},
    ], provider)

    assert len(provider.comments) == 1
    assert "bad config" in provider.comments[0]


def test_handle_configurations_errors_uses_unique_name_per_scope():
    # In GitHub check-run mode the persistent-comment `name` keys the check run, so multiple
    # settings errors (global + local) must use distinct names or later ones overwrite earlier ones.
    provider = FakeMarkdownProvider()

    handle_configurations_errors([
        {"settings": b"[config]\nmodel =", "error": "global error", "category": "global"},
        {"settings": b"[config]\nmodel =", "error": "local error", "category": "local"},
    ], provider)

    names = [c["name"] for c in provider.persistent_comments]
    assert names == ["config-errors-global", "config-errors-local"]
    assert len(set(names)) == 2


def test_handle_configurations_errors_ignores_empty_sentinel_entry():
    provider = FakePlainProvider()

    handle_configurations_errors([None], provider)

    assert provider.comments == []


def test_handle_configurations_errors_skips_empty_sentinel_entries_in_mixed_list():
    provider = FakePlainProvider()

    handle_configurations_errors([
        None,
        {
            "settings": b"[config]\nmodel =",
            "error": "Only error",
            "category": "local",
        },
    ], provider)

    assert len(provider.comments) == 1
    assert "Only error" in provider.comments[0]


def test_get_cached_global_settings_skips_oversized_values(monkeypatch):
    from pr_agent.git_providers import git_provider as gp
    gp._GLOBAL_SETTINGS_CACHE.clear()
    monkeypatch.setattr(gp, "_GLOBAL_SETTINGS_CACHE_MAX_VALUE_BYTES", 4)
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return b"way too big for the cap"

    try:
        gp.get_cached_global_settings("k", fetch)
        gp.get_cached_global_settings("k", fetch)  # oversized -> not cached, fetched again
        assert calls["n"] == 2
        assert "k" not in gp._GLOBAL_SETTINGS_CACHE
    finally:
        gp._GLOBAL_SETTINGS_CACHE.clear()


def test_get_cached_global_settings_caches_small_values():
    from pr_agent.git_providers import git_provider as gp
    gp._GLOBAL_SETTINGS_CACHE.clear()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return b"tiny"

    try:
        gp.get_cached_global_settings("k2", fetch)
        gp.get_cached_global_settings("k2", fetch)  # cached -> not fetched again
        assert calls["n"] == 1
    finally:
        gp._GLOBAL_SETTINGS_CACHE.clear()


def test_get_cached_global_settings_does_not_cache_fetch_errors():
    from pr_agent.git_providers import git_provider as gp
    gp._GLOBAL_SETTINGS_CACHE.clear()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        raise RuntimeError("transient 500")

    try:
        assert gp.get_cached_global_settings("k3", fetch) == ""  # error -> "" (not cached)
        assert gp.get_cached_global_settings("k3", fetch) == ""  # retried, not served from cache
        assert calls["n"] == 2
        assert "k3" not in gp._GLOBAL_SETTINGS_CACHE
    finally:
        gp._GLOBAL_SETTINGS_CACHE.clear()


@pytest.mark.parametrize("override", [None, "local"])
def test_apply_repo_settings_merges_global_group_and_local(monkeypatch, override):
    settings = get_settings()
    original_config = copy.deepcopy(settings.as_dict()["PR_REVIEWER"])
    local = '[pr_reviewer]\nnum_max_findings = 7\n'
    if override is not None:
        local += f'extra_instructions = "{override}"\n'
    provider = FakeErrorReportingProvider([
        ("global", b'[pr_reviewer]\nextra_instructions = "global"\nenable_intro_text = false\n'),
        ("group", b'[pr_reviewer]\nextra_instructions = "group"\nnum_max_findings = 3\n'),
        ("local", local.encode()),
    ])
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.delenv("AUTO_CAST_FOR_DYNACONF", raising=False)
    try:
        apply_repo_settings("https://gitlab.example.com/org/team/service/-/merge_requests/1")
        assert settings.pr_reviewer.extra_instructions == (override or "group")
        assert settings.pr_reviewer.num_max_findings == 7
        assert settings.pr_reviewer.enable_intro_text is False
        assert provider.comments == []
    finally:
        settings.unset("pr_reviewer")
        settings.set("pr_reviewer", original_config, merge=False)


@pytest.mark.parametrize("provider_cls", [FakeMarkdownProvider, FakePlainProvider])
def test_group_settings_error_does_not_publish_shared_content(provider_cls):
    provider = provider_cls()
    handle_configurations_errors([{
        "settings": b'[config]\nmodel = "private-subgroup-value"\n',
        "error": "Invalid TOML",
        "category": "group",
    }], provider)
    if isinstance(provider, FakeMarkdownProvider):
        comment = provider.persistent_comments[0]
        assert comment["name"] == "config-errors-group"
        body = comment["body"]
    else:
        body = provider.comments[0]
    assert "group settings repository (`config.global_settings_repo`)" in body
    assert "private-subgroup-value" not in body
    assert "Configuration content" not in body


def test_malformed_group_settings_does_not_block_global_or_local(monkeypatch):
    settings = get_settings()
    original_config = copy.deepcopy(settings.as_dict()["PR_REVIEWER"])
    provider = FakeErrorReportingProvider([
        ("global", b'[pr_reviewer]\nenable_intro_text = false\n'),
        ("group", b'[private_group_section\n'),
        ("local", b'[pr_reviewer]\nextra_instructions = "local-applied"\n'),
    ])
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.delenv("AUTO_CAST_FOR_DYNACONF", raising=False)
    try:
        apply_repo_settings("https://gitlab.example.com/org/team/service/-/merge_requests/1")
        assert settings.pr_reviewer.enable_intro_text is False
        assert settings.pr_reviewer.extra_instructions == "local-applied"
        assert len(provider.comments) == 1
        assert "group settings repository" in provider.comments[0]
        assert "private_group_section" not in provider.comments[0]
    finally:
        settings.unset("pr_reviewer")
        settings.set("pr_reviewer", original_config, merge=False)


@pytest.mark.parametrize("category", ["global", "group"])
def test_shared_settings_parse_error_redacts_private_table_names(monkeypatch, category):
    settings = get_settings()
    original_config = copy.deepcopy(settings.as_dict()["PR_REVIEWER"])
    provider = FakeErrorReportingProvider([
        (category, b"[private_customer_name]\n[private_customer_name]\n"),
        ("local", b'[pr_reviewer]\nextra_instructions = "local-applied"\n'),
    ])
    monkeypatch.setattr(utils, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.delenv("AUTO_CAST_FOR_DYNACONF", raising=False)
    try:
        apply_repo_settings("https://gitlab.example.com/org/team/service/-/merge_requests/1")
        assert settings.pr_reviewer.extra_instructions == "local-applied"
        assert len(provider.comments) == 1
        assert f"{category} settings repository" in provider.comments[0]
        assert "Invalid shared configuration" in provider.comments[0]
        assert "private_customer_name" not in provider.comments[0]
        assert "Cannot declare" not in provider.comments[0]
    finally:
        settings.unset("pr_reviewer")
        settings.set("pr_reviewer", original_config, merge=False)
