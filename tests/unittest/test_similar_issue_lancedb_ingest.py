"""The LanceDB ingest path adds new rows to an existing index instead of skipping them."""
import sys
import types
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import PRSimilarIssue


class FakeDB:
    def __init__(self, table_names):
        self._table_names = table_names
        self.table = None

    def table_names(self):
        return self._table_names


class _FakeDataFrame:
    def __init__(self, documents):
        self.records = [dict(doc) for doc in documents]

    def __len__(self):
        return len(self.records)

    def __getitem__(self, key):
        return _FakeSeries([record[key] for record in self.records])

    def __setitem__(self, key, value):
        for i, record in enumerate(self.records):
            record[key] = value[i]

    def to_dict(self, orient="records"):
        return self.records


class _FakeSeries:
    def __init__(self, values):
        self._values = values

    def to_list(self):
        return self._values


def _install_fake_pandas(monkeypatch):
    fake_pandas = types.ModuleType("pandas")
    fake_pandas.DataFrame = _FakeDataFrame
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas)


def _fake_embed(texts):
    return [[0.0] * 8] * len(texts)


def _fake_issue(number=5):
    return SimpleNamespace(
        number=number,
        title="a title",
        body="a body",
        pull_request=False,
        user=SimpleNamespace(login="tester"),
        created_at="2026-01-01T00:00:00Z",
    )


def _make_tool(monkeypatch, fake_db):
    _install_fake_pandas(monkeypatch)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue._embed_with_fallback", _fake_embed)
    tool = PRSimilarIssue.__new__(PRSimilarIssue)
    tool.db = fake_db
    tool.index_name = "codium-ai-pr-agent-issues"
    tool.max_issues_to_scan = 10
    tool.token_handler = SimpleNamespace(count_tokens=lambda _: 0)
    tool.table = fake_db.table
    tool._process_issue = lambda issue: (
        f"title: {issue.title}\nbody: {issue.body}",
        [],
        issue.number,
    )
    return tool


def test_ingest_appends_rows_when_table_exists(monkeypatch):
    """With an existing LanceDB table, new rows are added via table.add, not dropped."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = SimpleNamespace()
    fake_table.add_calls = []
    fake_table.add = lambda df: fake_table.add_calls.append(len(df))
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == [2]


def test_ingest_warns_when_table_missing(monkeypatch):
    """Adding into a missing table is not attempted."""
    fake_db = FakeDB([])
    fake_table = SimpleNamespace()
    fake_table.add_calls = []
    fake_table.add = lambda df: fake_table.add_calls.append(len(df))
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == []