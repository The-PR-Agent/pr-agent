"""The qdrant collection name is suffixed so pre-#2323 points cannot surface in results."""
import inspect

import pytest

import pr_agent.tools.pr_similar_issue as psi

BASE_INDEX_NAME = "codium-ai-pr-agent-issues"


class FakeSettings:
    """Stand in for the Dynaconf object, honouring the dotted-key get() the helper uses."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture
def settings(monkeypatch):
    def _apply(values):
        monkeypatch.setattr(psi, "get_settings", lambda: FakeSettings(values))

    return _apply


def test_default_suffix_is_applied(settings):
    """With the shipped default the new index lives beside, not on top of, the old collection."""
    settings({"pr_similar_issue.qdrant_collection_suffix": "v2"})

    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == "codium-ai-pr-agent-issues-v2"


def test_missing_setting_still_suffixes(settings):
    """An older user config without the key must not silently fall back to the stale collection."""
    settings({})

    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == "codium-ai-pr-agent-issues-v2"


@pytest.mark.parametrize("suffix", ["", "   ", "-", None])
def test_empty_suffix_keeps_the_original_collection(settings, suffix):
    """Opting out restores the pre-#2323 name for users who already re-indexed by hand."""
    settings({"pr_similar_issue.qdrant_collection_suffix": suffix})

    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == BASE_INDEX_NAME


@pytest.mark.parametrize("suffix", ["v3", "-v3", "v3-", " v3 "])
def test_suffix_is_joined_with_a_single_dash(settings, suffix):
    """A user-supplied dash or stray whitespace must not double up in the collection name."""
    settings({"pr_similar_issue.qdrant_collection_suffix": suffix})

    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == "codium-ai-pr-agent-issues-v3"


def test_suffix_is_scoped_to_qdrant_only():
    """index_name is shared with pinecone and lancedb, so only qdrant call sites may be renamed."""
    source = inspect.getsource(psi)
    qdrant_only_call_sites = [
        "if not self.qdrant.collection_exists(collection_name=self.qdrant_collection_name):",
        "self.qdrant.upsert(collection_name=self.qdrant_collection_name, points=points)",
    ]

    for call_site in qdrant_only_call_sites:
        assert call_site in source

    assert 'index_name = self.index_name = "codium-ai-pr-agent-issues"' in source
    assert "pinecone.Index(index_name=self.index_name)" in source
    assert "self.db.create_table(self.index_name, data=df, mode=\"overwrite\")" in source
    assert "self.qdrant_collection_name" not in source.split("elif get_settings().pr_similar_issue.vectordb == \"qdrant\":")[0]


def test_configuration_toml_ships_the_default():
    """AGENTS.md requires config defaults to live in configuration.toml, not in code."""
    from pathlib import Path

    toml = Path(psi.__file__).resolve().parents[1] / "settings" / "configuration.toml"
    content = toml.read_text(encoding="utf-8")

    assert 'qdrant_collection_suffix = "v2"' in content


def test_docs_carry_the_upgrade_note():
    """The issue requires the upgrade note to ship with the similar_issue docs."""
    from pathlib import Path

    doc = Path(psi.__file__).resolve().parents[2] / "docs" / "docs" / "tools" / "similar_issues.md"
    content = doc.read_text(encoding="utf-8")

    assert "qdrant_collection_suffix" in content
    assert "codium-ai-pr-agent-issues-v2" in content
