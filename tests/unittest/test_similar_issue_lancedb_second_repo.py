"""A second repository indexed into an existing LanceDB table is indexed, not crashed on."""
import sys
import types
from types import SimpleNamespace

from pr_agent.tools.pr_similar_issue import PRSimilarIssue

INDEX_NAME = "codium-ai-pr-agent-issues"


class FakeSearch:
    """The subset of LanceDB's query builder the sentinel probe uses."""

    def __init__(self, rows):
        self._rows = rows

    def limit(self, _n):
        return self

    def where(self, clause):
        wanted = clause.split("=", 1)[1].strip("'")
        return FakeSearch([row for row in self._rows if row["id"] == wanted])

    def to_list(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def search(self):
        return FakeSearch(self.rows)


class FakeDB:
    def __init__(self, tables):
        self._tables = tables

    def table_names(self):
        return list(self._tables)

    def __getitem__(self, name):
        return self._tables[name]


def _fake_repo(full_name):
    return SimpleNamespace(full_name=full_name, get_issues=lambda state: [])


def _install_fakes(monkeypatch, db, full_name):
    class FakeProvider:
        def __init__(self):
            self.repo = None
            self.repo_obj = None
            self.github_client = SimpleNamespace(get_repo=lambda name: _fake_repo(full_name))

        @classmethod
        def supports_issue_indexing(cls):
            return True

        def _parse_issue_url(self, _url):
            return full_name, 1

    settings = SimpleNamespace(
        CONFIG=SimpleNamespace(CLI_MODE=True),
        pr_similar_issue=SimpleNamespace(
            max_issues_to_scan=10,
            vectordb="lancedb",
            force_update_dataset=False,
        ),
        lancedb=SimpleNamespace(uri="/tmp/lancedb"),
    )
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_settings", lambda: settings)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.get_git_provider", lambda: FakeProvider)
    monkeypatch.setattr("pr_agent.tools.pr_similar_issue.TokenHandler", lambda: SimpleNamespace())

    fake_lancedb = types.ModuleType("lancedb")
    fake_lancedb.connect = lambda uri: db
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)


def _build(monkeypatch, db, full_name):
    """Construct the tool for `full_name` and report how it decided to index."""
    calls = []
    monkeypatch.setattr(
        PRSimilarIssue,
        "_update_table_with_issues",
        lambda self, issues, repo, ingest=False, force_refresh=False: calls.append(
            (repo, ingest, force_refresh)
        ),
    )
    tool = PRSimilarIssue(f"https://github.com/{full_name}/issues/1", None)
    return tool, calls


def test_second_repository_on_an_existing_table_is_indexed(monkeypatch):
    """A repo with no sentinel row indexes from scratch instead of raising IndexError."""
    table = FakeTable([{"id": "example_issue_org-repo-a", "vector": [0.0]}])
    db = FakeDB({INDEX_NAME: table})
    _install_fakes(monkeypatch, db, "org/repo-b")

    _tool, calls = _build(monkeypatch, db, "org/repo-b")

    assert calls == [("org-repo-b", True, False)]


def test_indexed_repository_still_takes_the_incremental_path(monkeypatch):
    """A repo whose sentinel row is already present is not re-indexed from scratch."""
    table = FakeTable([{"id": "example_issue_org-repo-a", "vector": [0.0]}])
    db = FakeDB({INDEX_NAME: table})
    _install_fakes(monkeypatch, db, "org/repo-a")

    _tool, calls = _build(monkeypatch, db, "org/repo-a")

    assert calls == []


def test_first_repository_creates_the_table(monkeypatch):
    """With no table at all the repo is indexed from scratch, creating the table."""
    db = FakeDB({})
    _install_fakes(monkeypatch, db, "org/repo-a")

    _tool, calls = _build(monkeypatch, db, "org/repo-a")

    assert calls == [("org-repo-a", False, False)]

