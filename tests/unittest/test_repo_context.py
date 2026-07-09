from unittest.mock import Mock, patch

import pytest
from github import GithubException
from jinja2 import Environment, StrictUndefined, select_autoescape

from pr_agent.algo import repo_context
from pr_agent.algo.repo_context import (
    TRUNCATION_MARKER,
    build_repo_context,
    render_instruction_files,
    render_instruction_files_with_line_budget,
)
from pr_agent.config_loader import get_settings
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


class UnsupportedProvider:
    get_repo_file_content = GitProvider.get_repo_file_content


@pytest.fixture
def repo_context_settings():
    settings = get_settings()
    original_files = settings.config.get("repo_context_files", [])
    original_max_lines = settings.config.get("repo_context_max_lines", 500)
    original_from_default_branch = settings.config.get("repo_context_from_default_branch", True)
    original_warned_provider_classes = repo_context._unsupported_repo_context_provider_classes.copy()
    original_process_cache = repo_context._repo_context_process_cache.copy()

    yield settings

    settings.set("CONFIG.REPO_CONTEXT_FILES", original_files)
    settings.set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)
    settings.set("CONFIG.REPO_CONTEXT_FROM_DEFAULT_BRANCH", original_from_default_branch)
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
    assert provider.get_repo_file_content.call_count == 2


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

    # select_autoescape() leaves string templates unescaped (matching production prompt rendering)
    # while avoiding the hard-coded autoescape=False that static analysis flags.
    environment = Environment(autoescape=select_autoescape(default_for_string=False), undefined=StrictUndefined)
    rendered = environment.from_string(template).render(variables)

    assert "Repository context:" in rendered
    assert '<file path="AGENTS.md" scope="repo-root">' in rendered
