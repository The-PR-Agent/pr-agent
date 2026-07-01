from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider


class FakeContent:
    def __init__(self, decoded_content):
        self.decoded_content = decoded_content


class FakeRepo:
    def __init__(self, files=None):
        self.files = files or {}

    def get_contents(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return FakeContent(self.files[path])


class FakeGithubClient:
    def __init__(self, repos=None):
        self.repos = repos or {}

    def get_repo(self, repo_name):
        if repo_name not in self.repos:
            raise FileNotFoundError(repo_name)
        return self.repos[repo_name]


def _provider(local_settings=None, global_settings=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "org/service"
    provider.repo_obj = FakeRepo({".pr_agent.toml": local_settings} if local_settings is not None else {})
    provider.github_client = FakeGithubClient(
        {"org/pr-agent-settings": FakeRepo({".pr_agent.toml": global_settings})}
        if global_settings is not None
        else {}
    )
    return provider


def test_get_repo_settings_returns_global_settings_when_local_settings_missing():
    provider = _provider(global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n")

    settings = provider.get_repo_settings()

    assert settings == [("global", b"[pr_reviewer]\nextra_instructions = \"global\"\n")]


def test_get_repo_settings_merges_global_before_local_settings():
    provider = _provider(
        global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n",
        local_settings=b"[pr_description]\npublish_labels = false\n",
    )

    settings = provider.get_repo_settings()

    assert settings == [
        ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\n"),
        ("local", b"[pr_description]\npublish_labels = false\n"),
    ]


def test_get_repo_settings_keeps_global_and_local_same_section_separate():
    provider = _provider(
        global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\nnum_code_suggestions = 3\n",
        local_settings=b"[pr_reviewer]\nextra_instructions = \"local\"\n",
    )

    settings = provider.get_repo_settings()

    assert settings == [
        ("global", b"[pr_reviewer]\nextra_instructions = \"global\"\nnum_code_suggestions = 3\n"),
        ("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n"),
    ]


def test_get_repo_settings_skips_global_settings_when_disabled():
    settings = get_settings()
    original = settings.config.use_global_settings_file
    settings.config.use_global_settings_file = False
    try:
        provider = _provider(
            global_settings=b"[pr_reviewer]\nextra_instructions = \"global\"\n",
            local_settings=b"[pr_reviewer]\nextra_instructions = \"local\"\n",
        )

        repo_settings = provider.get_repo_settings()

        assert repo_settings == [("local", b"[pr_reviewer]\nextra_instructions = \"local\"\n")]
    finally:
        settings.config.use_global_settings_file = original
