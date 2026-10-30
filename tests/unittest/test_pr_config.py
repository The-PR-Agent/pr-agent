import pytest
from dynaconf import Dynaconf
from starlette_context import request_cycle_context

from pr_agent import config_loader, git_providers
from pr_agent.tools import pr_config
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
    settings.set("config.hidden_option", "hidden")
    settings.set("config.skip_keys", ["hidden_option"])
    try:
        output = PRConfig.__new__(PRConfig)._prepare_pr_configs()
    finally:
        settings.set("CONFIG", original)

    assert "config.model = " in output
    assert "global_settings_repo" not in output
    assert "hidden_option" not in output


@pytest.mark.parametrize("include_extras", [False, True])
def test_config_output_identifies_names_absent_from_shipped_declarations(monkeypatch, include_extras):
    settings = Dynaconf(
        config={"model": "selected", "skip_keys": []},
        pr_code_suggestions={"committable_code_suggestions": False, "publish_failure_comment": False},
        core_loaders=[], loaders=[], envvar_prefix=False,
    )
    declarations = {
        "Config": {"MoDeL": "shipped"},
        "Pr_Code_Suggestions": {"CoMmItTaBlE_CoDe_SuGgEsTiOnS": False, "PUBLISH_FAILURE_COMMENT": False},
    }
    monkeypatch.setattr(config_loader, "global_settings", settings)
    monkeypatch.setattr(pr_config, "Dynaconf", lambda **kwargs: declarations)
    if include_extras:
        settings.set("config.zebra_option", 9)
        settings.set("config.extension_option", "kept")
        settings.set("pr_code_suggestions.commitable_code_suggestions", True)

    output = PRConfig.__new__(PRConfig)._prepare_pr_configs()

    expected = (
        "<details> <summary><strong>🛠️ PR-Agent Configurations:</strong></summary> \n\n"
        "\n\n```yaml\n\n"
        "\n\n==================== CONFIG ===================="
        "\nconfig.model = 'selected'  "
    )
    if include_extras:
        expected += "\nconfig.zebra_option = 9  \nconfig.extension_option = 'kept'  "
    expected += (
        "\n\n==================== PR_CODE_SUGGESTIONS ===================="
        "\npr_code_suggestions.committable_code_suggestions = False  "
        "\npr_code_suggestions.publish_failure_comment = False  "
    )
    if include_extras:
        expected += "\npr_code_suggestions.commitable_code_suggestions = True  "
        note = (
            "\n\n# These effective keys are not declared in the shipped configuration:"
            "\n# They may be legacy aliases or extension settings; check them only if unexpected."
            "\n# - config.extension_option"
            "\n# - config.zebra_option"
            "\n# - pr_code_suggestions.commitable_code_suggestions"
        )
        expected += note
        opener = output.index("```yaml")
        closer = output.index("```", opener + len("```yaml"))
        assert opener < output.index(note) < closer
        assert "not declared" not in output[closer:]
        assert "config.extension_option" not in output[closer:]
        assert "config.zebra_option" not in output[closer:]
        assert "pr_code_suggestions.commitable_code_suggestions" not in output[closer:]
        assert output.count("```") == 2
    expected += "\n```\n</details>\n"
    assert output == expected


def test_config_output_has_no_declaration_note_when_shipped_loading_fails(monkeypatch):
    def fail_loading(**kwargs):
        raise RuntimeError("controlled shipped configuration loading failure")

    monkeypatch.setattr(pr_config, "Dynaconf", fail_loading)

    output = PRConfig.__new__(PRConfig)._prepare_pr_configs()

    assert output == (
        "<details> <summary><strong>🛠️ PR-Agent Configurations:</strong></summary> \n\n"
        "\n\n```yaml\n\n\n```\n</details>\n"
    )
