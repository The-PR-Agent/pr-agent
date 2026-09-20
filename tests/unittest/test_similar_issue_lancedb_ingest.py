"""The LanceDB ingest path adds new rows to an existing index instead of skipping them."""
import sys
import types
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import PRSimilarIssue


class FakeDB:
    def __init__(self, tables):
        self._tables = tables
        self.table = None

    def list_tables(self):
        return SimpleNamespace(tables=self._tables)

    def __getitem__(self, name):
        if name not in self._tables:
            raise KeyError(name)
        return self.table


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


def _fake_table():
    fake_table = SimpleNamespace()
    fake_table.add_calls = []
    fake_table.delete_calls = []
    fake_table.add = lambda df: fake_table.add_calls.append(len(df))
    fake_table.delete = lambda where: fake_table.delete_calls.append(where)
    return fake_table


def test_initial_creation_overwrites_the_table(monkeypatch):
    """A from-scratch run (no existing table) creates the table with overwrite."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.created_with = None
    fake_db.create_table = lambda name, data, mode: fake_db.__setattr__(
        "created_with", (name, mode)
    )
    fake_db.table = None

    tool = _make_tool(monkeypatch, fake_db)
    tool.table = fake_db.table

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=False,
    )

    assert fake_db.created_with == ("codium-ai-pr-agent-issues", "overwrite")


def test_ingest_appends_rows_when_table_exists(monkeypatch):
    """Add new rows to an existing table instead of dropping them."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == [2]
    assert fake_table.delete_calls == []


def test_ingest_fetches_table_when_table_handle_unset(monkeypatch):
    """A missing table handle is fetched from the db before appending rows."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)
    tool.table = None

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == [2]
    assert tool.table is fake_table


def test_force_refresh_replaces_only_the_current_repo_rows(monkeypatch):
    """A forced refresh deletes only the current repo rows, then appends refreshed ones."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "org/repo-a",
        ingest=True,
        force_refresh=True,
    )

    assert fake_table.delete_calls == ["metadata.repo='org/repo-a'"]
    assert fake_table.add_calls == [2]


def test_ingest_warns_when_table_missing(monkeypatch):
    """Avoid adding rows when the table does not exist."""
    fake_db = FakeDB([])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == []


def test_ingest_finds_a_table_beyond_the_first_pagination_page(monkeypatch):
    """list_tables() lists every table, not just the first ten the deprecated call capped at."""
    names = [f"table-{i:02d}" for i in range(13)]
    names[10] = "codium-ai-pr-agent-issues"
    fake_db = FakeDB(names)
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=True,
    )

    assert fake_table.add_calls == [2]
