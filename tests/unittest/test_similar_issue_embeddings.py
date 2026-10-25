"""Embed with the openai>=1.0 client that requirements.txt pins."""
import asyncio
import inspect
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pr_agent.tools.pr_similar_issue as psi


class SettingsStub:
    class openai:
        key = "unit-test-key"


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    monkeypatch.setattr(psi, "get_settings", SettingsStub)
    psi._EMBEDDING_CLIENTS.clear()
    yield
    psi._EMBEDDING_CLIENTS.clear()


def test_no_removed_v0_embedding_api_remains():
    """Assert the removed openai.Embedding.create call is gone; it raises APIRemovedInV1."""
    source = inspect.getsource(psi)

    assert "openai.Embedding.create" not in source


def test_no_module_level_api_key_assignment_remains():
    """Assert the v0 module-level openai.api_key assignment is gone."""
    source = inspect.getsource(psi)

    assert "openai.api_key" not in source


def test_embed_uses_the_v1_client(monkeypatch):
    """Call client.embeddings.create and unwrap response.data."""
    calls = {}

    class FakeEmbeddings:
        def create(self, input, model):
            calls["input"] = input
            calls["model"] = model
            return type("R", (), {"data": [type("D", (), {"embedding": [0.5]})()
                                           for _ in input]})()

    class FakeClient:
        def __init__(self, api_key=None):
            self.embeddings = FakeEmbeddings()

    monkeypatch.setattr(psi.openai, "OpenAI", FakeClient)

    assert psi._embed(["a", "b"]) == [[0.5], [0.5]]
    assert calls["input"] == ["a", "b"]
    assert calls["model"] == psi.MODEL


def test_a_total_embedding_failure_raises_instead_of_indexing_zero_vectors(monkeypatch):
    """Raise on a total embedding failure instead of indexing an all-zero vector set."""
    def always_fails(texts):
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(psi, "_embed", always_fails)

    with pytest.raises(RuntimeError, match="refusing to index all-zero vectors"):
        psi._embed_with_fallback(["a", "b"])


def test_a_partial_embedding_failure_zeroes_only_the_failing_item(monkeypatch):
    """Zero only the item that fails, keeping the vectors that were embedded."""
    def fails_for_b(texts):
        if texts != ["a"]:
            raise RuntimeError("embedding backend down")
        return [[0.5]]

    monkeypatch.setattr(psi, "_embed", fails_for_b)

    assert psi._embed_with_fallback(["a", "b"]) == [[0.5], [0] * 1536]


def test_the_client_is_reused_across_calls(monkeypatch):
    """Reuse one client per key, so the one-by-one fallback does not rebuild it per item."""
    built = []

    class FakeClient:
        def __init__(self, api_key=None):
            built.append(api_key)
            self.embeddings = type("E", (), {
                "create": lambda _self, input, model: type(
                    "R", (), {"data": [type("D", (), {"embedding": [0.5]})() for _ in input]})()
            })()

    monkeypatch.setattr(psi.openai, "OpenAI", FakeClient)

    psi._embed(["a"])
    psi._embed(["b"])
    psi._embed(["c"])

    assert built == ["unit-test-key"]


def test_successful_embedding_keeps_a_single_bulk_request(monkeypatch):
    batches = []

    def embed(texts):
        batches.append(list(texts))
        return [[int(text)] for text in texts]

    monkeypatch.setattr(psi, "_embed", embed)
    assert psi._embed_with_fallback([str(i) for i in range(65)]) == [[i] for i in range(65)]
    assert [len(batch) for batch in batches] == [65]


def test_failed_bulk_embedding_retries_in_ordered_batches(monkeypatch):
    batches = []

    def embed(texts):
        batches.append(list(texts))
        if len(texts) > 32:
            raise RuntimeError("oversized request")
        return [[int(text)] for text in texts]

    monkeypatch.setattr(psi, "_embed", embed)
    assert psi._embed_with_fallback([str(i) for i in range(65)]) == [[i] for i in range(65)]
    assert [len(batch) for batch in batches] == [65, 32, 32, 1]


def test_failed_embedding_batch_retries_only_its_items(monkeypatch):
    batches = []

    def embed(texts):
        batches.append(list(texts))
        if len(texts) > 32 or (len(texts) > 1 and texts[0] == "32"):
            raise RuntimeError("batch rejected")
        if texts == ["33"]:
            raise RuntimeError("item rejected")
        return [[int(text)] for text in texts]

    monkeypatch.setattr(psi, "_embed", embed)
    result = psi._embed_with_fallback([str(i) for i in range(65)])

    assert result[:33] == [[i] for i in range(33)]
    assert result[33] == [0] * 1536
    assert result[34:] == [[i] for i in range(34, 65)]
    assert [len(batch) for batch in batches] == [65, 32, 32] + [1] * 33


def test_short_embedding_responses_retry_without_misaligning_vectors(monkeypatch):
    requests = []

    def embed(texts):
        requests.append(list(texts))
        if len(texts) == 65 or (len(texts) == 32 and texts[0] == "32"):
            return [[int(text)] for text in texts[:-1]]
        return [[int(text)] for text in texts]

    monkeypatch.setattr(psi, "_embed", embed)
    assert psi._embed_with_fallback([str(i) for i in range(65)]) == [[i] for i in range(65)]
    assert [len(batch) for batch in requests] == [65, 32, 32] + [1] * 33


@pytest.mark.asyncio
async def test_pinecone_query_branch_runs(monkeypatch):
    queried = []

    class FakeIndex:
        def query(self, *args, **kwargs):
            queried.append(kwargs)
            return SimpleNamespace(to_dict=lambda: {"matches": []})

    monkeypatch.setattr(psi, "_embed", lambda texts: [[0.5]])
    monkeypatch.setattr(
        psi,
        "get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(publish_output=False),
            pr_similar_issue=SimpleNamespace(vectordb="pinecone", skip_comments=True),
        ),
    )

    issue = SimpleNamespace(title="Issue", body="Body", number=1, get_comments=lambda: [])
    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)
    tool.supported = True
    tool.issue_url = "https://github.com/example/repo/issues/1"
    tool.index_name = "issues"
    tool.repo_name_for_index = "example-repo"
    tool.pinecone_namespace = psi._pinecone_namespace("example/repo")
    tool.pc = SimpleNamespace(Index=lambda name: FakeIndex())
    tool.git_provider = SimpleNamespace(
        _parse_issue_url=lambda url: ("example/repo", 1),
        repo_obj=SimpleNamespace(get_issue=lambda number: issue),
    )

    assert await tool.run() is None
    assert queried, "run() never entered the pinecone branch"
    assert queried[0]["vector"] == [0.5]
    assert queried[0]["namespace"] == psi._pinecone_namespace("example/repo")


@pytest.mark.parametrize("skip_comments", [False, True])
def test_process_issue_preserves_configured_comment_indexing(monkeypatch, skip_comments):
    monkeypatch.setattr(psi, "get_settings", lambda: SimpleNamespace(
        pr_similar_issue=SimpleNamespace(skip_comments=skip_comments),
    ))
    comments = [SimpleNamespace(body="Existing comment")]
    issue = SimpleNamespace(title="Issue", body="Body", number=1, get_comments=Mock(return_value=iter(comments)))
    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)

    issue_str, returned_comments, number = tool._process_issue(issue)

    assert issue_str == "Issue Header: \"Issue\"\n\nIssue Body:\nBody"
    assert list(returned_comments) == ([] if skip_comments else comments)
    assert number == 1
    assert issue.get_comments.call_count == (0 if skip_comments else 1)


@pytest.fixture(params=["pinecone", "lancedb", "qdrant"])
def query_tool(monkeypatch, request):
    """Exercise each query branch with ranked rows and an observable publication sink."""
    backend = request.param
    state = SimpleNamespace(attempts=[], responses=[], published=[], embedded=[], fetched=[],
                            comment_fetches=[], publish=True)

    def query(**kwargs):
        state.attempts.append(kwargs)
        rows = state.responses.pop(0)
        if isinstance(rows, BaseException):
            raise rows
        if rows is None:
            if backend == "pinecone":
                return SimpleNamespace(to_dict=lambda: {})
            if backend == "lancedb":
                return [{}]
            return SimpleNamespace(points=[SimpleNamespace(payload={}, score=0.9)])
        if backend == "pinecone":
            return SimpleNamespace(to_dict=lambda: {"matches": [{"id": rid, "score": score} for rid, score in rows]})
        if backend == "lancedb":
            return [{"id": rid, "_distance": 1 - score} for rid, score in rows]
        return SimpleNamespace(points=[SimpleNamespace(payload={"id": rid}, score=score) for rid, score in rows])

    class Builder:
        def distance_type(self, value):
            self.metric = value
            return self

        def limit(self, value):
            self.count = value
            return self

        def where(self, expression, prefilter=False):
            self.filter = (expression, prefilter)
            return self

        def to_list(self):
            return query(vector=[0.5], metric=self.metric, limit=self.count, filter=self.filter)

    def embed(texts):
        state.embedded.append(texts)
        return [[0.5]]

    def get_issue(number):
        state.fetched.append(number)

        def get_comments():
            state.comment_fetches.append(number)
            return [SimpleNamespace(html_url=f"https://example.test/{number}#comment-0")]

        return SimpleNamespace(
            title=f"Issue {number}", body="Body", number=number,
            html_url=f"https://github.com/example/repo/issues/{number}",
            get_comments=get_comments,
            create_comment=state.published.append,
        )

    monkeypatch.setattr(psi, "_embed", embed)
    monkeypatch.setattr(psi, "get_settings", lambda: SimpleNamespace(
        config=SimpleNamespace(publish_output=state.publish),
        pr_similar_issue=SimpleNamespace(vectordb=backend, skip_comments=False),
    ))
    monkeypatch.setitem(sys.modules, "qdrant_client.models", SimpleNamespace(
        FieldCondition=SimpleNamespace, Filter=SimpleNamespace, MatchValue=SimpleNamespace,
    ))
    tool = psi.PRSimilarIssue.__new__(psi.PRSimilarIssue)
    tool.supported = True
    tool.issue_url = "https://github.com/example/repo/issues/1"
    tool.index_name = "issues"
    tool.repo_name_for_index = "example-repo"
    tool.pinecone_namespace = psi._pinecone_namespace("example/repo")
    tool.qdrant_collection_name = "issues-v2"
    tool.pc = SimpleNamespace(Index=lambda name: SimpleNamespace(query=query))
    tool.qdrant = SimpleNamespace(query_points=query)
    tool.table = SimpleNamespace(search=lambda vector: Builder())
    tool.git_provider = SimpleNamespace(
        _parse_issue_url=lambda url: ("example/repo", 1), repo_obj=SimpleNamespace(get_issue=get_issue),
    )
    state.tool = tool
    yield state
    assert len(state.embedded) == 1
    for call in state.attempts:
        if backend == "pinecone":
            assert call == dict(vector=[0.5], top_k=call["top_k"], filter={"repo": "example-repo"},
                                namespace=tool.pinecone_namespace, include_metadata=True)
        elif backend == "lancedb":
            assert call == dict(vector=[0.5], metric="cosine", limit=call["limit"],
                                filter=("metadata.repo='example-repo'", True))
        else:
            assert call["collection_name"] == "issues-v2"
            assert call["query"] == [0.5]
            assert call["with_payload"] is True
            condition, = call["query_filter"].must
            assert (condition.key, condition.match.value) == ("metadata.repo", "example-repo")


def _limits(state):
    return [call.get("top_k", call.get("limit")) for call in state.attempts]


@pytest.mark.asyncio
async def test_healthy_query_keeps_five_results_and_one_request(query_tool):
    state = query_tool
    state.responses = [[(f"issue_{n}.issue", 0.9) for n in range(2, 7)]]
    await state.tool.run()
    assert _limits(state) == [5]
    assert state.fetched == [1, 2, 3, 4, 5, 6]
    assert state.comment_fetches == []
    assert state.embedded == [['Issue Header: "Issue 1"\n\nIssue Body:\nBody']]
    assert len(state.published) == 1
    assert "5. **[Issue 6]" in state.published[0]


@pytest.mark.asyncio
async def test_full_filtered_query_retries_and_uses_only_terminal_order(query_tool):
    state = query_tool
    state.responses = [
        [("issue_1.issue", 1), ("issue_1.comment_bad", 0.99), ("example_issue_repo", 0.98),
         ("issue_20.issue", 0.97), ("issue_20.comment_0", 0.96)],
        [("issue_1.issue", 1), ("issue_2.comment_bad", 0.99), ("issue_3.comment_0", 0.95),
         ("issue_3.issue", 0.94), ("issue_4.issue", 0.90), ("bad.issue", 0.89),
         ("issue_5.issue", 0.80), ("issue_6.issue", 0.70), ("issue_7.issue", 0.60), ("issue_8.issue", 0.5)],
    ]
    await state.tool.run()
    assert _limits(state) == [5, 10]
    assert state.fetched == [1, 3, 4, 5, 6, 7]
    assert len(state.published) == 1
    body = state.published[0]
    assert "1. **[Issue 3](https://example.test/3#comment-0)** (score=0.95)" in body
    assert "2. **[Issue 4](https://github.com/example/repo/issues/4)** (score=0.90)" in body
    assert "5. **[Issue 7]" in body
    assert "Issue 20" not in body


@pytest.mark.asyncio
async def test_short_response_does_not_retry(query_tool):
    state = query_tool
    state.responses = [[("issue_1.issue", 1), ("issue_2.issue", 0.8)]]
    await state.tool.run()
    assert _limits(state) == [5]
    assert state.fetched == [1, 2]
    assert len(state.published) == 1


@pytest.mark.asyncio
async def test_second_full_response_cannot_start_a_third_query(query_tool):
    state = query_tool
    state.responses = [[("issue_1.issue", 1)] * 5, [("issue_1.issue", 1)] * 10]
    state.publish = False
    await state.tool.run()
    assert _limits(state) == [5, 10]
    assert state.published == []
    assert state.fetched == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("query failed"), asyncio.CancelledError(), None])
async def test_retry_failure_does_not_publish_partial_first_response(query_tool, failure):
    state = query_tool
    state.responses = [[("issue_2.issue", 0.9)] * 5, failure]
    with pytest.raises(type(failure) if failure is not None else KeyError):
        await state.tool.run()
    assert _limits(state) == [5, 10]
    assert state.published == []
    assert state.fetched == [1]


@pytest.mark.asyncio
async def test_unresolved_comment_url_prevents_publication(query_tool):
    state = query_tool
    state.responses = [[("issue_2.comment_9", 0.9)]]
    with pytest.raises(IndexError):
        await state.tool.run()
    assert state.published == []


@pytest.mark.parametrize("rid", [None, 17, "bad.issue", "issue_bad.comment_0", "issue_2.comment_bad",
                                  "issue_2.bad_comment_0", "issue_2.comment_0.extra", "issue_1.comment_bad",
                                  "example_issue_repo", "other_2.issue", "issue_2.garbage", "issue_2"])
def test_ineligible_ids_leave_valid_hits_aligned(rid):
    hits = [(rid, 1), ("issue_2.comment_0", 0.9), ("issue_2.issue", 0.8), ("issue_3.issue", 0.7)]
    assert psi.PRSimilarIssue._select_similar_hits(hits, 1) == ([2, 3], [0, -1], ["0.90", "0.70"])
