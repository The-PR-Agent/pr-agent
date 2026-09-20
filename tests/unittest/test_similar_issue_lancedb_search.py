"""Regression tests for #3541.

The lancedb search path was copied from the pinecone path and carried three
bugs:

1. No ``.distance_type("cosine")`` — lancedb defaults to squared-L2, so
   ``1 - _distance`` is ``2cos(θ) − 1`` instead of ``cos(θ)``.
2. No ``.limit(5)`` — lancedb defaults to 10 results instead of 5 like
   pinecone and qdrant.
3. ``time.sleep(15)`` after ``create_table`` and ``time.sleep(5)`` after
   every ``add`` — copied from pinecone's eventually-consistent upsert;
   lancedb writes are synchronous and queryable immediately.

The tests verify that ``_update_table_with_issues`` does not call
``time.sleep``.
"""
import sys
import time
import types
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import PRSimilarIssue


# ---------------------------------------------------------------------------
# Fakes — modeled after the existing test_similar_issue_lancedb_ingest.py
# ---------------------------------------------------------------------------
class _FakeTable:
    def __init__(self):
        self.add_calls = []
        self.delete_calls = []

    def __len__(self):
        return 0

    def add(self, df):
        self.add_calls.append(len(df))

    def delete(self, where):
        self.delete_calls.append(where)


class _FakeDB:
    def __init__(self, table_names, table=None):
        self._tables = table_names
        self._table = table
        self.created_with = None

    def list_tables(self):
        return SimpleNamespace(tables=self._tables)

    def __getitem__(self, name):
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
# Tests for unnecessary sleeps (#3541)
# ---------------------------------------------------------------------------
def test_create_table_does_not_sleep(monkeypatch):
    """_update_table_with_issues(ingest=False) must not call time.sleep.

    lancedb writes are synchronous and queryable immediately; the
    time.sleep(15) was copied from the pinecone path and serves no purpose.
    """
    fake_table = _FakeTable()
    fake_db = _FakeDB(["codium-ai-pr-agent-issues"], table=fake_table)
    fake_db.create_table = lambda name, data, mode: fake_table

    tool = _make_tool(monkeypatch, fake_db)
    tool.table = None

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleep_calls.append(secs))

    tool._update_table_with_issues(
        [_fake_issue()], "test-repo", ingest=False,
    )

    assert sleep_calls == [], \
        f"lancedb is synchronous; create_table must not sleep, but slept {sleep_calls}"


def test_add_rows_does_not_sleep(monkeypatch):
    """_update_table_with_issues(ingest=True) must not call time.sleep.

    lancedb writes are synchronous and queryable immediately; the
    time.sleep(5) was copied from the pinecone path and serves no purpose.
    """
    fake_table = _FakeTable()
    fake_db = _FakeDB(["codium-ai-pr-agent-issues"], table=fake_table)

    tool = _make_tool(monkeypatch, fake_db)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleep_calls.append(secs))

    tool._update_table_with_issues(
        [_fake_issue()], "test-repo", ingest=True,
    )

    assert sleep_calls == [], \
        f"lancedb is synchronous; add must not sleep, but slept {sleep_calls}"
