import pytest
from dynaconf import Dynaconf
from starlette_context import request_cycle_context

from pr_agent import config_loader, git_providers
from pr_agent.tools.pr_config import PRConfig

PR_URL = "https://github.com/example/repo/pull/1"


@pytest.fixture
def provider_class(monkeypatch):
    class StubProvider:
        instances = []

        def __init__(self, pr_url):
            self.pr_url = pr_url
            self.instances.append(self)

    settings = Dynaconf(config={"git_provider": "test"}, core_loaders=[], loaders=[])
    monkeypatch.setattr(config_loader, "global_settings", settings)
    monkeypatch.setattr(git_providers, "_GIT_PROVIDERS", {"test": StubProvider})
    return StubProvider


def test_config_reuses_provider_initialized_for_request(provider_class):
    with request_cycle_context({"settings": config_loader.get_settings()}):
        request_provider = git_providers.get_git_provider_with_context(PR_URL)
        config = PRConfig(PR_URL)

    assert config.git_provider is request_provider
    assert provider_class.instances == [request_provider]
    assert request_provider.pr_url == PR_URL


def test_config_creates_provider_without_request_context(provider_class):
    config = PRConfig(PR_URL)

    assert type(config.git_provider) is provider_class
    assert provider_class.instances == [config.git_provider]
    assert config.git_provider.pr_url == PR_URL


def test_config_uses_final_provider_class_when_selection_changes(provider_class, monkeypatch):
    class CachedProvider(provider_class):
        pass

    monkeypatch.setitem(git_providers._GIT_PROVIDERS, "cached", CachedProvider)
    settings = config_loader.get_settings()
    with request_cycle_context({"settings": settings}):
        settings.set("config.git_provider", "cached")
        request_provider = git_providers.get_git_provider_with_context(PR_URL)
        settings.set("config.git_provider", "test")
        config = PRConfig(PR_URL)

    assert type(config.git_provider) is provider_class
    assert provider_class.instances == [request_provider, config.git_provider]
    assert config.git_provider.pr_url == PR_URL


def test_config_output_hides_host_only_keys():
    settings = config_loader.get_settings(use_context=False)
    original = settings.get("CONFIG").to_dict()
    settings.set("config.global_settings_repo", "private/config")
    try:
        output = PRConfig.__new__(PRConfig)._prepare_pr_configs()
    finally:
        settings.set("CONFIG", original)

    assert "config.model = " in output
    assert "global_settings_repo" not in output
