"""The qdrant credentials guard treats a blank url as unset instead of targeting localhost."""
import sys
import types
from types import SimpleNamespace

import pytest

import pr_agent.tools.pr_similar_issue as psi

CREDENTIALS_MESSAGE = "Please set qdrant url and api key in secrets file"


class _Recorder:
    def __init__(self):
        self.posted = []

    def create_comment(self, body):
        self.posted.append(body)


class _Repo:
    full_name = "org/repo"

    def __init__(self):
        self.recorder = _Recorder()

    def get_issue(self, number):
        return self.recorder


class _Settings:
    def __init__(self, cli_mode, url, api_key_secret=True):
        self.CONFIG = SimpleNamespace(CLI_MODE=cli_mode)
        self.pr_similar_issue = SimpleNamespace(
            max_issues_to_scan=10, skip_comments=True, force_update_dataset=False, vectordb="qdrant"
        )
        kwargs = {"api_key": "secret"} if api_key_secret else {}
        if url is not None:
            kwargs["url"] = url
        self.qdrant = SimpleNamespace(**kwargs)


def _install_fakes(monkeypatch, settings, repo):
    fake_client_module = types.ModuleType("qdrant_client")
    fake_client_module.QdrantClient = lambda **kwargs: SimpleNamespace(**kwargs)
    fake_models = types.ModuleType("qdrant_client.models")
    for name in ("Distance", "FieldCondition", "Filter", "MatchValue", "VectorParams"):
        setattr(fake_models, name, SimpleNamespace)
    fake_client_module.models = fake_models
    monkeypatch.setitem(sys.modules, "qdrant_client", fake_client_module)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_models)

    class _FakeProvider:
        @classmethod
        def supports_issue_indexing(cls):
            return True

        def __init__(self):
            self.github_client = SimpleNamespace(get_repo=lambda name: repo)
            self._parse_issue_url = lambda url: ("org/repo", "42")

    monkeypatch.setattr(psi, "get_git_provider", lambda: _FakeProvider)
    monkeypatch.setattr(psi, "get_settings", lambda: settings)
    monkeypatch.setattr(
        psi, "TokenHandler", lambda *a, **k: SimpleNamespace(count_tokens=lambda _: 0)
    )


def _build_tool(monkeypatch, settings, repo=None):
    repo = repo or _Repo()
    _install_fakes(monkeypatch, settings, repo)
    return repo, psi.PRSimilarIssue("https://github.com/org/repo/issues/42", ai_handler=None)


def test_blank_qdrant_url_is_treated_as_unset(monkeypatch):
    """A blank url from the secrets template raises instead of targeting localhost."""
    with pytest.raises(Exception, match=CREDENTIALS_MESSAGE):
        _build_tool(monkeypatch, _Settings(cli_mode=True, url=""))


def test_blank_qdrant_url_raises_even_with_an_api_key(monkeypatch):
    """The client would force https whenever api_key is not None, so url alone decides."""
    with pytest.raises(Exception, match=CREDENTIALS_MESSAGE):
        _build_tool(monkeypatch, _Settings(cli_mode=True, url="", api_key_secret=False))


def test_missing_qdrant_url_still_raises(monkeypatch):
    """An absent url keeps the pre-existing behaviour of raising the guidance error."""
    with pytest.raises(Exception, match=CREDENTIALS_MESSAGE):
        _build_tool(monkeypatch, _Settings(cli_mode=True, url=None))


def test_blank_url_in_app_mode_posts_the_guidance_comment(monkeypatch):
    """App mode posts the guidance comment on the issue before raising."""
    repo = _Repo()
    with pytest.raises(Exception, match=CREDENTIALS_MESSAGE):
        _build_tool(monkeypatch, _Settings(cli_mode=False, url=""), repo=repo)

    assert repo.recorder.posted == [CREDENTIALS_MESSAGE]
