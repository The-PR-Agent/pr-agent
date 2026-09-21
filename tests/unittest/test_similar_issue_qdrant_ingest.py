"""The qdrant ingest path submits the repo sentinel only after all issue points."""
import sys
import types
from types import SimpleNamespace

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

    def collection_exists(self, collection_name=None):
        return True

    def count(self, collection_name=None, count_filter=None):
        return SimpleNamespace(count=0)

    def create_collection(self, **kwargs):
        pass

    def upsert(self, collection_name=None, points=None, **kwargs):
        self.upserts.append((collection_name, points))


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

    def __init__(self):
        self.github_client = SimpleNamespace(
            get_repo=lambda repo_name: SimpleNamespace(
                full_name="Example/Repo",
                get_issues=lambda state: [_make_issue(2), _make_issue(1)],
            )
        )

    def _parse_issue_url(self, issue_url):
        return "Example/Repo", 1


def _stub_constructor_dependencies(monkeypatch, client):
    _install_fakes(monkeypatch, client)
    monkeypatch.setattr(psi, "get_settings", lambda: SettingsStub)
    monkeypatch.setattr(psi, "get_git_provider", lambda: FakeProvider)
    monkeypatch.setattr(psi, "_provider_supports_issue_indexing", lambda: True)
    monkeypatch.setattr(
        psi,
        "TokenHandler",
        lambda *args, **kwargs: SimpleNamespace(count_tokens=lambda text: 0),
    )


def test_qdrant_sentinel_is_the_final_point_of_a_full_ingest(monkeypatch):
    """Submit the completion sentinel after every issue point of a full ingest."""
    client = FakeQdrantClient()
    tool = _make_tool(monkeypatch, client)

    tool._update_qdrant_with_issues([_make_issue(2), _make_issue(1)], "example-repo", ingest=True)

    assert len(client.upserts) == 1
    _, points = client.upserts[0]
    ids = [point.payload["id"] for point in points]
    assert ids[:-1] == ["issue_2.issue", "issue_1.issue"]
    assert ids[-1] == "example_issue_example-repo"
    assert ids.count("example_issue_example-repo") == 1


def test_qdrant_collection_without_sentinel_reingests_full(monkeypatch):
    """Re-ingest the whole repo when an existing collection holds no sentinel.

    An interrupted full ingest leaves the collection populated but sentinel-free, so the
    constructor has to take the full-ingest path and finish with the sentinel point last.
    """
    client = FakeQdrantClient()
    _stub_constructor_dependencies(monkeypatch, client)

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert len(client.upserts) == 1
    _, points = client.upserts[0]
    ids = [point.payload["id"] for point in points]
    assert ids[:-1] == ["issue_2.issue", "issue_1.issue"]
    assert ids[-1] == "example_issue_example-repo"
