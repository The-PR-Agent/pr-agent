import pytest
from unittest.mock import MagicMock, patch
from gitlab.v4.objects import Project, ProjectFile, ProjectWiki
from pr_agent.git_providers import git_provider as _gp
from pr_agent.git_providers.gitlab_provider import GitLabProvider


@pytest.fixture(autouse=True)
def clear_global_settings_cache():
    """Prevent cross-test contamination from get_cached_global_settings."""
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    yield
    _gp._GLOBAL_SETTINGS_CACHE.clear()


class TestGitLabWikiSettings:
    @pytest.fixture
    def mock_gitlab_client(self):
        return MagicMock()

    @pytest.fixture
    def mock_project(self):
        return MagicMock()

    @pytest.fixture
    def gitlab_provider(self, mock_gitlab_client, mock_project):
        with patch('pr_agent.git_providers.gitlab_provider.gitlab.Gitlab', return_value=mock_gitlab_client), \
             patch('pr_agent.git_providers.gitlab_provider.get_settings') as mock_settings:

            mock_settings.return_value.get.side_effect = lambda key, default=None: {
                "GITLAB.URL": "https://gitlab.com",
                "GITLAB.PERSONAL_ACCESS_TOKEN": "fake_token"
            }.get(key, default)

            # Mock configuration.get for use_wiki_settings_file
            mock_settings.return_value.config.get.side_effect = lambda key, default=None: {
                "use_wiki_settings_file": True
            }.get(key, default)
            # Disable global settings by default (these tests focus on wiki/local)
            mock_settings.return_value.config.use_global_settings_file = False

            mock_gitlab_client.projects.get.return_value = mock_project
            # By default, wiki pages are not found (raise on any wikis.get call)
            mock_project.wikis.get.side_effect = Exception("Wiki page not found")
            provider = GitLabProvider("https://gitlab.com/test/repo/-/merge_requests/1")
            provider.gl = mock_gitlab_client
            provider.id_project = "test/repo"
            return provider

    def test_get_repo_settings_from_main_repo(self, gitlab_provider, mock_project):
        mock_file = MagicMock(ProjectFile)
        mock_file.decode.return_value = 'key = "value_main"'
        mock_project.files.get.return_value = mock_file

        settings = gitlab_provider.get_repo_settings()

        assert settings == [("local", 'key = "value_main"')]
        mock_project.files.get.assert_called_once()

    def test_get_repo_settings_from_wiki(self, gitlab_provider, mock_project):
        # 1. Main repo file not found
        mock_project.files.get.side_effect = Exception("404")

        # 2. Mock wiki page (reset the default side_effect that raises)
        mock_wiki_page = MagicMock(ProjectWiki)
        mock_wiki_page.content = 'key = "value_wiki"'
        mock_project.wikis.get.side_effect = None
        mock_project.wikis.get.return_value = mock_wiki_page

        settings = gitlab_provider.get_repo_settings()

        assert settings == [("wiki", 'key = "value_wiki"')]
        mock_project.wikis.get.assert_any_call('.pr_agent.toml')

    def test_get_repo_settings_from_wiki_with_markdown(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = Exception("404")

        mock_wiki_page = MagicMock(ProjectWiki)
        mock_wiki_page.content = "Some description\n\n```toml\nkey = \"value_wiki_md\"\n```\nMore text"
        mock_project.wikis.get.side_effect = None
        mock_project.wikis.get.return_value = mock_wiki_page

        settings = gitlab_provider.get_repo_settings()

        assert settings == [("wiki", 'key = "value_wiki_md"')]

    def test_get_repo_settings_wiki_disabled(self, gitlab_provider, mock_project):
        mock_project.files.get.side_effect = Exception("404")

        with patch('pr_agent.git_providers.gitlab_provider.get_settings') as mock_settings:
             mock_settings.return_value.config.get.side_effect = lambda key, default=None: {
                 "use_wiki_settings_file": False
             }.get(key, default)
             mock_settings.return_value.config.use_global_settings_file = False

             settings = gitlab_provider.get_repo_settings()
             assert settings == ""
             mock_project.wikis.get.assert_not_called()
