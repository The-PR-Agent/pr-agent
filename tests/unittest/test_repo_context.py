from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from github import GithubException
from gitlab.exceptions import GitlabGetError
from jinja2 import Environment, StrictUndefined, select_autoescape

from pr_agent.algo import repo_context
from pr_agent.algo.prompt_fragments import render_diff_hunk_format
from pr_agent.algo.repo_context import (
    TRUNCATION_MARKER,
    build_repo_context,
    render_instruction_files,
    render_instruction_files_with_line_budget,
)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import (
    AzureDevopsProvider,
    BitbucketProvider,
    BitbucketServerProvider,
    GiteaProvider,
    GitLabProvider,
)
from pr_agent.git_providers.codecommit_provider import CodeCommitProvider
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


class FakeProvider:
    def __init__(self, files, pr_url=None):
        self.files = files
        self.pr_url = pr_url
        self.requested_paths = []
        self.from_default_branch_calls = []

    def get_repo_file_content(self, file_path: str, from_default_branch: bool = False):
        self.requested_paths.append(file_path)
        self.from_default_branch_calls.append(from_default_branch)
        return self.files.get(file_path)

    def get_repo_context_ref(self, from_default_branch: bool = False):
        # Simulate a provider that keys its content on a revision: the default branch uses a
        # stable name, and the target/base branch uses a commit-derived value.
        return "default" if from_default_branch else "target-sha"


class UnsupportedProvider:
    get_repo_file_content = GitProvider.get_repo_file_content


class SiblingFakeProvider(FakeProvider):
    """FakeProvider that additionally resolves sibling-repository files."""

    def __init__(self, files, sibling_files=None, pr_url=None):
        super().__init__(files, pr_url)
        self.sibling_files = sibling_files or {}
        self.requested_siblings = []

    def get_sibling_repo_file_content(self, repo_id: str, file_path: str, from_default_branch: bool = False):
        self.requested_siblings.append(f"{repo_id}:{file_path}")
        return self.sibling_files.get(f"{repo_id}:{file_path}")


@pytest.fixture
def repo_context_settings():
    settings = get_settings()
    original_files = settings.config.get("repo_context_files", [])
    original_max_lines = settings.config.get("repo_context_max_lines", 500)
    original_from_default_branch = settings.config.get("repo_context_from_default_branch", True)
    original_max_sibling_files = settings.config.get("repo_context_max_sibling_files", 5)
    original_warned_provider_classes = repo_context._unsupported_repo_context_provider_classes.copy()
    original_process_cache = repo_context._repo_context_process_cache.copy()

    yield settings

    settings.set("CONFIG.REPO_CONTEXT_FILES", original_files)
    settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)
    settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", original_from_default_branch)
    settings.set("CONFIG.REPO_CONTEXT_MAX_SIBLING_FILES", original_max_sibling_files)
    repo_context._unsupported_repo_context_provider_classes = original_warned_provider_classes
    repo_context._repo_context_process_cache = original_process_cache


def test_default_config_ships_agents_md_as_repo_context():
    import tomllib
    from pathlib import Path

    import pr_agent

    config_path = Path(pr_agent.__file__).parent / "settings" / "configuration.toml"
    with open(config_path, "rb") as config_file:
        config = tomllib.load(config_file)

    assert config["config"]["repo_context_files"] == ["AGENTS.md"]
    # Reading from the default branch is the secure default.
    assert config["config"]["repo_context_from_default_branch"] is True


def test_build_repo_context_reads_from_default_branch_by_default(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", True)
    provider = FakeProvider({"AGENTS.md": "Repo purpose"})

    build_repo_context(provider)

    assert provider.from_default_branch_calls == [True]


def test_build_repo_context_reads_from_target_branch_when_disabled(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", False)
    provider = FakeProvider({"AGENTS.md": "Repo purpose"})

    build_repo_context(provider)

    assert provider.from_default_branch_calls == [False]


@pytest.mark.parametrize(
    "config_value,expected",
    [
        ("false", False),
        ("False", False),
        ("0", False),
        ("true", True),
        ("1", True),
        ("maybe", True),  # unparseable -> secure default
    ],
)
def test_build_repo_context_parses_string_default_branch_flag(repo_context_settings, config_value, expected):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", config_value)
    provider = FakeProvider({"AGENTS.md": "Repo purpose"})

    build_repo_context(provider)

    assert provider.from_default_branch_calls == [expected]


def test_build_repo_context_fetches_and_formats_configured_files(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "# Agent Guide\nUse focused tests.",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    context = build_repo_context(provider)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "# Agent Guide\n"
        "Use focused tests.\n"
        "`````\n"
        "</file>\n\n"
        '<file path="CONTRIBUTING.md" scope="repo-root">\n'
        "`````markdown\n"
        "Keep PRs small.\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_reuses_provider_cache_for_same_config(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    first_context = build_repo_context(provider)
    second_context = build_repo_context(provider)

    assert second_context == first_context
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_provider_cache_separates_default_and_target_branch(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({"AGENTS.md": "default-branch rules"})

    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", True)
    default_context = build_repo_context(provider)

    provider.files["AGENTS.md"] = "target-branch rules"
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", False)
    target_context = build_repo_context(provider)

    assert "default-branch rules" in default_context
    assert "target-branch rules" in target_context
    assert provider.from_default_branch_calls == [True, False]


def test_build_repo_context_reuses_process_cache_for_same_pr_url(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({"AGENTS.md": "Changed repo purpose"}, pr_url="https://example.com/org/repo/pull/1")

    first_context = build_repo_context(first_provider)
    second_context = build_repo_context(second_provider)

    assert second_context == first_context
    assert "Repo purpose" in second_context
    assert "Changed repo purpose" not in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == []


def test_build_repo_context_process_cache_separates_default_and_target_branch(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    pr_url = "https://example.com/org/repo/pull/1"
    default_provider = FakeProvider({"AGENTS.md": "default-branch rules"}, pr_url=pr_url)
    target_provider = FakeProvider({"AGENTS.md": "target-branch rules"}, pr_url=pr_url)

    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", True)
    default_context = build_repo_context(default_provider)

    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", False)
    target_context = build_repo_context(target_provider)

    assert "default-branch rules" in default_context
    assert "target-branch rules" in target_context
    assert default_provider.from_default_branch_calls == [True]
    assert target_provider.from_default_branch_calls == [False]


def test_build_repo_context_refreshes_process_cache_after_ttl(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({"AGENTS.md": "Changed repo purpose"}, pr_url="https://example.com/org/repo/pull/1")

    with patch("pr_agent.algo.repo_context.time.monotonic", side_effect=[100, 100, 2000, 2000, 2000, 2000]):
        first_context = build_repo_context(first_provider)
        second_context = build_repo_context(second_provider)

    assert "Repo purpose" in first_context
    assert "Changed repo purpose" in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == ["AGENTS.md"]


def test_build_repo_context_refreshes_empty_process_cache_after_ttl(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({}, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")

    with patch("pr_agent.algo.repo_context.time.monotonic", side_effect=[100, 100, 2000, 2000, 2000, 2000]):
        first_context = build_repo_context(first_provider)
        second_context = build_repo_context(second_provider)

    assert first_context == ""
    assert "Repo purpose" in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == ["AGENTS.md"]


def test_repo_context_cache_evicts_oldest_entry_when_full():
    cache = repo_context._RepoContextCache(max_size=2, ttl_seconds=900)
    missing = object()

    with patch("pr_agent.algo.repo_context.time.monotonic", return_value=100):
        cache["first"] = "one"
        cache["second"] = "two"
        cache["third"] = "three"

        assert cache.get("first", missing) is missing
        assert cache.get("second", missing) == "two"
        assert cache.get("third", missing) == "three"


def test_get_repo_context_config_normalizes_inputs(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", "AGENTS.md")
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", "12")

    assert repo_context._get_repo_context_config() == (["AGENTS.md"], 12)


def test_get_repo_context_config_rejects_non_list_container(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", {"AGENTS.md": True})

    assert repo_context._get_repo_context_config() is None


def test_provider_supports_repo_context_warns_once_for_unsupported_provider(repo_context_settings):
    provider = UnsupportedProvider()

    with patch("pr_agent.algo.repo_context.get_logger") as mock_get_logger:
        assert repo_context._provider_supports_repo_context(provider) is False
        assert repo_context._provider_supports_repo_context(provider) is False

    mock_get_logger.return_value.warning.assert_called_once_with(
        "repo_context_files is configured, but UnsupportedProvider does not support repository file fetching; "
        "skipping repo context"
    )


def test_load_repo_context_files_normalizes_fetch_results():
    provider = FakeProvider({
        "AGENTS.md": b"Repo purpose",
        "EMPTY.md": "",
        "MISSING.md": None,
    })

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider, ["AGENTS.md", "EMPTY.md", "MISSING.md", " "]
    )

    assert files == {"AGENTS.md": "Repo purpose"}
    assert had_fetch_error is False
    assert provider.requested_paths == ["AGENTS.md", "EMPTY.md", "MISSING.md"]


def test_load_repo_context_files_reports_fetch_errors():
    provider = FakeProvider({})
    provider.get_repo_file_content = Mock(side_effect=Exception("temporary outage"))

    files, had_fetch_error = repo_context._load_repo_context_files(provider, ["AGENTS.md"])

    assert files == {}
    assert had_fetch_error is True


@pytest.mark.parametrize(
    "entry,expected_repo_id,expected_path",
    [
        ("AGENTS.md", None, "AGENTS.md"),
        ("docs/guide.md", None, "docs/guide.md"),
        ("  src/ x.py  ", None, "src/ x.py"),
        ("group/sub/lib:src/api.py", "group/sub/lib", "src/api.py"),
        ("owner/lib:/deep/file.py", "owner/lib", "deep/file.py"),
        ("owner/lib:file.py", "owner/lib", "file.py"),
        ("  owner/lib  :  file.py  ", "owner/lib", "file.py"),
    ],
)
def test_parse_repo_context_file_entry(entry, expected_repo_id, expected_path):
    assert repo_context._parse_repo_context_file_entry(entry) == (expected_repo_id, expected_path)


@pytest.mark.parametrize("entry", [":path.py", "repo/only:"])
def test_parse_repo_context_file_entry_rejects_malformed(entry):
    with patch("pr_agent.algo.repo_context.get_logger") as mock_get_logger:
        repo_id, file_path = repo_context._parse_repo_context_file_entry(entry)
    assert (repo_id, file_path) == (None, "")
    assert mock_get_logger.return_value.warning.called


def test_parse_repo_context_file_entry_treats_blank_as_plain_path():
    assert repo_context._parse_repo_context_file_entry("   ") == (None, "")
    assert repo_context._parse_repo_context_file_entry("") == (None, "")


def test_load_repo_context_files_fetches_sibling_and_same_repo_files():
    provider = SiblingFakeProvider(
        files={"AGENTS.md": "Repo purpose"},
        sibling_files={"group/lib-api:src/interfaces/api.py": "Sibling contract"},
    )

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider,
        ["AGENTS.md", "group/lib-api:src/interfaces/api.py"],
        from_default_branch=True,
    )

    # The sibling file is rendered under its sibling path so the model sees where it came from.
    assert files == {
        "AGENTS.md": "Repo purpose",
        "group/lib-api/src/interfaces/api.py": "Sibling contract",
    }
    assert had_fetch_error is False
    assert provider.requested_paths == ["AGENTS.md"]
    assert provider.requested_siblings == ["group/lib-api:src/interfaces/api.py"]


def test_load_repo_context_files_reports_sibling_fetch_errors():
    provider = SiblingFakeProvider(files={})
    provider.get_sibling_repo_file_content = Mock(side_effect=Exception("temporary outage"))

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider, ["group/lib-api:src/api.py"], from_default_branch=True
    )

    assert files == {}
    assert had_fetch_error is True


def test_load_repo_context_files_skips_siblings_for_unsupported_provider():
    provider = FakeProvider({"AGENTS.md": "Repo purpose"})
    provider.get_sibling_repo_file_content = Mock()

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider, ["AGENTS.md", "group/lib-api:src/api.py"], from_default_branch=True
    )

    assert files == {"AGENTS.md": "Repo purpose"}
    assert had_fetch_error is False
    assert provider.requested_paths == ["AGENTS.md"]
    provider.get_sibling_repo_file_content.assert_not_called()


def test_load_repo_context_files_respects_sibling_file_cap(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_SIBLING_FILES", 2)
    provider = SiblingFakeProvider(
        files={},
        sibling_files={
            "group/g1:README.md": "one",
            "group/g2:README.md": "two",
            "group/g3:README.md": "three",
        },
    )

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider,
        [
            "group/g1:README.md",
            "group/g2:README.md",
            "group/g3:README.md",
            "group/g4:README.md",
        ],
        from_default_branch=True,
    )

    assert files == {
        "group/g1/README.md": "one",
        "group/g2/README.md": "two",
    }
    assert had_fetch_error is False
    assert provider.requested_siblings == [
        "group/g1:README.md",
        "group/g2:README.md",
    ]


def test_sibling_fetch_cap_counts_attempts_not_just_content(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_SIBLING_FILES", 2)
    provider = SiblingFakeProvider(
        files={},
        sibling_files={
            "group/g1:README.md": "",
            "group/g2:README.md": "two",
        },
    )

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider,
        [
            "group/g1:README.md",
            "group/g2:README.md",
            "group/g3:README.md",
        ],
        from_default_branch=True,
    )

    assert files == {"group/g2/README.md": "two"}
    assert provider.requested_siblings == [
        "group/g1:README.md",
        "group/g2:README.md",
    ]


def test_sibling_fetch_cap_counts_exceptions(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_SIBLING_FILES", 2)
    provider = SiblingFakeProvider(files={})
    call_count = 0
    original = provider.get_sibling_repo_file_content

    def counting_call(repo_id, file_path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("transient outage")
        return original(repo_id, file_path)

    provider.get_sibling_repo_file_content = Mock(side_effect=counting_call)
    provider.requested_siblings = []

    files, had_fetch_error = repo_context._load_repo_context_files(
        provider,
        [
            "group/g1:README.md",
            "group/g2:README.md",
            "group/g3:README.md",
        ],
        from_default_branch=True,
    )

    assert call_count == 2
    assert had_fetch_error is True


def test_build_repo_context_renders_sibling_file_with_budget(repo_context_settings):
    repo_context_settings.set(
        "CONFIG.REPO_CONTEXT_FILES", ["group/lib-api:src/interfaces/api.py"]
    )
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = SiblingFakeProvider(
        files={},
        sibling_files={"group/lib-api:src/interfaces/api.py": "def call(req): ...\n"},
    )

    context = build_repo_context(provider)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="group/lib-api/src/interfaces/api.py" scope="group/lib-api/src/interfaces">\n'
        "`````markdown\n"
        "def call(req): ...\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_github_provider_fetches_sibling_file_in_same_owner():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    sibling_repo = Mock()
    sibling_repo.private = False
    sibling_repo.get_contents.return_value.decoded_content = b"sibling contract"
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == "sibling contract"
    provider.github_client.get_repo.assert_called_once_with("myorg/lib")
    sibling_repo.get_contents.assert_called_once_with("src/api.py")


def test_github_provider_fetches_private_sibling_when_requester_is_collaborator():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    sibling_repo = Mock()
    sibling_repo.private = True
    sibling_repo.has_in_collaborators.return_value = True
    sibling_repo.get_contents.return_value.decoded_content = b"sibling contract"
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == "sibling contract"
    sibling_repo.has_in_collaborators.assert_called_once_with("alice")


def test_github_provider_fetches_private_sibling_when_requester_is_owner():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    sibling_repo = Mock()
    sibling_repo.private = True
    sibling_repo.owner = SimpleNamespace(login="alice")
    sibling_repo.get_contents.return_value.decoded_content = b"sibling contract"
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == "sibling contract"
    sibling_repo.has_in_collaborators.assert_not_called()


def test_github_provider_rejects_private_sibling_when_requester_is_not_collaborator():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    sibling_repo = Mock()
    sibling_repo.private = True
    sibling_repo.has_in_collaborators.return_value = False
    provider.github_client.get_repo.return_value = sibling_repo

    with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == ""

    sibling_repo.get_contents.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring sibling repo context file the review requester cannot read: myorg/lib"
    )


def test_github_provider_rejects_private_sibling_when_requester_is_unknown():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    sibling_repo = Mock()
    sibling_repo.private = True
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == ""
    sibling_repo.get_contents.assert_not_called()


def test_github_provider_rejects_private_sibling_when_command_actor_lacks_access():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    provider.set_command_actor("mallory")
    sibling_repo = Mock()
    sibling_repo.private = True
    sibling_repo.visibility = "private"
    sibling_repo.has_in_collaborators.return_value = False
    provider.github_client.get_repo.return_value = sibling_repo

    with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == ""

    # The commenter (command actor), not the PR author, is the identity that must be verified.
    sibling_repo.has_in_collaborators.assert_called_once_with("mallory")
    sibling_repo.get_contents.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring sibling repo context file the review requester cannot read: myorg/lib"
    )


def test_github_provider_fetches_private_sibling_when_command_actor_is_collaborator():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    provider.set_command_actor("mallory")
    sibling_repo = Mock()
    sibling_repo.private = True
    sibling_repo.visibility = "private"
    sibling_repo.has_in_collaborators.return_value = True
    sibling_repo.get_contents.return_value.decoded_content = b"sibling contract"
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == "sibling contract"
    sibling_repo.has_in_collaborators.assert_called_once_with("mallory")


def test_github_provider_rejects_internal_sibling_when_requester_is_not_collaborator():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.pr = SimpleNamespace(user=SimpleNamespace(login="alice"))
    sibling_repo = Mock()
    sibling_repo.private = False
    sibling_repo.visibility = "internal"
    sibling_repo.has_in_collaborators.return_value = False
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == ""
    sibling_repo.has_in_collaborators.assert_called_once_with("alice")
    sibling_repo.get_contents.assert_not_called()


def test_github_provider_fetches_internal_sibling_when_command_actor_is_collaborator():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.set_command_actor("alice")
    sibling_repo = Mock()
    sibling_repo.private = False
    sibling_repo.visibility = "internal"
    sibling_repo.has_in_collaborators.return_value = True
    sibling_repo.get_contents.return_value.decoded_content = b"sibling contract"
    provider.github_client.get_repo.return_value = sibling_repo

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == "sibling contract"
    sibling_repo.has_in_collaborators.assert_called_once_with("alice")


def test_github_provider_rejects_out_of_owner_sibling_without_api_call():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()

    with patch("pr_agent.git_providers.github_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("other-org/lib", "src/api.py") == ""

    provider.github_client.get_repo.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring out-of-owner sibling repo in repo context: other-org/lib"
    )


def test_github_provider_treats_missing_sibling_file_as_no_context():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "myorg/current"
    provider.github_client = Mock()
    provider.github_client.get_repo.side_effect = GithubException(404, {"message": "Not Found"}, {})

    assert provider.get_sibling_repo_file_content("myorg/lib", "src/api.py") == ""


def test_gitlab_provider_fetches_sibling_file_in_same_namespace():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "public"
    sibling_project.files.get.return_value.decode.return_value = b"sibling contract"
    provider.gl.projects.get.return_value = sibling_project

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == "sibling contract"
    provider.gl.projects.get.assert_called_once_with("group/sub/lib")
    sibling_project.files.get.assert_called_once_with(file_path="src/api.py", ref="main")


def test_gitlab_provider_fetches_internal_sibling_without_membership_check():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "internal"
    sibling_project.files.get.return_value.decode.return_value = b"sibling contract"
    provider.gl.projects.get.return_value = sibling_project

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == "sibling contract"
    sibling_project.members_all.get.assert_not_called()


def test_gitlab_provider_fetches_private_sibling_when_requester_is_member():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    provider.mr = SimpleNamespace(author={"id": 42})
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "private"
    sibling_project.files.get.return_value.decode.return_value = b"sibling contract"
    provider.gl.projects.get.return_value = sibling_project

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == "sibling contract"
    sibling_project.members_all.get.assert_called_once_with(42)


def test_gitlab_provider_rejects_private_sibling_when_command_actor_is_not_member():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    provider.mr = SimpleNamespace(author={"id": 42})
    provider.set_command_actor(99)
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "private"
    sibling_project.members_all.get.side_effect = GitlabGetError("Not found", response_code=404)
    provider.gl.projects.get.return_value = sibling_project

    with patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == ""

    # The commenter (command actor), not the MR author, is the identity that must be verified.
    sibling_project.members_all.get.assert_called_once_with(99)
    sibling_project.files.get.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring sibling repo context file the review requester cannot read: group/sub/lib"
    )


def test_gitlab_provider_fetches_private_sibling_when_command_actor_is_member():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    provider.mr = SimpleNamespace(author={"id": 42})
    provider.set_command_actor(99)
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "private"
    sibling_project.files.get.return_value.decode.return_value = b"sibling contract"
    provider.gl.projects.get.return_value = sibling_project

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == "sibling contract"
    sibling_project.members_all.get.assert_called_once_with(99)


def test_gitlab_provider_rejects_private_sibling_when_requester_is_not_member():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    provider.mr = SimpleNamespace(author={"id": 42})
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "private"
    sibling_project.members_all.get.side_effect = GitlabGetError("Not found", response_code=404)
    provider.gl.projects.get.return_value = sibling_project

    with patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == ""

    sibling_project.files.get.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring sibling repo context file the review requester cannot read: group/sub/lib"
    )


def test_gitlab_provider_rejects_private_sibling_when_requester_is_unknown():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "private"
    provider.gl.projects.get.return_value = sibling_project

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == ""
    sibling_project.files.get.assert_not_called()
    sibling_project.members_all.get.assert_not_called()


def test_gitlab_provider_rejects_out_of_namespace_sibling_without_api_call():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/sub/current"
    provider.gl = Mock()

    with patch("pr_agent.git_providers.gitlab_provider.get_logger") as mock_get_logger:
        assert provider.get_sibling_repo_file_content("other/lib", "src/api.py") == ""

    provider.gl.projects.get.assert_not_called()
    mock_get_logger.return_value.warning.assert_called_once_with(
        "Ignoring out-of-namespace sibling repo in repo context: other/lib"
    )


def test_gitlab_provider_resolves_namespace_for_numeric_project_id():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "123"
    provider.gl = Mock()
    # python-gitlab exposes the project namespace as a mapping rather than an object.
    current_project = Mock()
    current_project.namespace = {"full_path": "group/sub"}
    provider.gl.projects.get.return_value = current_project
    sibling_project = Mock()
    sibling_project.default_branch = "main"
    sibling_project.visibility = "public"
    sibling_project.files.get.return_value.decode.return_value = b"sibling contract"
    provider.gl.projects.get.side_effect = [current_project, sibling_project]

    assert provider.get_sibling_repo_file_content("group/sub/lib", "src/api.py") == "sibling contract"

    # Current project (id 123) resolved once for its namespace, then the sibling fetched.
    assert provider.gl.projects.get.call_args_list[0] == (("123",),)
    assert provider.gl.projects.get.call_args_list[1] == (("group/sub/lib",),)


def test_gitlab_provider_rejects_sibling_when_namespace_lookup_fails():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "123"
    provider.gl = Mock()
    current_project = Mock()
    current_project.namespace = {"full_path": "group/sub"}
    provider.gl.projects.get.side_effect = [GitlabGetError("boom", response_code=500), current_project]

    # A failed namespace lookup must not let a one-component/numeric sibling id pass validation.
    assert provider.get_sibling_repo_file_content("456", "file.md") == ""

    # Only the namespace lookup was attempted; the numeric sibling was never fetched.
    assert provider.gl.projects.get.call_args_list == [(("123",),)]


def test_gitlab_provider_extracts_namespace_full_path_from_object_and_mapping():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "123"
    assert provider._extract_repo_context_namespace_full_path({"full_path": "group/sub"}) == "group/sub"
    namespace_object = SimpleNamespace(full_path="group/sub")
    assert provider._extract_repo_context_namespace_full_path(namespace_object) == "group/sub"
    assert provider._extract_repo_context_namespace_full_path({"path": "sub"}) == ""
    assert provider._extract_repo_context_namespace_full_path(None) == ""


def test_build_repo_context_process_cache_invalidates_when_config_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    first_provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    }, pr_url="https://example.com/org/repo/pull/1")
    second_provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    }, pr_url="https://example.com/org/repo/pull/1")

    first_context = build_repo_context(first_provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["CONTRIBUTING.md"])
    second_context = build_repo_context(second_provider)

    assert "Repo purpose" in first_context
    assert "Keep PRs small." in second_context
    assert first_provider.requested_paths == ["AGENTS.md"]
    assert second_provider.requested_paths == ["CONTRIBUTING.md"]


def test_build_repo_context_does_not_cache_empty_context_after_fetch_error(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({"AGENTS.md": "Repo purpose"}, pr_url="https://example.com/org/repo/pull/1")
    provider.get_repo_file_content = Mock(side_effect=[Exception("temporary outage"), "Repo purpose"])

    first_context = build_repo_context(provider)
    second_context = build_repo_context(provider)

    assert first_context == ""
    assert "Repo purpose" in second_context


def test_build_repo_context_does_not_cache_sibling_content(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["group/lib:api.py"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = SiblingFakeProvider(
        files={},
        sibling_files={"group/lib:api.py": "def call(req): ...\n"},
        pr_url="https://example.com/org/repo/pull/1",
    )

    first_context = build_repo_context(provider)
    assert "<file path=\"group/lib/api.py\"" in first_context
    assert "def call(req): ..." in first_context

    # Sibling default branches change independently of the requesting repo; the cache must not
    # serve the previous revision.
    provider.sibling_files["group/lib:api.py"] = "def call(req, body): ...\n"
    second_context = build_repo_context(provider)

    assert "def call(req, body): ..." in second_context
    assert "def call(req): ..." not in second_context
    assert provider.requested_siblings == ["group/lib:api.py", "group/lib:api.py"]


def test_build_repo_context_cache_invalidates_when_repo_context_files_change(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = FakeProvider({
        "AGENTS.md": "Repo purpose",
        "CONTRIBUTING.md": "Keep PRs small.",
    })

    first_context = build_repo_context(provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["CONTRIBUTING.md"])
    second_context = build_repo_context(provider)

    assert "Repo purpose" in first_context
    assert "Keep PRs small." in second_context
    assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]


def test_build_repo_context_cache_invalidates_when_line_budget_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 9)
    provider = FakeProvider({"AGENTS.md": "one\ntwo\nthree"})

    truncated_context = build_repo_context(provider)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    full_context = build_repo_context(provider)

    assert TRUNCATION_MARKER in truncated_context
    assert "one\ntwo\nthree" in full_context
    assert provider.requested_paths == ["AGENTS.md", "AGENTS.md"]


def test_render_instruction_files_escapes_path_and_derives_scope():
    context = render_instruction_files({
        'docs/Agent "Notes".md': "Use <literal> markers.\n",
    })

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="docs/Agent &quot;Notes&quot;.md" scope="docs">\n'
        "`````markdown\n"
        "Use <literal> markers.\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_render_instruction_files_uses_longer_fence_when_content_contains_default_fence():
    context = render_instruction_files({
        "AGENTS.md": "Avoid closing this fence:\n`````",
    })

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "``````markdown\n"
        "Avoid closing this fence:\n"
        "`````\n"
        "``````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_render_instruction_files_with_line_budget_uses_longer_fence_for_conflicting_content():
    context = render_instruction_files_with_line_budget({
        "AGENTS.md": "Avoid closing this fence:\n`````",
    }, max_lines=500)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "``````markdown\n"
        "Avoid closing this fence:\n"
        "`````\n"
        "``````\n"
        "</file>\n\n"
        "</instruction_files>"
    )


def test_build_repo_context_skips_invalid_missing_and_empty_files(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["", 7, "MISSING.md", "EMPTY.md", "AGENTS.md"])
    provider = FakeProvider({"EMPTY.md": "", "AGENTS.md": "Loaded context"})

    assert build_repo_context(provider) == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "Loaded context\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["MISSING.md", "EMPTY.md", "AGENTS.md"]


def test_build_repo_context_enforces_total_line_cap(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 4)
    provider = FakeProvider({
        "AGENTS.md": "one\ntwo\nthree",
        "CONTRIBUTING.md": "four\nfive",
    })

    context = build_repo_context(provider)

    assert context == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        "</instruction_files>"
    )
    assert len(context.splitlines()) <= 4


def test_render_instruction_files_with_line_budget_returns_empty_when_wrapper_exceeds_budget():
    context = render_instruction_files_with_line_budget({
        "AGENTS.md": "one",
    }, max_lines=2)

    assert context == ""


@pytest.mark.parametrize("max_lines", range(0, 12))
def test_render_instruction_files_with_line_budget_never_exceeds_configured_budget(max_lines):
    context = render_instruction_files_with_line_budget({
        "AGENTS.md": "one\ntwo\nthree",
        "CONTRIBUTING.md": "four\nfive",
    }, max_lines=max_lines)

    assert len(context.splitlines()) <= max_lines


def test_build_repo_context_returns_empty_when_no_files_configured(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", [])

    assert build_repo_context(FakeProvider({"AGENTS.md": "repo purpose"})) == ""


def test_build_repo_context_treats_string_config_as_single_file(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", "AGENTS.md")
    provider = FakeProvider({"AGENTS.md": "repo purpose"})

    assert build_repo_context(provider) == (
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.\n"
        "<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        "`````markdown\n"
        "repo purpose\n"
        "`````\n"
        "</file>\n\n"
        "</instruction_files>"
    )
    assert provider.requested_paths == ["AGENTS.md"]


def test_build_repo_context_skips_non_list_container(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", {"AGENTS.md": True})
    provider = FakeProvider({"AGENTS.md": "repo purpose"})

    assert build_repo_context(provider) == ""
    assert provider.requested_paths == []


def test_build_repo_context_warns_once_for_provider_without_repo_file_fetching(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    provider = UnsupportedProvider()

    with patch("pr_agent.algo.repo_context.get_logger") as mock_get_logger:
        context = build_repo_context(provider)
        second_context = build_repo_context(provider)

    assert context == ""
    assert second_context == ""
    mock_get_logger.return_value.warning.assert_called_once_with(
        "repo_context_files is configured, but UnsupportedProvider does not support repository file fetching; "
        "skipping repo context"
    )


def test_github_provider_decodes_repo_context_files_and_treats_404_as_missing():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"

    # A genuine 404 (missing file) is treated as "no context".
    provider.repo_obj.get_contents.side_effect = GithubException(404, {"message": "Not Found"}, {})

    assert provider.get_repo_file_content("MISSING.md") == ""


def test_github_provider_propagates_transient_fetch_errors():
    # Transient/unexpected errors must propagate (not be swallowed as "missing"), so the
    # repo-context loader flags a fetch error and does not cache an empty result.
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.side_effect = GithubException(500, {"message": "Server Error"}, {})

    with pytest.raises(GithubException):
        provider.get_repo_file_content("AGENTS.md")


def test_github_provider_reads_repo_context_files_from_pr_base_ref():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"
    provider.pr = Mock(base=Mock(sha="base-sha", ref="release/1.0"))

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"
    provider.repo_obj.get_contents.assert_called_once_with("AGENTS.md", ref="base-sha")


def test_github_provider_falls_back_to_default_branch_without_pr_base():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"
    provider.pr = None

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"
    provider.repo_obj.get_contents.assert_called_once_with("AGENTS.md")


def test_github_provider_reads_from_default_branch_when_requested():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"
    # Even with a PR base ref available, from_default_branch must ignore it.
    provider.pr = Mock(base=Mock(sha="base-sha", ref="release/1.0"))

    assert provider.get_repo_file_content("AGENTS.md", from_default_branch=True) == "repo context"
    provider.repo_obj.get_contents.assert_called_once_with("AGENTS.md")  # no ref -> default branch


@pytest.mark.parametrize(
    "prompt_name,variables",
    [
        (
            "pr_review_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "skills_context": "",
                "require_can_be_split_review": False,
                "related_tickets": "",
                "require_estimate_contribution_time_cost": False,
                "require_score": False,
                "require_tests": True,
                "question_str": "",
                "require_security_review": True,
                "require_todo_scan": False,
                "require_estimate_effort_to_review": True,
                "require_risk_assessment": False,
                "require_merge_recommendation": False,
                "require_priority_files": False,
                "num_max_findings": 3,
                "num_pr_files": 1,
                "is_ai_metadata": False,
            },
        ),
        (
            "pr_description_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "skills_context": "",
                "enable_custom_labels": False,
                "custom_labels_class": "",
                "enable_semantic_files_types": True,
                "include_file_summary_changes": True,
                "enable_pr_diagram": False,
                "enable_pr_description": True,
            },
        ),
        (
            "pr_code_suggestions_prompt",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "skills_context": "",
                "focus_only_on_problems": True,
                "num_code_suggestions": 3,
                "is_ai_metadata": False,
            },
        ),
        (
            "pr_code_suggestions_prompt_not_decoupled",
            {
                "extra_instructions": "",
                "repo_context": render_instruction_files({"AGENTS.md": "Repo purpose"}),
                "skills_context": "",
                "focus_only_on_problems": True,
                "num_code_suggestions": 3,
                "is_ai_metadata": False,
            },
        ),
    ],
)
def test_prompt_templates_render_configured_repo_context(prompt_name, variables):
    template = getattr(get_settings(), prompt_name).system

    if prompt_name == "pr_review_prompt":
        variables["diff_hunk_format"] = render_diff_hunk_format(
            include_line_numbers=True,
            include_ai_metadata=False,
        )
    elif prompt_name == "pr_code_suggestions_prompt":
        variables["diff_hunk_format"] = render_diff_hunk_format(
            include_line_numbers=False,
            include_ai_metadata=False,
        )

    # select_autoescape() leaves string templates unescaped (matching production prompt rendering)
    # while avoiding the hard-coded autoescape=False that static analysis flags.
    environment = Environment(autoescape=select_autoescape(default_for_string=False), undefined=StrictUndefined)
    rendered = environment.from_string(template).render(variables)

    assert "Repository context:" in rendered
    assert '<file path="AGENTS.md" scope="repo-root">' in rendered


class RefishProvider(FakeProvider):
    """A provider whose repo-context revision can move between calls, like a rebased base branch."""

    def __init__(self, files, pr_url=None):
        super().__init__(files, pr_url)
        self.context_ref = "sha-1"

    def get_repo_context_ref(self, from_default_branch: bool = False):
        return self.context_ref


def test_build_repo_context_process_cache_refreshes_when_revision_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    pr_url = "https://example.com/org/repo/pull/1"
    provider = RefishProvider({"AGENTS.md": "before rebase"}, pr_url=pr_url)

    first_context = build_repo_context(provider)
    assert "before rebase" in first_context

    # The base branch moved (rebase/push within the TTL): the revision the cache is keyed on
    # changes, so the stale entry must not be served.
    provider.context_ref = "sha-2"
    provider.files["AGENTS.md"] = "after rebase"

    second_context = build_repo_context(provider)

    assert "after rebase" in second_context
    assert "before rebase" not in second_context
    assert provider.requested_paths == ["AGENTS.md", "AGENTS.md"]


def test_build_repo_context_provider_cache_refreshes_when_revision_changes(repo_context_settings):
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    provider = RefishProvider({"AGENTS.md": "before rebase"})

    first_context = build_repo_context(provider)
    assert "before rebase" in first_context

    provider.context_ref = "sha-3"
    provider.files["AGENTS.md"] = "after push"

    second_context = build_repo_context(provider)

    assert "after push" in second_context
    assert provider.requested_paths == ["AGENTS.md", "AGENTS.md"]


def test_get_repo_context_ref_github_returns_base_sha():
    provider = GithubProvider.__new__(GithubProvider)
    provider.base_url = "https://api.github.com"
    provider.provider_id = None
    provider.pr = Mock(base=Mock(sha="base-sha", ref="release/1.0"))

    assert provider.get_repo_context_ref() == "base-sha"


def test_get_repo_context_ref_github_resolves_default_branch_head():
    """Reading the default branch keys the cache on its head commit, so a push to it
    invalidates cached content within the TTL instead of serving a moved commit."""
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.default_branch = "main"
    provider.repo_obj.get_branch.return_value.commit.sha = "default-sha"

    assert provider.get_repo_context_ref(from_default_branch=True) == "default-sha"
    provider.repo_obj.get_branch.assert_called_once_with("main")


def test_get_repo_context_ref_github_without_pr_base_reads_default_branch_head():
    """Without a PR base the fallback read also comes from the default branch, so the
    same key rule applies."""
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = None
    provider.repo_obj = Mock()
    provider.repo_obj.default_branch = "main"
    provider.repo_obj.get_branch.return_value.commit.sha = "default-sha"

    assert provider.get_repo_context_ref() == "default-sha"
    provider.repo_obj.get_branch.assert_called_once_with("main")


def test_get_repo_context_ref_github_falls_back_to_none_without_repo_obj():
    provider = GithubProvider.__new__(GithubProvider)

    assert provider.get_repo_context_ref(from_default_branch=True) is None
    assert provider.get_repo_context_ref() is None


def test_build_repo_context_github_invalidates_default_branch_cache_when_head_moves(repo_context_settings):
    """The shipping default reads repo-context from the default branch; a push to it
    within the TTL must not serve the previous head's content. Keying the cache on the
    resolved head commit closes the gap the review called out on GitHub."""
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md"])
    repo_context_settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
    repo_context_settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", True)
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = Mock(base=Mock(sha="base-sha", ref="release/1.0"))
    provider.repo_obj = Mock()
    provider.repo_obj.default_branch = "main"
    provider.repo_obj.get_contents.return_value.decoded_content = b"before push"
    provider.repo_obj.get_branch.return_value.commit.sha = "head-1"

    first_context = build_repo_context(provider)
    assert "before push" in first_context

    provider.repo_obj.get_contents.return_value.decoded_content = b"after push"
    provider.repo_obj.get_branch.return_value.commit.sha = "head-2"

    second_context = build_repo_context(provider)

    assert "after push" in second_context
    assert "before push" not in second_context
    assert provider.repo_obj.get_contents.call_count == 2


def test_get_repo_context_ref_gitlab_returns_target_branch():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "owner/repo"
    provider.mr = Mock(target_branch="main")
    provider.gl = Mock()
    provider.gl.projects.get.return_value.default_branch = "project-default"

    assert provider.get_repo_context_ref() == "main"
    assert provider.get_repo_context_ref(from_default_branch=True) == "project-default"


def test_get_repo_context_ref_azure_returns_base_commit():
    provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    provider.workspace_slug = "my-project"
    provider.repo_slug = "my-repo"
    provider.pr = Mock(last_merge_target_commit=Mock(commit_id="base-sha"))

    assert provider.get_repo_context_ref() == "base-sha"
    assert provider.get_repo_context_ref(from_default_branch=True) is None


def test_get_repo_context_ref_bitbucket_server_returns_base_commit():
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.workspace_slug = "PRJ"
    provider.repo_slug = "repo"
    provider.pr = SimpleNamespace(toRef={"latestCommit": "base-sha"})
    provider.bitbucket_client = Mock()

    assert provider.get_repo_context_ref() == "base-sha"
    provider.bitbucket_client.get_default_branch.return_value = {"displayId": "develop"}
    assert provider.get_repo_context_ref(from_default_branch=True) == "develop"


def test_get_repo_context_ref_gitea_returns_base_ref():
    provider = GiteaProvider.__new__(GiteaProvider)
    provider.logger = Mock()
    provider.owner = "owner"
    provider.repo = "repo"
    provider.base_sha = "base-sha"
    provider.base_ref = None
    provider.repo_api = Mock()

    assert provider.get_repo_context_ref() == "base-sha"
    provider.repo_api.repo_get.return_value.default_branch = "main"
    assert provider.get_repo_context_ref(from_default_branch=True) == "main"


def test_get_repo_context_ref_bitbucket_cloud_returns_destination_branch():
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "myws"
    provider.repo_slug = "myrepo"
    provider.pr = Mock(destination_branch="main")
    provider.token = None

    assert provider.get_repo_context_ref() == "main"
    provider.get_repo_default_branch = Mock(return_value="develop")
    assert provider.get_repo_context_ref(from_default_branch=True) == "develop"


def test_get_repo_context_ref_inherits_none_for_providers_without_repo_context():
    provider = CodeCommitProvider.__new__(CodeCommitProvider)

    assert provider.get_repo_context_ref() is None
    assert provider.get_repo_context_ref(from_default_branch=True) is None
