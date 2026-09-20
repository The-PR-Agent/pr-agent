"""Focused unit tests for the pinecone backend of the similar issue tool."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pr_agent.tools.pr_similar_issue as psi


class SettingsStub:
    class CONFIG:
        CLI_MODE = True

    class pr_similar_issue:
        skip_comments = True
        max_issues_to_scan = 100
        vectordb = "pinecone"
        force_update_dataset = False

    class pinecone:
        api_key = "pinecone-key"
        cloud = "aws"
        region = "us-east-1"


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


def _make_tool(pc):
    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)
    tool.pc = pc
    tool.pc_spec = None
    tool.index_name = "codium-ai-pr-agent-issues"
    tool.max_issues_to_scan = 100
    tool.token_handler = SimpleNamespace(count_tokens=lambda text: 1)
    tool.pinecone_index = None
    return tool


def _stub_embeddings(monkeypatch):
    monkeypatch.setitem(sys.modules, "pandas",
                        SimpleNamespace(DataFrame=_PandasDataFrame))
    embedding = [0.5, 0.5]
    monkeypatch.setattr(psi, "get_settings", lambda: SettingsStub)
    monkeypatch.setattr(psi, "get_max_tokens", lambda model: 8192)
    monkeypatch.setattr(psi, "_embed_with_fallback",
                        lambda texts: [embedding for _ in texts])
    monkeypatch.setattr(psi.time, "sleep", lambda seconds: None)


def test_pinecone_namespace_does_not_collapse_repo_separators():
    assert psi._pinecone_namespace("foo/bar-baz") != psi._pinecone_namespace("foo-bar/baz")


def test_pinecone_upsert_passes_batch_size_and_max_concurrency(monkeypatch):
    calls = {}
    pinecone_namespace = psi._pinecone_namespace("Example/Repo")

    class FakeIndex:
        def upsert(self, **kwargs):
            calls.update(kwargs)

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)

    tool._update_index_with_issues(
        [_make_issue(7)],
        "example-repo",
        pinecone_namespace=pinecone_namespace,
        upsert=True,
    )

    assert calls["namespace"] == pinecone_namespace
    assert calls["batch_size"] == 100
    assert calls["max_concurrency"] == 10
    assert [vector[0] for vector in calls["vectors"]] == [
        "example_issue_example-repo",
        "issue_7.issue",
    ]


def test_pinecone_existing_index_fetches_from_repo_namespace(monkeypatch):
    fetches = []

    class FakeIndex:
        def fetch(self, **kwargs):
            fetches.append(kwargs)
            return SimpleNamespace(to_dict=lambda: {"vectors": {
                kwargs["ids"][0]: {"metadata": {"repo": "example-repo"}},
            }})

    class FakePineconeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def has_index(self, index_name):
            return True

        def Index(self, name):
            return FakeIndex()

    class FakeProvider:
        @staticmethod
        def supports_issue_indexing():
            return True

        def __init__(self):
            self.github_client = SimpleNamespace(
                get_repo=lambda repo_name: SimpleNamespace(
                    full_name="Example/Repo",
                    get_issues=lambda state: [_make_issue(7)],
                )
            )

        def _parse_issue_url(self, issue_url):
            return "Example/Repo", 1

    fake_pinecone_module = SimpleNamespace(
        Pinecone=FakePineconeClient,
        ServerlessSpec=lambda cloud, region: SimpleNamespace(cloud=cloud, region=region),
    )

    monkeypatch.setitem(sys.modules, "pinecone", fake_pinecone_module)
    monkeypatch.setattr(psi, "get_settings", lambda: SettingsStub)
    monkeypatch.setattr(psi, "get_git_provider", lambda: FakeProvider)

    tool = psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)
    pinecone_namespace = psi._pinecone_namespace("Example/Repo")

    assert tool.repo_name_for_index == "example-repo"
    assert tool.pinecone_namespace == pinecone_namespace
    assert fetches == [
        {
            "ids": ["example_issue_example-repo"],
            "namespace": pinecone_namespace,
        },
        {
            "ids": ["issue_7.issue"],
            "namespace": pinecone_namespace,
        }
    ]


def test_pinecone_upsert_path_skips_index_creation(monkeypatch):
    created = []

    class FakeIndex:
        def upsert(self, **kwargs):
            pass

    pc = SimpleNamespace(
        Index=lambda name: FakeIndex(),
        create_index=lambda **kwargs: created.append(kwargs),
    )
    tool = _make_tool(pc)
    _stub_embeddings(monkeypatch)

    tool._update_index_with_issues(
        [_make_issue(7)],
        "example-repo",
        pinecone_namespace=psi._pinecone_namespace("Example/Repo"),
        upsert=True,
    )

    assert created == []


def test_pinecone_create_index_path_builds_new_index_then_upserts(monkeypatch):
    created = []
    upserted = []

    class FakeIndex:
        def upsert(self, **kwargs):
            upserted.append(kwargs)

    pc = SimpleNamespace(
        Index=lambda name: FakeIndex(),
        create_index=lambda **kwargs: created.append(kwargs),
    )
    tool = _make_tool(pc)
    _stub_embeddings(monkeypatch)

    tool._update_index_with_issues(
        [_make_issue(7)],
        "example-repo",
        pinecone_namespace=psi._pinecone_namespace("Example/Repo"),
        upsert=False,
    )

    assert len(created) == 1
    kwargs = created[0]
    assert kwargs["name"] == tool.index_name
    assert kwargs["dimension"] == 2
    assert kwargs["metric"] == "cosine"
    assert kwargs["timeout"] == 120
    assert upserted, "expected the upsert to run after index creation"


def test_vectordb_defaults_to_lancedb():
    config_path = Path(psi.__file__).resolve().parents[2] / "pr_agent" / "settings" / "configuration.toml"
    content = config_path.read_text(encoding="utf-8")
    section = content.split("[pr_similar_issue]")[1].split("[")[0]

    assert 'vectordb = "lancedb"' in section
    assert 'vectordb = "pinecone"' not in section
