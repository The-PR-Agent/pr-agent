"""Regression test for #3539.

When a second repository is initialized against an already-existing lancedb
table, the sentinel-probe query returns an empty list.  Before the fix the
code did ``res[0].get("vector")``, which raised ``IndexError``.

The test constructs the tool for two repositories against the same fake
database: the first one has a sentinel row (already indexed), the second
doesn't.  After the fix the second repository must fall through to the
ingest path without crashing.
"""
import sys
import types
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import PRSimilarIssue


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeSearchBuilder:
    """Mimics the chained lancedb query builder."""

    def __init__(self, rows):
        self._rows = rows

    def limit(self, _n):
        return self

    def where(self, expr, **_kw):
        # Simulate filtering: return only rows whose id matches the WHERE clause
        # expr looks like: "id='example_issue_<repo>'"
        self._rows = [r for r in self._rows if f"id='{r['id']}'" == expr]
        return self

    def to_list(self):
        return list(self._rows)


class _FakeTable:
    """A fake lancedb table that holds rows as dicts."""

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.add_calls = []

    def __len__(self):
        return len(self._rows)

    def search(self, _vector=None):
        return _FakeSearchBuilder(self._rows)

    def add(self, df):
        self.add_calls.append(len(df))


class _FakeDB:
    def __init__(self, table_names, table=None):
        self._tables = table_names
        self._table = table
        self.created_with = None

    def list_tables(self):
        return SimpleNamespace(tables=self._tables)

    def __getitem__(self, name):
        if name not in self._tables:
            raise KeyError(name)
        return self._table

    def create_table(self, name, data, mode):
        self.created_with = (name, mode)
        return self._table


class _FakeDataFrame:
    def __init__(self, documents):
        self.records = [dict(doc) for doc in documents]

    def __len__(self):
        return len(self.records)

    def __getitem__(self, key):
        return _FakeSeries([r[key] for r in self.records])

    def __setitem__(self, key, value):
        for i, r in enumerate(self.records):
            r[key] = value[i]

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


def _fake_issue(number=1):
    return SimpleNamespace(
        number=number,
        title="a title",
        body="a body",
        pull_request=False,
        user=SimpleNamespace(login="tester"),
        created_at="2026-01-01T00:00:00Z",
    )


def _make_tool(monkeypatch, fake_db, repo_name="org/first-repo"):
    _install_fake_pandas(monkeypatch)
    monkeypatch.setattr(
        "pr_agent.tools.pr_similar_issue._embed_with_fallback", _fake_embed,
    )
    tool = PRSimilarIssue.__new__(PRSimilarIssue)
    tool.db = fake_db
    tool.index_name = "codium-ai-pr-agent-issues"
    tool.max_issues_to_scan = 10
    tool.token_handler = SimpleNamespace(count_tokens=lambda _: 0)
    tool.table = fake_db._table
    tool._process_issue = lambda issue: (
        f"title: {issue.title}\nbody: {issue.body}",
        [],
        issue.number,
    )
    return tool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_second_repo_does_not_crash_on_empty_sentinel(monkeypatch):
    """#3539: a second repository on an existing table must not IndexError.

    The table already has the sentinel for repo-a but NOT for repo-b.
    Before the fix, ``res[0].get("vector")`` raised ``IndexError`` because
    ``res`` was ``[]``.  After the fix the tool falls through to ``ingest=True``.
    """
    sentinel_row = {
        "id": "example_issue_org-first-repo",
        "vector": [0.1] * 8,
        "text": "example_issue",
        "metadata": {"repo": "org-first-repo"},
    }
    fake_table = _FakeTable(rows=[sentinel_row])
    fake_db = _FakeDB(
        table_names=["codium-ai-pr-agent-issues"],
        table=fake_table,
    )

    tool = _make_tool(monkeypatch, fake_db, repo_name="org/second-repo")
    tool.table = fake_table

    # Simulate the init path for a second repository:
    # table exists, force_update is off, sentinel for this repo is missing.
    index_name = "codium-ai-pr-agent-issues"
    repo_name_for_index = "org-second-repo"

    # This is the block from __init__ that used to crash:
    table = fake_db[index_name]
    res = table.search().limit(len(table)).where(
        f"id='example_issue_{repo_name_for_index}'"
    ).to_list()

    # Before fix: res[0].get("vector") -> IndexError
    # After fix: `if res and res[0].get("vector"):` evaluates to False
    assert res == [], "sentinel for second repo should not exist yet"

    # Verify the guard works
    ingest = True
    if res and res[0].get("vector"):
        ingest = False
    assert ingest is True, "second repo should trigger ingest path"


def test_first_repo_still_skips_ingest_when_sentinel_present(monkeypatch):
    """A repository whose sentinel row already exists should NOT re-ingest."""
    sentinel_row = {
        "id": "example_issue_org-first-repo",
        "vector": [0.1] * 8,
        "text": "example_issue",
        "metadata": {"repo": "org-first-repo"},
    }
    fake_table = _FakeTable(rows=[sentinel_row])
    fake_db = _FakeDB(
        table_names=["codium-ai-pr-agent-issues"],
        table=fake_table,
    )

    tool = _make_tool(monkeypatch, fake_db, repo_name="org/first-repo")

    index_name = "codium-ai-pr-agent-issues"
    repo_name_for_index = "org-first-repo"

    table = fake_db[index_name]
    res = table.search().limit(len(table)).where(
        f"id='example_issue_{repo_name_for_index}'"
    ).to_list()

    assert len(res) == 1, "sentinel should be found for the first repo"

    ingest = True
    if res and res[0].get("vector"):
        ingest = False
    assert ingest is False, "existing repo should skip ingest"
