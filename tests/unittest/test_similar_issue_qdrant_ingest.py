"""The qdrant ingest path keeps the repo sentinel consistent with every completed write."""
import sys
import types
from types import SimpleNamespace

import pytest

import pr_agent.tools.pr_similar_issue as psi


class _PandasSeries(list):
    @property
    def values(self):
        return list(self)

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


def _make_issue(number):
    return SimpleNamespace(
        pull_request=False,
        title=f"Issue {number}",
        body="Body",
        number=number,
        user=SimpleNamespace(login="user"),
        created_at="2020-01-01",
        get_comments=lambda: [],
    )


def _fake_embed(texts):
    return [[0.1, 0.2] for _ in texts]


class _PointStruct:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class FakeQdrantClient:
    def __init__(self, url=None, api_key=None, **kwargs):
        self.upserts = []
        self.deletes = []

    def collection_exists(self, collection_name=None):
        return True

    def count(self, collection_name=None, count_filter=None):
        return SimpleNamespace(count=1)

    def create_collection(self, **kwargs):
        pass

    def upsert(self, collection_name=None, points=None, **kwargs):
        self.upserts.append((collection_name, points))

    def upload_points(self, collection_name=None, points=None, **kwargs):
        self.upserts.append((collection_name, points))

    def delete(self, collection_name=None, points_selector=None, **kwargs):
        sentinel_id = None
        for condition in getattr(points_selector, "must", ()):
            if condition["key"] == "id":
                sentinel_id = condition["match"].value
        self.deletes.append(sentinel_id)


class _StatefulQdrantClient:
    """Mirrors qdrant behaviour: store indexed ids and honor sentinel filters."""

    def __init__(self, ids=()):
        self.ids = set(ids)
        self.upserts = []
        self.deletes = []

    def collection_exists(self, collection_name=None):
        return True

    def count(self, collection_name=None, count_filter=None):
        sentinel_id = None
        for condition in getattr(count_filter, "must", ()):
            if condition["key"] == "id":
                sentinel_id = condition["match"].value
        return SimpleNamespace(count=1 if sentinel_id in self.ids else 0)

    def upsert(self, collection_name=None, points=None, **kwargs):
        self.upserts.append([point.payload["id"] for point in points])
        for point in points:
            self.ids.add(point.payload["id"])

    def upload_points(self, collection_name=None, points=None, **kwargs):
        self.upserts.append([point.payload["id"] for point in points])
        for point in points:
            self.ids.add(point.payload["id"])

    def delete(self, collection_name=None, points_selector=None, **kwargs):
        sentinel_id = None
        for condition in getattr(points_selector, "must", ()):
            if condition["key"] == "id":
                sentinel_id = condition["match"].value
        if sentinel_id is not None:
            self.ids.discard(sentinel_id)
        self.deletes.append(sentinel_id)


def _install_fakes(monkeypatch, client):
    monkeypatch.setitem(
        sys.modules,
        "pandas",
        SimpleNamespace(DataFrame=_PandasDataFrame),
    )
    fake_qdrant_client = types.ModuleType("qdrant_client")
    fake_qdrant_client.QdrantClient = lambda *args, **kwargs: client
    monkeypatch.setitem(sys.modules, "qdrant_client", fake_qdrant_client)
    fake_models = SimpleNamespace(
        Distance=None,
        FieldCondition=lambda **kwargs: kwargs,
        Filter=lambda must=None: SimpleNamespace(must=must),
        MatchValue=lambda value=None: SimpleNamespace(value=value),
        VectorParams=lambda **kwargs: kwargs,
        PointStruct=_PointStruct,
    )
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_models)
    monkeypatch.setattr(psi, "_embed_with_fallback", _fake_embed)


def _make_tool(monkeypatch, client):
    _install_fakes(monkeypatch, client)
    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)
    tool.qdrant = client
    tool.qdrant_collection_name = "codium-ai-pr-agent-issues-v2"
    tool.max_issues_to_scan = 100
    tool.token_handler = SimpleNamespace(count_tokens=lambda _: 0)
    tool._process_issue = lambda issue: (
        f"title: {issue.title}\nbody: {issue.body}",
        [],
        issue.number,
    )
    return tool


class SettingsStub:
    class CONFIG:
        CLI_MODE = True

    class pr_similar_issue:
        skip_comments = True
        max_issues_to_scan = 100
        vectordb = "qdrant"
        force_update_dataset = False

    class qdrant:
        url = "http://localhost:6333"
        api_key = "qdrant-key"


class FakeProvider:
    @staticmethod
    def supports_issue_indexing():
        return True

    def __init__(self, issues=None):
        self._issues = list(issues) if issues is not None else [_make_issue(1)]
        self.github_client = SimpleNamespace(
            get_repo=lambda repo_name: SimpleNamespace(
                full_name="Example/Repo",
                get_issues=lambda state: self._issues,
            )
        )

    def _parse_issue_url(self, issue_url):
        return "Example/Repo", 1


def _stub_constructor_dependencies(monkeypatch, client, issues):
    _install_fakes(monkeypatch, client)
    monkeypatch.setattr(psi, "get_settings", lambda: SettingsStub)

    def provider_factory():
        return FakeProvider(issues)

    monkeypatch.setattr(psi, "get_git_provider", lambda: provider_factory)
    monkeypatch.setattr(psi, "_provider_supports_issue_indexing", lambda: True)
    monkeypatch.setattr(
        psi,
        "TokenHandler",
        lambda *args, **kwargs: SimpleNamespace(count_tokens=lambda text: 0),
    )


def test_qdrant_sentinel_is_the_final_point_of_a_full_ingest(monkeypatch):
    """Submit the completion sentinel in a separate call after every issue point."""
    client = FakeQdrantClient()
    tool = _make_tool(monkeypatch, client)

    tool._update_qdrant_with_issues([_make_issue(2), _make_issue(1)], "example-repo", ingest=True)

    assert client.deletes == ["example_issue_example-repo"]
    assert len(client.upserts) == 2
    _, issue_points = client.upserts[0]
    assert [point.payload["id"] for point in issue_points] == ["issue_2.issue", "issue_1.issue"]
    _, sentinel_points = client.upserts[1]
    assert [point.payload["id"] for point in sentinel_points] == ["example_issue_example-repo"]


def test_qdrant_oversized_issues_do_not_burn_the_scan_budget(monkeypatch):
    """Oversized rejects are not counted so the scan budget reaches older index gaps."""
    client = FakeQdrantClient()
    tool = _make_tool(monkeypatch, client)
    tool.max_issues_to_scan = 2
    tool.token_handler = SimpleNamespace(count_tokens=lambda _: 10 ** 6)
    monkeypatch.setattr(psi, "get_max_tokens", lambda model: 8192)

    oversized = SimpleNamespace(
        pull_request=False,
        title="Oversized",
        body="x" * 9000,
        number=1,
        user=SimpleNamespace(login="user"),
        created_at="2020-01-01",
        get_comments=lambda: [],
    )
    tool._update_qdrant_with_issues(
        [oversized, _make_issue(2), _make_issue(3), _make_issue(4)],
        "example-repo",
        ingest=True,
    )

    _, issue_points = client.upserts[0]
    assert [point.payload["id"] for point in issue_points] == ["issue_2.issue", "issue_3.issue"]
    _, sentinel_points = client.upserts[1]
    assert [point.payload["id"] for point in sentinel_points] == ["example_issue_example-repo"]


def test_qdrant_collection_without_sentinel_reingests_full(monkeypatch):
    """Re-ingest the whole repo when an existing collection holds no sentinel.

    An interrupted full ingest leaves the collection populated but sentinel-free, so the
    constructor has to take the full-ingest path and finish with the sentinel point last.
    """
    client = _StatefulQdrantClient(ids={"issue_5.issue"})
    _stub_constructor_dependencies(
        monkeypatch, client, issues=[_make_issue(5), _make_issue(4)]
    )

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert len(client.upserts) == 2
    assert client.upserts[0] == ["issue_5.issue", "issue_4.issue"]
    assert client.upserts[1] == ["example_issue_example-repo"]
    assert "example_issue_example-repo" in client.ids


def test_qdrant_incremental_backfills_an_older_index_gap(monkeypatch):
    """Keep scanning past indexed issues and backfill an older missing issue.

    With the sentinel and the newest issue already indexed, a deleted older issue must be
    re-added by a normal incremental run, without a forced refresh.
    """
    client = _StatefulQdrantClient(ids={"example_issue_example-repo", "issue_5.issue"})
    _stub_constructor_dependencies(
        monkeypatch, client, issues=[_make_issue(5), _make_issue(4)]
    )

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert len(client.upserts) == 2
    assert client.upserts[0] == ["issue_4.issue"]
    assert client.upserts[1] == ["example_issue_example-repo"]
    assert "issue_4.issue" in client.ids
    assert "example_issue_example-repo" in client.ids


class _CommentsEnabledSettings(SettingsStub):
    class pr_similar_issue:
        skip_comments = False
        max_issues_to_scan = 100
        vectordb = "qdrant"
        force_update_dataset = False


def test_qdrant_incremental_scan_skips_comment_fetching(monkeypatch):
    """Incremental scans only need the issue number, so no comment fetch happens.

    Only the issue number is used to build the lookup id; fetching body and comments
    per issue in the scan window wastes an API call per issue on every run.
    """
    client = _StatefulQdrantClient(ids={"example_issue_example-repo", "issue_5.issue"})
    comment_fetches = []

    def fetch_comments():
        comment_fetches.append(1)
        return []

    issue_5 = _make_issue(5)
    issue_5.get_comments = fetch_comments
    _stub_constructor_dependencies(
        monkeypatch, client, issues=[issue_5, _make_issue(4)]
    )
    monkeypatch.setattr(psi, "get_settings", lambda: _CommentsEnabledSettings)

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert comment_fetches == []
    assert len(client.upserts) == 2
    assert client.upserts[0] == ["issue_4.issue"]
    assert client.upserts[1] == ["example_issue_example-repo"]


def test_qdrant_failed_write_then_full_reingest(monkeypatch):
    """Re-ingest in full when a failed write revoked the sentinel.

    The sentinel is deleted before writing, so a failed incremental write leaves the
    collection sentinel-free and the next constructor takes the full-ingest path.
    """
    client = _StatefulQdrantClient(ids={"example_issue_example-repo", "issue_5.issue"})
    _stub_constructor_dependencies(
        monkeypatch, client, issues=[_make_issue(6), _make_issue(5)]
    )
    original_upload_points = client.upload_points

    def failing_upload_points(collection_name=None, points=None, **kwargs):
        ids = [point.payload["id"] for point in points]
        if "example_issue_" not in ids[0]:
            raise RuntimeError("point write failed")
        original_upload_points(collection_name=collection_name, points=points, **kwargs)

    client.upload_points = failing_upload_points

    with pytest.raises(RuntimeError):
        psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert "example_issue_example-repo" not in client.ids

    client.upload_points = original_upload_points
    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert client.upserts[-1][-1] == "example_issue_example-repo"
    assert "example_issue_example-repo" in client.ids
    assert {"issue_6.issue", "issue_5.issue"} <= client.ids
