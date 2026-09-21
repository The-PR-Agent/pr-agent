"""Focused unit tests for the pinecone backend of the similar issue tool."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_pinecone_indexes_exactly_max_issues(monkeypatch):
    saved_vectors = []

    class FakeIndex:
        def upsert(self, **kwargs):
            saved_vectors.extend(kwargs["vectors"])

        def delete(self, **kwargs):
            pass

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    tool.max_issues_to_scan = 3
    _stub_embeddings(monkeypatch)

    issues = [_make_issue(number) for number in range(1, 6)]
    tool._update_index_with_issues(issues, "example-repo", pinecone_namespace="ns", upsert=True)

    issue_ids = [
        vector[0] for vector in saved_vectors
        if vector[0].endswith(".issue")
    ]
    assert issue_ids == ["issue_1.issue", "issue_2.issue", "issue_3.issue"]


def test_pinecone_namespace_does_not_collapse_repo_separators():
    assert psi._pinecone_namespace("foo/bar-baz") != psi._pinecone_namespace("foo-bar/baz")


def test_pinecone_upsert_passes_batch_size_and_max_concurrency(monkeypatch):
    upsert_calls = []
    pinecone_namespace = psi._pinecone_namespace("Example/Repo")

    class FakeIndex:
        def upsert(self, **kwargs):
            upsert_calls.append(kwargs)

        def delete(self, **kwargs):
            pass

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)

    tool._update_index_with_issues(
        [_make_issue(7)],
        "example-repo",
        pinecone_namespace=pinecone_namespace,
        upsert=True,
    )

    assert upsert_calls[0]["namespace"] == pinecone_namespace
    assert upsert_calls[0]["batch_size"] == 100
    assert upsert_calls[0]["max_concurrency"] == 10
    assert [vector[0] for vector in upsert_calls[0]["vectors"]] == ["issue_7.issue"]
    assert [vector[0] for vector in upsert_calls[1]["vectors"]] == [
        "example_issue_example-repo"
    ]
    assert "batch_size" not in upsert_calls[1]


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

        def delete(self, **kwargs):
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


def test_pinecone_upsert_response_errors_raise_before_sleep(monkeypatch):
    logged = []

    class FakeIndex:
        def upsert(self, **kwargs):
            return SimpleNamespace(has_errors=True, failed_item_count=100)

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)
    monkeypatch.setattr(psi.time, "sleep", lambda seconds: pytest.fail("should not sleep after upsert errors"))
    monkeypatch.setattr(
        psi,
        "get_logger",
        lambda: SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: logged.append(kwargs.get("artifact")),
        ),
    )

    with pytest.raises(RuntimeError, match="100"):
        tool._update_index_with_issues(
            [_make_issue(7)],
            "example-repo",
            pinecone_namespace="ns",
            upsert=True,
        )

    assert logged == [{"failed_item_count": 100, "errors": []}]


def test_pinecone_upsert_response_dict_errors_raise(monkeypatch):
    class FakeIndex:
        def upsert(self, **kwargs):
            return {"has_errors": True, "failed_item_count": 5}

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)

    with pytest.raises(RuntimeError, match="5"):
        tool._update_index_with_issues(
            [_make_issue(7)],
            "example-repo",
            pinecone_namespace="ns",
            upsert=True,
        )


def test_pinecone_upsert_failed_item_count_raises_without_has_errors(monkeypatch):
    class FakeIndex:
        def upsert(self, **kwargs):
            return {"failed_item_count": 3}

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)

    with pytest.raises(RuntimeError, match="3"):
        tool._update_index_with_issues(
            [_make_issue(7)],
            "example-repo",
            pinecone_namespace="ns",
            upsert=True,
        )


def test_pinecone_upsert_logs_batch_error_messages(monkeypatch):
    logged = []

    class FakeIndex:
        def upsert(self, **kwargs):
            return SimpleNamespace(
                has_errors=True,
                failed_item_count=2,
                errors=[
                    SimpleNamespace(error_message="namespace does not exist"),
                    {"error_message": "duplicate id"},
                ],
            )

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)
    monkeypatch.setattr(
        psi,
        "get_logger",
        lambda: SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: logged.append(kwargs.get("artifact")),
        ),
    )

    with pytest.raises(RuntimeError, match="2"):
        tool._update_index_with_issues(
            [_make_issue(7)],
            "example-repo",
            pinecone_namespace="ns",
            upsert=True,
        )

    assert logged == [
        {
            "failed_item_count": 2,
            "errors": ["namespace does not exist", "duplicate id"],
        },
    ]


def test_pinecone_upsert_waits_until_lsn_is_reconciled(monkeypatch):
    fetched = []
    sleeps = []

    class FakeIndex:
        def upsert(self, **kwargs):
            return SimpleNamespace(
                has_errors=False,
                response_info=SimpleNamespace(lsn_committed=10),
            )

        def fetch(self, **kwargs):
            fetched.append(kwargs)
            lsn_reconciled = 5 if len(fetched) == 1 else 10
            return SimpleNamespace(response_info=SimpleNamespace(
                is_reconciled=lambda target: lsn_reconciled >= target
            ))

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)
    monkeypatch.setattr(psi.time, "sleep", lambda seconds: sleeps.append(seconds))

    tool._update_index_with_issues(
        [_make_issue(7)],
        "example-repo",
        pinecone_namespace="ns",
        upsert=True,
    )

    assert fetched == [
        {"ids": ["example_issue_example-repo"], "namespace": "ns"},
        {"ids": ["example_issue_example-repo"], "namespace": "ns"},
    ]
    assert sleeps == [psi.PINECONE_UPSERT_READY_POLL_SECONDS]


def test_pinecone_upsert_wait_supports_dict_response_info():
    class FakeIndex:
        def __init__(self):
            self.fetch_calls = 0

        def fetch(self, **kwargs):
            self.fetch_calls += 1
            return {"response_info": {"lsn_reconciled": 20}}

    fake_index = FakeIndex()

    psi._wait_for_pinecone_upsert_readiness(
        fake_index,
        {"response_info": {"lsn_committed": 20}},
        namespace="",
        vector_id="example_issue_example-repo",
    )

    assert fake_index.fetch_calls == 1


def test_pinecone_upsert_wait_checks_deadline_before_fetch(monkeypatch):
    state = {"t": 0.0}
    fetched = []

    class FakeIndex:
        def fetch(self, **kwargs):
            fetched.append(kwargs)
            return {"response_info": {"lsn_reconciled": 1}}

    monkeypatch.setattr(psi.time, "monotonic", lambda: state["t"])
    monkeypatch.setattr(
        psi.time,
        "sleep",
        lambda seconds: state.__setitem__(
            "t", state["t"] + psi.PINECONE_UPSERT_READY_TIMEOUT_SECONDS + 1
        ),
    )

    with pytest.raises(TimeoutError, match="not query-ready"):
        psi._wait_for_pinecone_upsert_readiness(
            FakeIndex(),
            {"response_info": {"lsn_committed": 10}},
            namespace="ns",
            vector_id="example_issue_example-repo",
        )

    assert len(fetched) == 1


def test_pinecone_create_index_path_builds_new_index_then_upserts(monkeypatch):
    created = []
    upserted = []

    class FakeIndex:
        def upsert(self, **kwargs):
            upserted.append(kwargs)

        def delete(self, **kwargs):
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
        upsert=False,
    )

    assert len(created) == 1
    kwargs = created[0]
    assert kwargs["name"] == tool.index_name
    assert kwargs["dimension"] == 2
    assert kwargs["metric"] == "cosine"
    assert kwargs["timeout"] == 120
    assert upserted, "expected the upsert to run after index creation"


def test_pinecone_interrupted_ingest_without_sentinel_reingests_full(monkeypatch):
    """Re-run the full ingest when an interrupted run left no sentinel.

    The sentinel is the last row written, so an index holding the newest issues without a
    sentinel row proves the previous run was interrupted. The next run has to re-index the
    whole repo instead of assuming the first indexed issue closes the scan frontier,
    otherwise the missing older issues stay out of the index forever.
    """
    fetches = []
    upserted = []
    deletes = []

    class FakeIndex:
        def fetch(self, **kwargs):
            fetches.append(kwargs["ids"])
            return SimpleNamespace(to_dict=lambda: {"vectors": {}})

        def upsert(self, **kwargs):
            upserted.append(kwargs["vectors"])

        def delete(self, **kwargs):
            deletes.append(kwargs["ids"])

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
                    get_issues=lambda state: [_make_issue(3), _make_issue(2), _make_issue(1)],
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
    _stub_embeddings(monkeypatch)

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert fetches == [["example_issue_example-repo"]]
    assert deletes == [["example_issue_example-repo"]]
    assert [vector[0] for vector in upserted[0]] == [
        "issue_3.issue",
        "issue_2.issue",
        "issue_1.issue",
    ]
    assert [vector[0] for vector in upserted[1]] == ["example_issue_example-repo"]


def test_pinecone_failed_issue_batch_skips_sentinel_upsert(monkeypatch):
    """Revoke the sentinel and abort before writing any completion marker on failure.

    The sentinel is deleted before the batched issue write and recreated only after it
    succeeds, so an exception from the issue upsert must leave the marker absent.
    """
    upsert_calls = []
    deletes = []

    class FakeIndex:
        def upsert(self, **kwargs):
            upsert_calls.append(kwargs)
            if not kwargs["vectors"][0][0].startswith("example_issue_"):
                raise RuntimeError("issue batch failed")

        def delete(self, **kwargs):
            deletes.append(kwargs["ids"])

    tool = _make_tool(SimpleNamespace(Index=lambda name: FakeIndex()))
    _stub_embeddings(monkeypatch)

    with pytest.raises(RuntimeError):
        tool._update_index_with_issues(
            [_make_issue(7)],
            "example-repo",
            pinecone_namespace="ns",
            upsert=True,
        )

    assert deletes == [["example_issue_example-repo"]]
    assert len(upsert_calls) == 1
    assert not any(
        vector[0].startswith("example_issue_")
        for call in upsert_calls
        for vector in call["vectors"]
    )


def test_pinecone_incremental_backfills_an_older_index_gap(monkeypatch):
    """Keep scanning past indexed issues and backfill an older missing issue.

    With the sentinel and the newest issue already indexed, a deleted older issue sits
    below the scan frontier; a normal incremental run must find it and re-add it without a
    forced refresh or a full re-ingest.
    """
    store = {
        "issue_5.issue": "example-repo",
        "example_issue_example-repo": "example-repo",
    }
    upserted = []

    class FakeIndex:
        def fetch(self, **kwargs):
            id = kwargs["ids"][0]
            return SimpleNamespace(to_dict=lambda: {
                "vectors": {"id": {"metadata": {"repo": "example-repo"}}}
                if id in store else {}
            })

        def upsert(self, **kwargs):
            upserted.append(kwargs["vectors"])
            for vector in kwargs["vectors"]:
                store[vector[0]] = "example-repo"

        def delete(self, **kwargs):
            for id in kwargs["ids"]:
                store.pop(id, None)

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
                    get_issues=lambda state: [_make_issue(5), _make_issue(4)],
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
    _stub_embeddings(monkeypatch)

    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert "issue_4.issue" in store
    assert store["example_issue_example-repo"] == "example-repo"
    issue_ids = {
        vector[0]
        for call in upserted
        for vector in call
        if vector[0] != "example_issue_example-repo"
    }
    assert issue_ids == {"issue_4.issue"}


def test_pinecone_failed_incremental_write_triggers_full_reingest(monkeypatch):
    """Re-ingest in full when a failed write revoked the sentinel.

    A completed previous ingest seeded the sentinel and one issue. When the next incremental
    write fails after revoking the sentinel, the following constructor must take the
    full-ingest path instead of trusting the stale marker or the newest indexed issue.
    """
    store = {
        "issue_5.issue": "example-repo",
        "example_issue_example-repo": "example-repo",
    }
    upserted = []
    fail_write = True

    class FakeIndex:
        def fetch(self, **kwargs):
            id = kwargs["ids"][0]
            return SimpleNamespace(to_dict=lambda: {
                "vectors": {"id": {"metadata": {"repo": "example-repo"}}}
                if id in store else {}
            })

        def upsert(self, **kwargs):
            upserted.append(kwargs["vectors"])
            if fail_write and not kwargs["vectors"][0][0].startswith("example_issue_"):
                raise RuntimeError("incremental write failed")
            for vector in kwargs["vectors"]:
                store[vector[0]] = "example-repo"

        def delete(self, **kwargs):
            for id in kwargs["ids"]:
                store.pop(id, None)

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
                    get_issues=lambda state: [_make_issue(6), _make_issue(5)],
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
    _stub_embeddings(monkeypatch)

    with pytest.raises(RuntimeError):
        psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert store.get("example_issue_example-repo") is None

    fail_write = False
    psi.PRSimilarIssue("https://github.com/Example/Repo/issues/1", ai_handler=None)

    assert store["example_issue_example-repo"] == "example-repo"
    issue_ids = {
        vector[0]
        for call in upserted
        for vector in call
        if vector[0] != "example_issue_example-repo"
    }
    assert issue_ids == {"issue_6.issue", "issue_5.issue"}


def test_vectordb_defaults_to_lancedb():
    config_path = Path(psi.__file__).resolve().parents[2] / "pr_agent" / "settings" / "configuration.toml"
    content = config_path.read_text(encoding="utf-8")
    section = content.split("[pr_similar_issue]")[1].split("[")[0]

    assert 'vectordb = "lancedb"' in section
    assert 'vectordb = "pinecone"' not in section
