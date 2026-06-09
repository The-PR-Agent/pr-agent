from pr_agent.git_providers.utils import handle_configurations_errors


class FakeMarkdownProvider:
    def __init__(self):
        self.persistent_comments = []

    def is_supported(self, capability):
        return capability == "gfm_markdown"

    def publish_persistent_comment(self, body, initial_header, update_header, final_update_message):
        self.persistent_comments.append({
            "body": body,
            "initial_header": initial_header,
            "update_header": update_header,
            "final_update_message": final_update_message,
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
            "settings": b"[pr_reviewer]\nnum_max_findings =",
            "error": "Second error",
            "category": "global",
        },
    ], provider)

    assert len(provider.comments) == 2
    assert "First error" in provider.comments[0]
    assert "[config]\nmodel =" in provider.comments[0]
    assert "Second error" in provider.comments[1]
    assert "[pr_reviewer]\nnum_max_findings =" in provider.comments[1]


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
