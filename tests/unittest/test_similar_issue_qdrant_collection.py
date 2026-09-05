"""The qdrant collection name is suffixed so pre-#2323 points cannot surface in results."""
import inspect

import pr_agent.tools.pr_similar_issue as psi

BASE_INDEX_NAME = "codium-ai-pr-agent-issues"


def test_collection_name_has_the_v2_suffix():
    """The v2 suffix keeps the new index separate from the pre-#2323 collection."""
    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == "codium-ai-pr-agent-issues-v2"


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


def test_docs_carry_the_upgrade_note():
    """The issue requires the upgrade note to ship with the similar_issue docs."""
    from pathlib import Path

    doc = Path(psi.__file__).resolve().parents[2] / "docs" / "docs" / "tools" / "similar_issues.md"
    content = doc.read_text(encoding="utf-8")

    assert "codium-ai-pr-agent-issues-v2" in content
