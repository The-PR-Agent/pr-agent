"""The LanceDB ingest path adds new rows to an existing index instead of skipping them."""
import sys
import time
import types
from types import SimpleNamespace

import pytest

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


class _FakeSearch:
    def __init__(self, table):
        self._table = table
        self._id = None

    def limit(self, _n):
        return self

    def where(self, sql):
        self._id = sql.split("'")[1]
        return self

    def to_list(self):
        return [row for row in self._table.rows if row["id"] == self._id]


def _fake_add(table, df):
    table.add_calls.append(len(df))
    table.rows.extend(df.to_dict("records"))


def _fake_delete(table, where):
    table.delete_calls.append(where)
    repo = where.split("'")[1]
    table.rows = [row for row in table.rows if row["metadata"].repo != repo]


def _fake_table(existing_rows=()):
    fake_table = SimpleNamespace()
    fake_table.rows = list(existing_rows)
    fake_table.add_calls = []
    fake_table.delete_calls = []
    fake_table.add = lambda df: _fake_add(fake_table, df)
    fake_table.delete = lambda where: _fake_delete(fake_table, where)
    fake_table.search = lambda: _FakeSearch(fake_table)
    return fake_table


class _FakeLanceQuery:
    def __init__(self, table):
        self._table = table
        self._id_filter = None

    def limit(self, limit):
        return self

    def where(self, expr, prefilter=False):
        if expr.startswith("id="):
            self._id_filter = expr[expr.index("=") + 1 :].strip("'\"")
        return self

    def to_list(self):
        if self._id_filter is None:
            return list(self._table.rows)
        return [row for row in self._table.rows if row["id"] == self._id_filter]


class _FakeSearchableTable:
    def __init__(self, rows):
        self.rows = rows
        self.add_calls = []
        self.delete_calls = []

    def __len__(self):
        return len(self.rows)

    def search(self, query=None):
        return _FakeLanceQuery(self)

    def add(self, df):
        self.add_calls.append(len(df))
        self.rows.extend(df.to_dict())

    def delete(self, where):
        self.delete_calls.append(where)
        if where.startswith("metadata.repo="):
            repo = where[where.index("=") + 1 :].strip("'\"")
            self.rows = [row for row in self.rows if row["metadata"]["repo"] != repo]


def _lancedb_row(record_id, repo):
    return {
        "id": record_id,
        "text": "a body",
        "metadata": {"repo": repo},
        "vector": [0.0] * 8,
    }


def test_initial_creation_overwrites_the_table(monkeypatch):
    """A from-scratch run (no existing table) creates the table with overwrite and does not sleep."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.created_with = None
    fake_db.create_table = lambda name, data, mode: fake_db.__setattr__(
        "created_with", (name, mode)
    )
    fake_db.table = None

    tool = _make_tool(monkeypatch, fake_db)
    tool.table = fake_db.table

    monkeypatch.setattr(
        time, "sleep", lambda seconds: pytest.fail(f"unexpected sleep({seconds}) after create")
    )

    tool._update_table_with_issues(
        [_fake_issue()],
        "utkarsh-demo",
        ingest=False,
    )

    assert fake_db.created_with == ("codium-ai-pr-agent-issues", "overwrite")


def test_ingest_appends_rows_when_table_exists(monkeypatch):
    """Add new rows to an existing table instead of dropping them, without sleeping."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    monkeypatch.setattr(
        time, "sleep", lambda seconds: pytest.fail(f"unexpected sleep({seconds}) after add")
    )

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
    """A table past the first ten names is still found."""
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


def test_init_does_not_rebuild_when_the_table_sorts_past_the_first_ten(monkeypatch):
    """A table beyond the deprecated pagination cap is recognized, so the index is not rebuilt."""
    names = [f"table-{i:02d}" for i in range(13)]
    names[10] = "codium-ai-pr-agent-issues"

    tool = _make_tool(monkeypatch, FakeDB(names))

    assert tool._table_exists_in_db("codium-ai-pr-agent-issues")


def test_init_rebuilds_when_the_table_is_absent(monkeypatch):
    """A genuinely missing table is still treated as needing a full rebuild."""
    tool = _make_tool(monkeypatch, FakeDB(["table-00"]))

    assert not tool._table_exists_in_db("codium-ai-pr-agent-issues")
def _capturing_table():
    fake_table = _fake_table()
    fake_table.added_rows = []
    fake_table.add = lambda df: fake_table.added_rows.extend(df.to_dict(orient="records"))
    return fake_table


_LONG_COMMENT = "this comment body has more than ten words and so it will be indexed in full"


def _fake_issue_with_comments(comments):
    return SimpleNamespace(
        number=5,
        title="a title",
        body="a body",
        pull_request=False,
        user=SimpleNamespace(login="tester"),
        created_at="2026-01-01T00:00:00Z",
        get_comments=lambda: comments,
    )


def _tool_with_comments(monkeypatch, fake_db):
    tool = _make_tool(monkeypatch, fake_db)
    tool._process_issue = lambda issue: (
        f"title: {issue.title}\nbody: {issue.body}",
        list(issue.get_comments()),
        issue.number,
    )
    return tool


def test_ingest_skips_null_comment_body_without_crashing(monkeypatch):
    """A comment with a null body is skipped instead of raising AttributeError."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _capturing_table()
    fake_db.table = fake_table

    tool = _tool_with_comments(monkeypatch, fake_db)
    issue = _fake_issue_with_comments([
        SimpleNamespace(body=_LONG_COMMENT),
        SimpleNamespace(body=None),
    ])

    tool._update_table_with_issues([issue], "org/repo", ingest=True)

    texts = [row["text"] for row in fake_table.added_rows]
    assert _LONG_COMMENT in texts
    assert len(fake_table.added_rows) == 3  # sentinel + issue + one comment


def test_ingest_skips_short_comments_and_indexes_long_ones(monkeypatch):
    """Word-count guard still skips short bodies while long ones are indexed."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _capturing_table()
    fake_db.table = fake_table

    tool = _tool_with_comments(monkeypatch, fake_db)
    issue = _fake_issue_with_comments([
        SimpleNamespace(body="short"),
        SimpleNamespace(body=_LONG_COMMENT),
    ])

    tool._update_table_with_issues([issue], "org/repo", ingest=True)

    texts = [row["text"] for row in fake_table.added_rows]
    assert "short" not in texts
    assert _LONG_COMMENT in texts
    assert len(fake_table.added_rows) == 3  # sentinel + issue + one comment


def test_second_repository_missing_sentinel_joins_instead_of_crashing(monkeypatch):
    """Detect a repo never indexed on an existing table and join it without an IndexError."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.table = _FakeSearchableTable([_lancedb_row("example_issue_org-repo-a", "org-repo-a")])
    tool = _make_tool(monkeypatch, fake_db)

    assert tool._lancedb_repo_already_indexed("org-repo-a") is True
    assert tool._lancedb_repo_already_indexed("org-repo-b") is False

    tool._update_table_with_issues([_fake_issue()], "org-repo-b", ingest=True)

    assert tool._lancedb_repo_already_indexed("org-repo-b") is True


def test_each_repository_gets_exactly_one_sentinel_row(monkeypatch):
    """Add the second repo's sentinel row without touching the first repo's rows."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.table = _FakeSearchableTable([_lancedb_row("example_issue_org-repo-a", "org-repo-a")])
    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues([_fake_issue()], "org-repo-b", ingest=True)

    sentinels = [
        row["id"] for row in fake_db.table.rows if row["id"].startswith("example_issue_")
    ]
    assert sentinels == ["example_issue_org-repo-a", "example_issue_org-repo-b"]
    assert fake_db.table.delete_calls == []


class _LanceSettings:
    class CONFIG:
        CLI_MODE = True

    class config:
        model = "gpt-4o-mini"

    class pr_similar_issue:
        vectordb = "lancedb"
        max_issues_to_scan = 10
        skip_comments = True
        force_update_dataset = False

    class lancedb:
        uri = "/tmp/test-lancedb"


class _FakeRepo:
    def __init__(self):
        self.full_name = "org/repo-b"

    def get_issues(self, state="all"):
        return [_fake_issue(5), _fake_issue(6)]


class _FakeGithubClient:
    def __init__(self):
        self._repo = _FakeRepo()

    def get_repo(self, name):
        return self._repo


class _FakeProvider:
    def __init__(self):
        self.repo_obj = _FakeRepo()
        self.github_client = _FakeGithubClient()

    @staticmethod
    def _parse_issue_url(url):
        return ("org/repo-b", 5)


def _install_fake_lancedb(monkeypatch, fake_db):
    fake_lancedb = types.ModuleType("lancedb")
    fake_lancedb.connect = lambda uri: fake_db
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)


def _stub_constructor_dependencies(monkeypatch, fake_db):
    _install_fake_lancedb(monkeypatch, fake_db)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_settings", lambda: _LanceSettings)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: _FakeProvider)
    monkeypatch.setattr(
        "pr_agent.tools.pr_similar_issue._provider_supports_issue_indexing",
        lambda: True,
    )
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue._embed_with_fallback", _fake_embed)
    monkeypatch.setattr(
        "pr_agent.tools.pr_similar_issue.TokenHandler",
        lambda *args, **kwargs: SimpleNamespace(count_tokens=lambda text: 0),
    )
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.time.sleep", lambda seconds: None)


def test_constructor_second_repo_joins_existing_table(monkeypatch):
    """Construct the tool for a second repo on an existing table and construct it again."""
    _install_fake_pandas(monkeypatch)
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.table = _FakeSearchableTable([_lancedb_row("example_issue_org-repo-a", "org-repo-a")])
    _stub_constructor_dependencies(monkeypatch, fake_db)

    tool = PRSimilarIssue("https://github.com/org/repo-b/pull/5", None)

    assert tool.repo_name_for_index == "org-repo-b"
    assert fake_db.table.delete_calls == ["metadata.repo='org-repo-b'"]  # no-op first join
    assert fake_db.table.add_calls == [3]  # sentinel plus two issues

    PRSimilarIssue("https://github.com/org/repo-b/pull/5", None)

    assert fake_db.table.add_calls == [3]  # sentinel is present now, incremental path only
    sentinels = [
        row["id"] for row in fake_db.table.rows if row["id"].startswith("example_issue_")
    ]
    assert sentinels == ["example_issue_org-repo-a", "example_issue_org-repo-b"]


def test_concurrent_first_runs_do_not_duplicate_rows(monkeypatch):
    """Overlapping first runs for the same repo leave a single set of rows."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.table = _FakeSearchableTable([_lancedb_row("example_issue_org-repo-a", "org-repo-a")])
    tool = _make_tool(monkeypatch, fake_db)

    for _ in range(2):
        tool._update_table_with_issues([_fake_issue()], "org-repo-b", ingest=True, force_refresh=True)

    ids = [row["id"] for row in fake_db.table.rows]
    assert ids.count("example_issue_org-repo-b") == 1
    assert ids.count("issue_5.issue") == 1


def test_sentinel_added_once_across_incremental_runs(monkeypatch):
    """Later incremental runs add only the new issue row, not another sentinel.

    The incremental caller in run() already filters out indexed issues by id, so the method
    receives only new issues; its own contract is limited to the de-duplication LanceDB cannot
    do for the sentinel marker.
    """
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table()
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues([_fake_issue()], "utkarsh-demo", ingest=True)
    tool._update_table_with_issues([_fake_issue(6)], "utkarsh-demo", ingest=True)

    assert fake_table.add_calls == [2, 1]
    sentinels = [row for row in fake_table.rows if row["id"] == "example_issue_utkarsh-demo"]
    assert len(sentinels) == 1
    assert len([row for row in fake_table.rows if row["id"] == "issue_5.issue"]) == 1


def test_initial_creation_with_empty_corpus_still_builds_table(monkeypatch):
    """A from-scratch run on an empty corpus still creates the table (sentinel row only)."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_db.created_with = None
    fake_db.create_table = lambda name, data, mode: fake_db.__setattr__(
        "created_with", (name, mode, data)
    )
    fake_db.table = None

    tool = _make_tool(monkeypatch, fake_db)
    tool.table = fake_db.table

    tool._update_table_with_issues([], "utkarsh-demo", ingest=False)

    name, mode, df = fake_db.created_with
    assert name == "codium-ai-pr-agent-issues"
    assert mode == "overwrite"
    assert [row["id"] for row in df.to_dict("records")] == ["example_issue_utkarsh-demo"]


def test_ingest_empty_corpus_skips_update(monkeypatch):
    """An empty corpus on the ingest path is skipped without touching the table."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table(existing_rows=[
        {"id": "issue_1.issue", "text": "an old issue",
         "metadata": SimpleNamespace(repo="org/repo-a")},
    ])
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues([], "org/repo-a", ingest=True)

    assert fake_table.add_calls == []
    assert fake_table.delete_calls == []
    assert len(fake_table.rows) == 1


def test_force_refresh_keeps_a_single_sentinel(monkeypatch):
    """A forced refresh deletes the repo rows, then re-adds a single sentinel."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table(existing_rows=[
        {"id": "example_issue_org/repo-a", "text": "example_issue",
         "metadata": SimpleNamespace(repo="org/repo-a")},
    ])
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
    sentinels = [row for row in fake_table.rows if row["id"] == "example_issue_org/repo-a"]
    assert len(sentinels) == 1


def test_force_refresh_empty_corpus_restores_the_sentinel(monkeypatch):
    """A forced refresh with no embeddable issues clears stale rows and keeps the sentinel."""
    fake_db = FakeDB(["codium-ai-pr-agent-issues"])
    fake_table = _fake_table(existing_rows=[
        {"id": "issue_1.issue", "text": "an old issue",
         "metadata": SimpleNamespace(repo="org/repo-a")},
        {"id": "example_issue_org/repo-a", "text": "example_issue",
         "metadata": SimpleNamespace(repo="org/repo-a")},
    ])
    fake_db.table = fake_table

    tool = _make_tool(monkeypatch, fake_db)

    tool._update_table_with_issues([], "org/repo-a", ingest=True, force_refresh=True)

    assert fake_table.delete_calls == ["metadata.repo='org/repo-a'"]
    assert fake_table.add_calls == [1]
    sentinels = [row for row in fake_table.rows if row["id"] == "example_issue_org/repo-a"]
    assert len(sentinels) == 1
    assert len(fake_table.rows) == 1
