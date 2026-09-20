"""The qdrant collection name is suffixed so pre-#2323 points cannot surface in results."""

import inspect
import sys
from types import SimpleNamespace

import pr_agent.tools.pr_similar_issue as psi

BASE_INDEX_NAME = "codium-ai-pr-agent-issues"


class _PandasSeries(list):
    def to_list(self):
        return list(self)


class _PandasDataFrame:
    def __init__(self, records):
        self._records = list(records)
        self._overrides = {}

    def __getitem__(self, column):
        if column in self._overrides:
            return _PandasSeries(self._overrides[column])
        return _PandasSeries([row[column] for row in self._records])

    def __setitem__(self, column, values):
        self._overrides[column] = list(values)

    def to_dict(self, orient="records"):
        rows = [dict(record) for record in self._records]
        for column, values in self._overrides.items():
            for row, value in zip(rows, values, strict=True):
                row[column] = value
        return rows


class _PointStruct:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_qdrant_upload_points_uses_batching(monkeypatch):
    calls = {}

    class FakeQdrant:
        def upload_points(self, **kwargs):
            calls.update(kwargs)

    fake_qdrant_models = SimpleNamespace(PointStruct=_PointStruct)
    fake_qdrant_client = SimpleNamespace(models=fake_qdrant_models)

    monkeypatch.setitem(
        sys.modules,
        "pandas",
        SimpleNamespace(DataFrame=_PandasDataFrame),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        fake_qdrant_client,
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client.models",
        fake_qdrant_models,
    )

    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)
    tool.qdrant = FakeQdrant()
    tool.qdrant_collection_name = "test-collection"
    tool.max_issues_to_scan = 100
    tool.token_handler = SimpleNamespace(count_tokens=lambda text: 1)

    issue = SimpleNamespace(
        pull_request=False,
        user=SimpleNamespace(login="user"),
        created_at="2020-01-01",
        number=7,
        get_comments=lambda: [],
    )

    monkeypatch.setattr(
        psi.PRSimilarIssue,
        "_process_issue",
        lambda self, issue: ("Issue body", [], issue.number),
    )
    monkeypatch.setattr(
        psi,
        "_embed_with_fallback",
        lambda texts: [[0.5, 0.5] for _ in texts],
    )

    tool._update_qdrant_with_issues([issue], "example-repo")

    assert calls["collection_name"] == "test-collection"
    assert calls["batch_size"] == 100
    assert calls["wait"] is True
    assert len(calls["points"]) == 2


def test_collection_name_has_the_v2_suffix():
    """The v2 suffix keeps the new index separate from the pre-#2323 collection."""
    assert psi._qdrant_collection_name(BASE_INDEX_NAME) == "codium-ai-pr-agent-issues-v2"


def test_suffix_is_scoped_to_qdrant_only():
    """index_name is shared with pinecone and lancedb, so only qdrant call sites may be renamed."""
    source = inspect.getsource(psi)
    qdrant_only_call_sites = [
        "if not self.qdrant.collection_exists(collection_name=self.qdrant_collection_name):",
        "self.qdrant.upload_points(",
        "batch_size=100",
        "wait=True",
    ]

    for call_site in qdrant_only_call_sites:
        assert call_site in source

    assert 'index_name = self.index_name = "codium-ai-pr-agent-issues"' in source
    assert "self.pc.Index(name=self.index_name)" in source
    assert 'self.db.create_table(self.index_name, data=df, mode="overwrite")' in source
    assert "self.qdrant_collection_name" not in source.split(
        'elif get_settings().pr_similar_issue.vectordb == "qdrant":'
    )[0]


def test_docs_carry_the_upgrade_note():
    """The issue requires the upgrade note to ship with the similar_issue docs."""
    from pathlib import Path

    doc = (
        Path(psi.__file__).resolve().parents[2]
        / "docs"
        / "docs"
        / "tools"
        / "similar_issues.md"
    )
    content = doc.read_text(encoding="utf-8")
    assert "codium-ai-pr-agent-issues-v2" in content
