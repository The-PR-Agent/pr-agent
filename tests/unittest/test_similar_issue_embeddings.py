"""/similar_issue must use the openai>=1.0 client that requirements.txt pins."""
import inspect

import pytest

import pr_agent.tools.pr_similar_issue as psi


class SettingsStub:
    class openai:
        key = "sk-test"


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    monkeypatch.setattr(psi, "get_settings", lambda: SettingsStub())


def test_no_removed_v0_embedding_api_remains():
    """openai.Embedding.create was removed in openai 1.0 and raises APIRemovedInV1."""
    source = inspect.getsource(psi)

    assert "openai.Embedding.create" not in source


def test_no_module_level_api_key_assignment_remains():
    """openai.api_key is the v0 configuration style."""
    source = inspect.getsource(psi)

    assert "openai.api_key" not in source


def test_embed_uses_the_v1_client(monkeypatch):
    """The helper must call client.embeddings.create and unwrap response.data."""
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
    """Silently storing [0]*1536 for every issue made similarity results meaningless."""
    class FakeClient:
        def __init__(self, api_key=None):
            raise RuntimeError("embedding backend down")

    monkeypatch.setattr(psi.openai, "OpenAI", FakeClient)

    with pytest.raises(RuntimeError):
        psi._embed(["a"])
