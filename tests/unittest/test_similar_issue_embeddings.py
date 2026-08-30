"""Embed with the openai>=1.0 client that requirements.txt pins."""
import inspect

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
