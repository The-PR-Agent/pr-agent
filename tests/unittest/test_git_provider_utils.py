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
        assert "organization's global settings file" in provider.comments[0]
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
    assert "Second error" in provider.comments[1]
    assert "organization's global settings file" in provider.comments[1]
    assert "dummy-value" not in provider.comments[1]
    assert "num_max_findings" not in provider.comments[1]


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
