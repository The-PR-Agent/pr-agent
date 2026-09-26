from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.git_providers.gitlab_provider import GitLabProvider


@pytest.mark.parametrize("languages", [{"Python": 75.0, "JavaScript": 25.0}, {}])
def test_get_languages_reuses_result(languages):
    provider = object.__new__(GitLabProvider)
    provider.id_project = "group/project"
    project = SimpleNamespace(languages=MagicMock(return_value=languages))
    provider.gl = SimpleNamespace(projects=SimpleNamespace(get=MagicMock(return_value=project)))

    first = provider.get_languages()
    second = provider.get_languages()

    assert first == languages
    assert second is first
    provider.gl.projects.get.assert_called_once_with("group/project")
    project.languages.assert_called_once_with()


def test_get_languages_does_not_cache_failures():
    provider = object.__new__(GitLabProvider)
    provider.id_project = "group/project"
    project = SimpleNamespace(languages=MagicMock(side_effect=[RuntimeError("temporary"), {"Python": 100.0}]))
    provider.gl = SimpleNamespace(projects=SimpleNamespace(get=MagicMock(return_value=project)))

    with pytest.raises(RuntimeError, match="temporary"):
        provider.get_languages()

    assert provider.get_languages() == {"Python": 100.0}
    assert project.languages.call_count == 2
